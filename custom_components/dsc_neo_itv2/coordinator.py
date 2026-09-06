"""Per-config-entry coordinator.

Owns the long-lived :class:`ITv2Server`, holds cached panel state
(zones, partitions, labels, connection status), and dispatches HA
signals when state changes.

One instance per HA config entry (one panel).
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import datetime as _dt
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMAND_OUTPUT_PGM_TYPES,
    CONN_STATE_CONNECTED,
    CONN_STATE_DISCONNECTED,
    CONN_STATE_FAILED,
    CONN_STATE_HANDSHAKING,
    DOMAIN,
    EVENT_ARM,
    EVENT_DISARM,
    EVENT_FIRE_PRE_ALERT,
    EVENT_PARTITION_ALARM,
    EVENT_TROUBLE_CHANGE,
    EVENT_USER_ACTIVITY,
    EVENT_ZONE_CHANGE,
    MANUFACTURER,
    MAX_PGM_TO_PROBE,
    MODEL,
    SIGNAL_CONNECTION_UPDATE,
    SIGNAL_NEW_ZONE,
    SIGNAL_PARTITION_UPDATE,
    SIGNAL_TROUBLE_UPDATE,
    SIGNAL_ZONE_UPDATE,
)
from .dsc_itv2.enums import ArmingMode, ProgrammingMode, ZoneStatusFlags
from .dsc_itv2.messages import (
    TROUBLE_GROUP_LABELS,
    CommandError,
    CommandRequestMessage,
    CommandResponse,
    ConfigurationEnter,
    ConfigurationExit,
    ConfigurationWriteTimeAndDate,
    ConnectionPoll,
    ConnectionSystemCapabilities,
    DefaultMessage,
    ModuleControlCommandOutput,
    ModulePartitionStatus,
    ModuleStatusSystemTroubleStatusNew,
    ModuleZoneStatus,
    MultipleMessagePacket,
    NotificationArmDisarm,
    NotificationBufferOverload,
    NotificationChimeBroadcast,
    NotificationDateTimeBroadcast,
    NotificationEntryDelay,
    NotificationExitDelay,
    NotificationLabelText,
    NotificationLifestyleZoneStatus,
    NotificationPartitionAlarmMemory,
    NotificationPartitionBypassStatus,
    NotificationPartitionReadyStatus,
    NotificationPartitionTroubleStatus,
    PartitionArm,
    PartitionDisarm,
    ProgrammingLeadInOut,
    ResponseTimeAndDate,
    SectionRead,
    SectionReadResponse,
    SingleZoneBypassStatus,
)
from .dsc_itv2.server import ITv2Connection, ITv2Server
from .dsc_itv2.session import (
    Closed,
    Connected,
    ConnectionSettings,
    Failed,
    Notification,
)
from .repairs import (
    clear_all_trouble_issues,
    clear_auth_failed_issue,
    is_auth_failure_reason,
    set_auth_failed_issue,
    sync_trouble_issues,
)
from .storage import CachedPanelConfig, PanelConfigStore
from .zone_definitions import is_fire_zone

_LOGGER = logging.getLogger(__name__)


@dataclass
class ZoneState:
    number: int
    label: str | None = None
    is_open: bool = False
    is_tamper: bool = False
    is_trouble: bool = False
    is_low_battery: bool = False
    is_alarm: bool = False
    is_bypassed: bool = False
    raw_status: int = 0


@dataclass
class PartitionState:
    number: int
    label: str | None = None
    raw_status1: int = 0
    raw_status2: int = 0
    raw_status3: int = 0
    is_armed: bool = False
    arming_mode: int = 0
    is_ready: bool = False
    is_in_alarm: bool = False
    is_fire_alarm: bool = False        # inferred from active fire zones
    is_burglary_alarm: bool = False    # inferred when in alarm and not fire
    is_alarm_in_memory: bool = False
    is_entry_delay: bool = False
    is_exit_delay: bool = False
    is_quick_exit: bool = False
    is_fire_pre_alert: bool = False
    is_walk_test: bool = False
    is_programming_mode: bool = False
    is_door_chime_enabled: bool = False
    is_audible_bell: bool = False
    is_trouble: bool = False
    is_bypassed: bool = False
    exit_delay_seconds: int = 0
    entry_delay_seconds: int = 0
    last_user_id: int | None = None


@dataclass
class TroubleState:
    """System-wide trouble bitmap, indexed by 1-based group ID.

    Each value is the raw bitmap byte for that group. A non-zero byte
    means the group has at least one active sub-item. Group labels and
    sub-item meanings come from the HS2032 ``[*][2]`` Trouble menu;
    see ``TROUBLE_GROUP_LABELS`` in ``dsc_itv2.messages``.
    """
    groups: dict[int, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.groups is None:
            self.groups = {}

    @property
    def any_active(self) -> bool:
        return any(v != 0 for v in self.groups.values())

    def is_group_active(self, group_id: int) -> bool:
        return bool(self.groups.get(group_id, 0))


# Typed config entry alias. With ``ConfigEntry[DscNeoCoordinator]`` HA's
# generic ConfigEntry exposes ``entry.runtime_data`` as the coordinator
# itself, eliminating the ``hass.data[DOMAIN][entry_id]`` indirection.
if TYPE_CHECKING:
    DscNeoConfigEntry = ConfigEntry["DscNeoCoordinator"]
else:
    DscNeoConfigEntry = ConfigEntry


class DscNeoCoordinator:
    """Owns one TL280 listener and the cached panel state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        settings: ConnectionSettings,
        host: str,
        port: int,
        installer_code: str | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.settings = settings
        self.host = host
        self.port = port
        self.installer_code = installer_code or None

        self.zones: dict[int, ZoneState] = {}
        self.partitions: dict[int, PartitionState] = {}
        self.zone_labels: dict[int, str] = {}
        self.partition_labels: dict[int, str] = {}
        # System-wide trouble bitmap (from ModuleStatus_System_Trouble_Status_New).
        # The panel pushes this unsolicited; we never request it. That means
        # the bitmap stays at its default until the panel volunteers a
        # snapshot, so these sensors can read clear on a fresh connection
        # even if a trouble is active. Surfaced as diagnostic binary_sensors.
        self.trouble: TroubleState = TroubleState()
        # Most-recent panel-reported date/time, taken from the panel's
        # periodic NotificationDateTimeBroadcast pushes. ``None`` means we
        # haven't received one yet.
        self.panel_datetime: _dt.datetime | None = None

        self.connection_state: str = CONN_STATE_DISCONNECTED
        self.panel_firmware: str | None = None
        self.panel_session_id: str | None = None
        # Populated by ConnectionSystemCapabilities response on connect.
        self.max_zones: int = 0
        self.max_partitions: int = 0
        self.max_users: int = 0
        # Populated by reading installer-mode sections (requires installer code).
        # When non-empty, entity creation can filter to enabled items only.
        # Map: zone_number -> zone_definition byte (0 = NullZone = disabled).
        self.zone_definitions: dict[int, int] = {}
        # Map: partition_number -> bool (True = enabled, False = disabled).
        self.partition_enabled: dict[int, bool] = {}
        # Map: partition_number -> sorted list of zone numbers assigned to it,
        # decoded from sections [201]..[208] (partition-zone assignment bitmap).
        # An empty list means "no zones assigned" (rare). A missing key means
        # "we never read this section for this partition" and consumers should
        # fall back to the legacy heuristic.
        self.zone_partition_assignment: dict[int, list[int]] = {}
        # Map: zone_number -> functional-attribute byte (section [002][N] byte 0).
        self.zone_attributes_functional: dict[int, int] = {}
        # Map: zone_number -> physical-attribute byte  (section [002][N] byte 1).
        self.zone_attributes_physical: dict[int, int] = {}
        # Map: zone_number -> event-reporting byte    (section [307][N]).
        self.zone_event_reporting: dict[int, int] = {}
        # Map: pgm_number -> PGM type code (section [009][N], 100 = Null PGM).
        # PGM numbering is global across onboard + expansion modules.
        self.pgm_types: dict[int, int] = {}
        # Map: pgm_number -> partition number 1-8 (section [007][N], 0 if
        # unassigned). Only applies to PGM types that support
        # multi-partition operation (e.g. command outputs); for other
        # types the value is meaningless.
        self.pgm_partition_assignment: dict[int, int] = {}
        # True once the installer-mode section read finished successfully.
        self.has_panel_config: bool = False
        # Timestamp of the cached pull (ISO-8601 string). Surfaced as
        # an attribute on the connection-state sensor.
        self.panel_config_fetched_at: str | None = None

        self._server: ITv2Server | None = None
        self._active_conn: ITv2Connection | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stop_event_unsub = None
        # Persistent storage for installer-mode-derived data.
        self._store: PanelConfigStore = PanelConfigStore(hass, entry_id)
        # Label responses arrive asynchronously and can trail the request that
        # triggered the panel-config save. Debounce follow-up writes so late
        # batches are persisted without writing HA storage once per label.
        self._label_cache_save_task: asyncio.Task | None = None
        # Set to True by async_reload_panel_config() to force the next
        # _initial_state_pull to re-enter installer mode even if cached.
        self._force_reload_config: bool = False

        # Reconnect-storm dampening: log a summary instead of N warnings
        # if the panel keeps reconnecting and failing within a short
        # window. ``_failure_window_start`` resets after FAILURE_WINDOW
        # seconds of quiet.
        self._failure_count_in_window: int = 0
        self._failure_window_start: float = 0.0
        self.FAILURE_WINDOW: float = 60.0
        self.FAILURE_WARN_EVERY: int = 5

        # Heartbeat interval: NeoHub uses 100s; the panel typically
        # tolerates up to ~150s of silence before tearing down. We pick
        # 60s so the worst-case detection latency for a dead link is
        # bounded by 60s + TCP keepalive.
        self.HEARTBEAT_INTERVAL: float = 60.0

    # ---- lifecycle ----

    async def async_start(self) -> None:
        if self._server is not None:
            return
        # Hydrate cached panel config so entities can self-filter even
        # before the first panel connection happens.
        cached = await self._store.async_load()
        if not cached.is_empty:
            self._apply_cached_config(cached)
            _LOGGER.info(
                "Loaded cached panel config: %d enabled zones, %d enabled partitions (fetched %s)",
                sum(1 for d in self.zone_definitions.values() if d != 0),
                sum(1 for e in self.partition_enabled.values() if e),
                self.panel_config_fetched_at,
            )
            self.async_prune_stale_entities()
        self._server = ITv2Server(self.settings, host=self.host, port=self.port)
        await self._server.start()
        _LOGGER.info(
            "DSC Neo coordinator listening on %s:%d (entry %s)",
            self.host, self._server.port, self.entry_id,
        )
        self._spawn(self._accept_loop(), name="dsc-accept")
        self._stop_event_unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            lambda _e: self.hass.async_create_task(self.async_stop()),
        )

    async def async_stop(self) -> None:
        if self._stop_event_unsub is not None:
            self._stop_event_unsub()
            self._stop_event_unsub = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._active_conn = None
        self._update_connection_state(CONN_STATE_DISCONNECTED)
        # Drop any open Repairs prompts - they're stale once we unload.
        clear_all_trouble_issues(self.hass, self.entry_id)

    # ---- send API ----

    async def async_arm_partition(
        self, partition: int, mode: ArmingMode, access_code: str = "",
    ) -> None:
        conn = self._require_connected()
        msg = PartitionArm(
            command_sequence=0, partition=partition,
            arm_mode=int(mode), access_code=access_code or "",
        )
        await conn.send_message(msg)

    async def async_disarm_partition(self, partition: int, access_code: str) -> None:
        conn = self._require_connected()
        msg = PartitionDisarm(
            command_sequence=0, partition=partition, access_code=access_code,
        )
        await conn.send_message(msg)

    async def async_bypass_zone(
        self, partition: int, zone: int, bypass: bool,
    ) -> None:
        from .dsc_itv2.messages import SingleZoneBypassWrite
        conn = self._require_connected()
        msg = SingleZoneBypassWrite(
            command_sequence=0, partition=partition,
            zone_number=zone, bypass_state=1 if bypass else 0,
        )
        await conn.send_message(msg)

    async def async_command_output(
        self,
        output_number: int,
        partition: int = 1,
        access_code: str = "",
    ) -> None:
        """Trigger a panel command-output (PGM action).

        Maps to the ``[*][7][N]`` keypad action where ``N`` is the
        command-output number (1..4 on the HS2032). The PGM's
        programmed type (``Sensor Reset``, ``Chime On/Off``,
        ``Command Output``, ...) determines what physically happens
        when this fires; the ITv2 command itself just says "fire
        command output N on partition P".

        Common uses on the HS2032:
            * ``output_number=2`` with PGM2 programmed as **Sensor
              Reset** - power-cycles 2-wire smoke detectors after an
              alarm. Equivalent to ``[*][7][2]`` at the keypad.
            * ``output_number=1`` with a PGM programmed as **Chime
              Enable** - toggles door chime.

        ``access_code`` is the user PIN that authorises the action when
        the panel requires one; pass the stored user PIN or installer
        code if needed.
        """
        conn = self._require_connected()
        await conn.send_message(
            ModuleControlCommandOutput(
                command_sequence=0,
                partition=partition,
                output_number=output_number,
                access_code=access_code or "",
            )
        )

    async def async_set_panel_datetime(
        self, when: _dt.datetime | None = None,
    ) -> None:
        """Write a new date/time to the panel clock.

        ``when`` defaults to the HA host's current local time. The Neo
        panel stores naive local time - no timezone field on the wire -
        so callers should pass a naive datetime in the panel's local
        timezone (typically your HA system's local time).

        Used by the ``set_date_time`` service and by an optional
        startup sync to fix the panel's "loss of clock" trouble.
        """
        conn = self._require_connected()
        dt_value = when if when is not None else _dt.datetime.now()
        # Strip tzinfo - the wire format is naive local time.
        if dt_value.tzinfo is not None:
            dt_value = dt_value.astimezone().replace(tzinfo=None)
        await conn.send_message(
            ConfigurationWriteTimeAndDate(
                command_sequence=0, date_time=dt_value,
            )
        )
        # Reflect the change locally; the panel may also broadcast a
        # NotificationDateTimeBroadcast within the next minute which
        # will overwrite this with the panel's own reading.
        self.panel_datetime = dt_value
        async_dispatcher_send(
            self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
        )

    async def async_reload_panel_config(self) -> bool:
        """Force a fresh installer-mode read on the next initial-pull.

        Wired to the "Reload Panel Config" button entity. Triggers an
        immediate pull if the panel is currently connected; otherwise
        the next reconnect will pick up the flag.

        Returns True if a fresh read was started, False if it can't
        run right now (no installer code, not connected).
        """
        if not self.installer_code:
            _LOGGER.warning(
                "Reload-panel-config requested but no installer code configured",
            )
            return False
        self._force_reload_config = True
        conn = self._active_conn
        if conn is None or not conn.is_open:
            _LOGGER.info(
                "Reload-panel-config queued; will run on next panel connection",
            )
            return True
        # Run a pull right now in a background task.
        self._spawn(
            self._reload_pull(conn),
            name=f"dsc-reload-{conn.peer_addr[0]}",
        )
        return True

    async def _reload_pull(self, conn: ITv2Connection) -> None:
        """One-shot installer-mode re-read triggered by the reload button."""
        try:
            _LOGGER.info("Manual reload: clearing cache + re-reading panel config")
            self.zone_definitions.clear()
            self.partition_enabled.clear()
            self.zone_partition_assignment.clear()
            self.zone_labels.clear()
            self.partition_labels.clear()
            self.has_panel_config = False
            self._force_reload_config = False
            await self._read_panel_config(
                conn,
                self.max_zones or 128,
                self.max_partitions or 8,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover (best-effort)
            _LOGGER.exception("Manual reload failed")

    # ---- helpers ----

    def _require_connected(self) -> ITv2Connection:
        if self._active_conn is None or not self._active_conn.is_open:
            raise RuntimeError("DSC panel is not currently connected")
        return self._active_conn

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        task = self.hass.async_create_background_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @callback
    def _schedule_label_cache_save(self) -> None:
        """Persist asynchronously received labels after a short debounce."""
        task = self._label_cache_save_task
        if task is not None and not task.done():
            task.cancel()
        self._label_cache_save_task = self._spawn(
            self._save_label_cache_after_delay(),
            name="dsc-label-cache-save",
        )

    async def _save_label_cache_after_delay(self) -> None:
        await asyncio.sleep(0.5)
        await self._save_cache_now()

    @callback
    def _update_connection_state(self, new_state: str) -> None:
        if new_state == self.connection_state:
            return
        self.connection_state = new_state
        async_dispatcher_send(
            self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
        )

    @callback
    def _send_zone_signal(self, zone_no: int) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_ZONE_UPDATE}_{self.entry_id}_{zone_no}",
        )

    @callback
    def _send_new_zone_signal(self, zone_no: int) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_NEW_ZONE}_{self.entry_id}", zone_no,
        )

    @callback
    def _send_partition_signal(self, partition_no: int) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_PARTITION_UPDATE}_{self.entry_id}_{partition_no}",
        )

    @callback
    def _send_trouble_signal(self) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_TROUBLE_UPDATE}_{self.entry_id}",
        )

    @callback
    def _fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.hass.bus.async_fire(event_type, {"entry_id": self.entry_id, **data})

    @callback
    def is_zone_enabled(self, zone_no: int) -> bool:
        """True if the zone is enabled (or we don't know yet).

        Without installer config, we have to assume every zone is
        enabled - better to over-expose than to hide real zones.
        """
        if not self.has_panel_config:
            return True
        return self.zone_definitions.get(zone_no, 0) != 0

    @property
    def panel_device_info(self) -> DeviceInfo:
        """Single source of truth for the HA device-registry entry.

        Every entity in this integration must use this so they all
        cluster under one device card in the UI rather than appearing
        as N separate devices.
        """
        name = f"DSC Neo ({self.panel_session_id})" if self.panel_session_id else "DSC Neo"
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=name,
            sw_version=self.panel_firmware,
            # No ``configuration_url``: the panel is a TCP endpoint,
            # not a web UI, and HA's validator only accepts
            # http(s):// / homeassistant:// schemes. The host:port
            # is surfaced on the connection-state sensor and in
            # diagnostics for users who need it.
        )

    @callback
    def is_partition_enabled(self, partition_no: int) -> bool:
        """True if the partition is enabled (or we don't know yet)."""
        if not self.has_panel_config:
            return True
        return self.partition_enabled.get(partition_no, True)

    @callback
    def command_output_pgms(
        self, partition_no: int, output_no: int,
    ) -> list[tuple[int, int]]:
        """Return PGMs programmed for a command-output slot and partition."""
        triggering_types = COMMAND_OUTPUT_PGM_TYPES.get(output_no, frozenset())
        matches: list[tuple[int, int]] = []
        for pgm_no, type_code in sorted(self.pgm_types.items()):
            if type_code not in triggering_types:
                continue
            assigned_partition = self.pgm_partition_assignment.get(pgm_no, 1) or 1
            if assigned_partition == partition_no:
                matches.append((pgm_no, type_code))
        return matches

    @callback
    def zones_for_partition(self, partition_no: int) -> list[int]:
        """Return the zone numbers assigned to *partition_no*.

        Source order:
            1. Decoded partition-zone assignment table (sections [201]..[208])
               authoritative when present.
            2. Fallback heuristic for panels we haven't yet pulled the
               assignment table from: partition 1 owns every enabled zone,
               other partitions own none. This matches the common
               single-partition install and is harmless for multi-partition
               setups (the first read pass will replace it).
        """
        assigned = self.zone_partition_assignment.get(partition_no)
        if assigned is not None:
            return list(assigned)
        if partition_no == 1:
            return sorted(self.zones)
        return []

    @callback
    def _touch_zone(self, zone_no: int) -> ZoneState:
        zone = self.zones.get(zone_no)
        if zone is None:
            zone = ZoneState(number=zone_no, label=self.zone_labels.get(zone_no))
            self.zones[zone_no] = zone
            # Only surface as a new HA entity if the zone is enabled.
            if self.is_zone_enabled(zone_no):
                self._send_new_zone_signal(zone_no)
        elif zone.label is None and zone_no in self.zone_labels:
            zone.label = self.zone_labels[zone_no]
        return zone

    @callback
    def _touch_partition(self, partition_no: int) -> PartitionState:
        part = self.partitions.get(partition_no)
        if part is None:
            part = PartitionState(
                number=partition_no,
                label=self.partition_labels.get(partition_no),
            )
            self.partitions[partition_no] = part
        return part

    @callback
    def _record_failure(self, reason: str) -> None:
        """Log handshake failures while dampening reconnect-storm spam.

        First failure in a quiet window logs at WARNING. Subsequent
        failures within ``FAILURE_WINDOW`` seconds are counted; we only
        emit another WARNING every ``FAILURE_WARN_EVERY`` failures
        (with the running count). Detailed messages always go to DEBUG.
        """
        now = time.monotonic()
        _LOGGER.debug("DSC connection failed: %s", reason)
        if now - self._failure_window_start > self.FAILURE_WINDOW:
            self._failure_count_in_window = 1
            self._failure_window_start = now
            _LOGGER.warning("DSC connection failed: %s", reason)
            return
        self._failure_count_in_window += 1
        if self._failure_count_in_window % self.FAILURE_WARN_EVERY == 0:
            _LOGGER.warning(
                "DSC connection still failing (%d times in last %ds): %s",
                self._failure_count_in_window,
                int(now - self._failure_window_start),
                reason,
            )

    async def _heartbeat_loop(self, conn: ITv2Connection) -> None:
        """Send :class:`ConnectionPoll` periodically to keep the panel
        from idling the session out.

        The DSC panel closes the ITv2 session after a long silence; a
        periodic empty ``Connection_Poll`` (0x0600) message resets that
        timer. Loop exits when the connection closes or we're cancelled.
        """
        try:
            while conn.is_open:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                if not conn.is_open:
                    return
                try:
                    await conn.send_message(ConnectionPoll())
                except Exception:  # pragma: no cover (best-effort)
                    _LOGGER.debug("Heartbeat send failed", exc_info=True)
                    return
        except asyncio.CancelledError:
            raise

    async def _initial_state_pull(self, conn: ITv2Connection) -> None:
        """Proactively request system state after handshake completes.

        Sequence:
          1. Wait 2s for the panel's initial post-handshake burst.
          2. Request system capabilities (max zones/partitions/users).
          3. If installer code provided, enter installer programming
             mode and read sections [001]/[200] for zone definitions
             and partition enables. Lets us filter out disabled items.
          4. Request zone status (full range).
          5. Request partition status (each partition).
          6. Request labels that are not already present in the panel-config
             cache.

        Each request is throttled by REQUEST_GAP seconds to keep the
        panel's input buffer from overflowing.
        """
        LABEL_BATCH = 8     # request labels in groups of 8
        REQUEST_GAP = 0.10  # 100ms between requests (panel buffer-friendly)

        async def _send(msg, what: str) -> bool:
            if not conn.is_open:
                return False
            try:
                await conn.send_message(msg)
                await asyncio.sleep(REQUEST_GAP)
                return True
            except Exception:  # pragma: no cover (best-effort)
                _LOGGER.debug("Initial-state request %s failed", what, exc_info=True)
                return False

        try:
            await asyncio.sleep(2.0)
            if not conn.is_open:
                return

            _LOGGER.debug("Requesting panel capabilities...")
            await _send(
                CommandRequestMessage(
                    command_sequence=0, request=ConnectionSystemCapabilities(),
                ),
                "capabilities",
            )

            # Wait briefly for the response.
            for _ in range(20):
                await asyncio.sleep(0.1)
                if self.max_zones or self.max_partitions:
                    break
            max_zones = self.max_zones or 128
            max_partitions = self.max_partitions or 8

            # Try installer-mode config read on first install OR when the
            # user clicked the Reload Panel Config button. Subsequent
            # reconnects skip this: we rely on the cached snapshot.
            should_read_config = (
                self.installer_code
                and conn.is_open
                and (not self.has_panel_config or self._force_reload_config)
            )
            if should_read_config:
                self._force_reload_config = False
                try:
                    await self._read_panel_config(conn, max_zones, max_partitions)
                except Exception:  # pragma: no cover (best-effort)
                    _LOGGER.exception("Installer-mode config read failed")
            elif self.has_panel_config:
                _LOGGER.info(
                    "Using cached panel config (fetched %s); skipping installer-mode read",
                    self.panel_config_fetched_at,
                )

            # Zone status: covers all physically-defined zones.
            _LOGGER.debug("Requesting zone status for zones 1..%d", max_zones)
            await _send(
                CommandRequestMessage(
                    command_sequence=0,
                    request=ModuleZoneStatus(
                        zone_start=1, zone_count=max_zones,
                        status_size_in_bytes=1, zone_status_bytes=b"",
                    ),
                ),
                "zone status",
            )

            for partition in range(1, max_partitions + 1):
                if (
                    self.has_panel_config
                    and not self.partition_enabled.get(partition, True)
                ):
                    continue   # skip disabled partitions
                _LOGGER.debug("Requesting partition %d status", partition)
                await _send(
                    CommandRequestMessage(
                        command_sequence=0,
                        request=ModulePartitionStatus(partition=partition),
                    ),
                    f"partition {partition} status",
                )

            # System-wide trouble polling intentionally omitted. Empirical
            # evidence (TL280 fw 5.61 logs, 2026-05-21) shows the panel
            # rejects all four ITv2 trouble-query opcodes: 0x0814 /
            # 0x0817 / 0x0821 / 0x0822: with Command_Response code=1
            # ("Invalid Type" per NeoHub CommandResponseCode.cs). We
            # rely on (a) the per-partition `TroublesPresent` bit in
            # ModulePartitionStatus, and (b) unsolicited cmd 0x023F
            # (NotificationPartitionTroubleStatus) pushes; the keypad
            # `[*][2]` menu remains the only source for system-level
            # detail (cellular, AC, battery, ...). The inbound handler
            # for 0x0822 is still wired up in case a newer panel ever
            # pushes one unsolicited.

            # Repair missing labels even when installer configuration came
            # from cache. Older/incomplete caches could contain definitions
            # but only the first few asynchronous label responses.
            for start in range(1, max_zones + 1, LABEL_BATCH):
                end = min(start + LABEL_BATCH - 1, max_zones)
                if self.has_panel_config:
                    expected = [
                        zone
                        for zone in range(start, end + 1)
                        if self.zone_definitions.get(zone, 0) != 0
                    ]
                    if not any(zone not in self.zone_labels for zone in expected):
                        continue
                _LOGGER.debug("Requesting zone labels %d-%d", start, end)
                await _send(
                    CommandRequestMessage(
                        command_sequence=0,
                        request=NotificationLabelText(
                            collection=0xD1, start=start, end=end, labels=(),
                        ),
                    ),
                    f"zone labels {start}-{end}",
                )

            if max_partitions:
                expected_partitions = (
                    [
                        partition
                        for partition in range(1, max_partitions + 1)
                        if self.partition_enabled.get(partition, True)
                    ]
                    if self.has_panel_config
                    else list(range(1, max_partitions + 1))
                )
                if any(
                    partition not in self.partition_labels
                    for partition in expected_partitions
                ):
                    _LOGGER.debug(
                        "Requesting partition labels (1..%d)", max_partitions,
                    )
                    await _send(
                        CommandRequestMessage(
                            command_sequence=0,
                            request=NotificationLabelText(
                                collection=0xD3,
                                start=1,
                                end=max_partitions,
                                labels=(),
                            ),
                        ),
                        "partition labels",
                    )

            _LOGGER.info(
                "Initial-state pull complete (panel config: %s, enabled zones: %d, enabled partitions: %d)",
                "yes" if self.has_panel_config else "no",
                sum(1 for d in self.zone_definitions.values() if d != 0)
                if self.has_panel_config else max_zones,
                sum(1 for e in self.partition_enabled.values() if e)
                if self.has_panel_config else max_partitions,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover (defensive)
            _LOGGER.exception("Initial-state pull crashed")

    async def _read_panel_config(
        self, conn: ITv2Connection, max_zones: int, max_partitions: int,
    ) -> None:
        """Read installer-mode-only data and cache it.

        Reads:
          * [001][zone]: zone type byte (0 = NullZone)
          * [200][1]: enabled-partition bitmap (LSB = partition 1)
          * Zone labels: via wildcard NotificationLabelText request
            (faster than section reads and works without leaving installer mode)
          * Partition labels: same as above

        Persists everything to :class:`PanelConfigStore` so subsequent
        connects can skip installer-mode entry entirely.

        Requires :attr:`installer_code`.
        """
        if not self.installer_code:
            return

        _LOGGER.info("Entering installer programming mode to read panel config")
        await conn.send_message(ConfigurationEnter(
            command_sequence=0, partition=1,
            programming_mode=int(ProgrammingMode.InstallersProgramming),
            access_code=self.installer_code,
            read_write=0,   # ReadOnlyMode
        ))
        await asyncio.sleep(0.5)
        if not conn.is_open:
            return

        # --- Zone definitions: [001][zone] ---
        for zone in range(1, max_zones + 1):
            if not conn.is_open:
                break
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(1, zone),
            ))
            await asyncio.sleep(0.05)
            if zone not in self.zone_definitions:
                await asyncio.sleep(0.05)

        # --- Zone attributes: [002][zone] (2 bytes: functional, physical) ---
        for zone in range(1, max_zones + 1):
            if not conn.is_open:
                break
            # Skip zones we've already established are disabled. Saves
            # ~32 unnecessary section reads on a typical install.
            if self.zone_definitions.get(zone, 0) == 0:
                continue
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(2, zone),
            ))
            await asyncio.sleep(0.05)

        # --- Zone event reporting: [307][zone] (1 byte) ---
        for zone in range(1, max_zones + 1):
            if not conn.is_open:
                break
            if self.zone_definitions.get(zone, 0) == 0:
                continue
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(307, zone),
            ))
            await asyncio.sleep(0.05)

        # --- Partition enables: [200][1] is one LSB-first bitmap ---
        if conn.is_open:
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(200, 1),
            ))
            await asyncio.sleep(0.05)
            if not self.partition_enabled:
                await asyncio.sleep(0.05)

        # --- Partition-zone assignment bitmap: [200 + partition][byteIdx]
        #     Each byte covers 8 zones (MSB-first). For HS2032 with 32 zones
        #     we need 4 byte reads per partition (sections [201][001..004],
        #     [202][001..004], ..., [208][001..004]).
        bytes_per_partition = (max_zones + 7) // 8
        for partition in range(1, max_partitions + 1):
            if not conn.is_open:
                break
            # Skip disabled partitions: saves a few keypad chirps.
            if not self.partition_enabled.get(partition, True):
                continue
            # Wipe any stale assignment so re-reads are clean.
            self.zone_partition_assignment.pop(partition, None)
            for byte_idx in range(1, bytes_per_partition + 1):
                if not conn.is_open:
                    break
                await conn.send_message(SectionRead(
                    command_sequence=0,
                    section_address=(200 + partition, byte_idx),
                ))
                await asyncio.sleep(0.05)

        # --- PGM programming: [009][pgm_no] is the type code, [007][pgm_no]
        # is the partition assignment (1-8). PGM numbering is global across
        # onboard outputs and HSM2208/HSM2204 expanders. Scan up to
        # ``MAX_PGM_TO_PROBE`` PGMs; the [007] pass skips null PGMs so cost
        # scales with the [009] pass only (~50ms * MAX_PGM_TO_PROBE).
        self.pgm_types = {}
        self.pgm_partition_assignment = {}
        for pgm_no in range(1, MAX_PGM_TO_PROBE + 1):
            if not conn.is_open:
                break
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(9, pgm_no),
            ))
            await asyncio.sleep(0.05)
        for pgm_no in range(1, MAX_PGM_TO_PROBE + 1):
            if not conn.is_open:
                break
            # Only ask for the partition byte once we know the PGM is
            # programmed: saves N reads for installs that don't use
            # expansion modules. If the [009] read raced, we'll just miss
            # the [007] byte for that PGM and the button falls back to
            # "partition 1" attribution.
            if self.pgm_types.get(pgm_no, 100) == 100:
                continue
            await conn.send_message(SectionRead(
                command_sequence=0,
                section_address=(7, pgm_no),
            ))
            await asyncio.sleep(0.05)

        # --- Labels: use the CommandRequestMessage(NotificationLabelText)
        # path while still in installer mode. It works inside or outside
        # installer mode but we already have the connection warmed up.
        LABEL_BATCH = 8
        for start in range(1, max_zones + 1, LABEL_BATCH):
            if not conn.is_open:
                break
            end = min(start + LABEL_BATCH - 1, max_zones)
            if self.zone_definitions and not any(
                self.zone_definitions.get(zone, 0) != 0
                for zone in range(start, end + 1)
            ):
                continue
            await conn.send_message(CommandRequestMessage(
                command_sequence=0,
                request=NotificationLabelText(
                    collection=0xD1, start=start, end=end, labels=(),
                ),
            ))
            await asyncio.sleep(0.10)

        if max_partitions:
            await conn.send_message(CommandRequestMessage(
                command_sequence=0,
                request=NotificationLabelText(
                    collection=0xD3, start=1, end=max_partitions, labels=(),
                ),
            ))
            await asyncio.sleep(0.10)

        # Exit installer mode promptly.
        if conn.is_open:
            await conn.send_message(ConfigurationExit(
                command_sequence=0, partition=1,
            ))
            await asyncio.sleep(0.2)

        n_enabled_zones = sum(1 for d in self.zone_definitions.values() if d != 0)
        n_enabled_parts = sum(1 for e in self.partition_enabled.values() if e)
        if n_enabled_zones > 0 or n_enabled_parts > 0:
            self.has_panel_config = True
            _LOGGER.info(
                "Panel config read: %d enabled zones, %d enabled partitions",
                n_enabled_zones, n_enabled_parts,
            )
            # Persist to HA storage so next connect can skip installer mode.
            await self._save_cache_now()
            # Drop entities for any zone/partition that was previously enabled
            # but is now disabled in the freshly-read panel config.
            self.async_prune_stale_entities()
            # Wake any already-existing entities so their `available`
            # property re-evaluates against the freshly-loaded enable
            # bitmap. Without this, entities for newly-disabled zones
            # stay at their last-known value instead of flipping to
            # unavailable.
            async_dispatcher_send(
                self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
            )
            for zone_no in self.zone_definitions:
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_ZONE_UPDATE}_{self.entry_id}_{zone_no}",
                )
            for partition_no in self.partition_enabled:
                async_dispatcher_send(
                    self.hass,
                    f"{SIGNAL_PARTITION_UPDATE}_{self.entry_id}_{partition_no}",
                )
        else:
            _LOGGER.warning(
                "Panel config read returned no data: installer code may be wrong, "
                "or panel didn't accept ConfigurationEnter. Falling back to all-zones mode.",
            )

    @callback
    def _apply_cached_config(self, cached: CachedPanelConfig) -> None:
        """Hydrate live state from a :class:`CachedPanelConfig` on startup."""
        if cached.max_zones:
            self.max_zones = cached.max_zones
        if cached.max_partitions:
            self.max_partitions = cached.max_partitions
        if cached.max_users:
            self.max_users = cached.max_users
        self.zone_definitions = {int(k): v for k, v in cached.zone_definitions.items()}
        self.partition_enabled = {int(k): v for k, v in cached.partition_enabled.items()}
        self.zone_partition_assignment = {
            int(k): sorted(int(z) for z in v)
            for k, v in cached.zone_partition_assignment.items()
        }
        self.zone_attributes_functional = {
            int(k): int(v) & 0xFF for k, v in cached.zone_attributes_functional.items()
        }
        self.zone_attributes_physical = {
            int(k): int(v) & 0xFF for k, v in cached.zone_attributes_physical.items()
        }
        self.zone_event_reporting = {
            int(k): int(v) & 0xFF for k, v in cached.zone_event_reporting.items()
        }
        self.pgm_types = {
            int(k): int(v) & 0xFF for k, v in cached.pgm_types.items()
        }
        self.pgm_partition_assignment = {
            int(k): int(v) & 0xFF
            for k, v in cached.pgm_partition_assignment.items()
        }
        self.zone_labels = {int(k): v for k, v in cached.zone_labels.items()}
        self.partition_labels = {int(k): v for k, v in cached.partition_labels.items()}
        self.panel_config_fetched_at = cached.fetched_at
        if self.zone_definitions or self.partition_enabled:
            self.has_panel_config = True
        # Materialise enabled cached zones/partitions before the first panel
        # status pull. Platforms can then create correctly named entities as
        # soon as the config entry loads instead of waiting several seconds.
        for zone_no, definition in self.zone_definitions.items():
            if definition != 0:
                self.zones[zone_no] = ZoneState(
                    number=zone_no,
                    label=self.zone_labels.get(zone_no),
                )
        for partition_no, enabled in self.partition_enabled.items():
            if enabled:
                self.partitions[partition_no] = PartitionState(
                    number=partition_no,
                    label=self.partition_labels.get(partition_no),
                )

    @callback
    def async_prune_stale_entities(self) -> int:
        """Remove entity-registry entries no longer backed by panel config.

        Walks every entity registered against this config entry and drops the
        ones whose unique_id encodes a zone, partition or command output the
        panel has since marked unavailable.
        Globals (firmware, connection, panel_time, trouble_* ...) are never
        touched. Returns the number of entries removed.
        """
        registry = er.async_get(self.hass)
        entries = er.async_entries_for_config_entry(registry, self.entry_id)
        prefix = f"{self.entry_id}_"
        removed = 0
        for ent in list(entries):
            uid = ent.unique_id or ""
            if not uid.startswith(prefix):
                continue
            tail = uid[len(prefix):]
            parts = tail.split("_")
            zone_no: int | None = None
            partition_no: int | None = None
            command_output_no: int | None = None
            if (
                len(parts) == 5
                and parts[0] == "partition"
                and parts[1].isdigit()
                and parts[2:4] == ["command", "output"]
                and parts[4].isdigit()
            ):
                partition_no = int(parts[1])
                command_output_no = int(parts[4])
            elif len(parts) >= 2 and parts[0] == "zone" and parts[1].isdigit():
                zone_no = int(parts[1])
            elif len(parts) >= 2 and parts[0] == "partition" and parts[1].isdigit():
                partition_no = int(parts[1])
            else:
                continue  # global entity: never prune
            if zone_no is not None:
                # Only prune if the panel told us about this zone AND it's disabled.
                if zone_no in self.zone_definitions and self.zone_definitions[zone_no] == 0:
                    registry.async_remove(ent.entity_id)
                    removed += 1
            elif partition_no is not None and command_output_no is not None:
                partition_disabled = (
                    partition_no in self.partition_enabled
                    and not self.partition_enabled[partition_no]
                )
                output_unavailable = (
                    bool(self.installer_code and self.pgm_types)
                    and not self.command_output_pgms(
                        partition_no, command_output_no,
                    )
                )
                if partition_disabled or output_unavailable:
                    registry.async_remove(ent.entity_id)
                    removed += 1
            elif partition_no is not None:
                if (
                    partition_no in self.partition_enabled
                    and not self.partition_enabled[partition_no]
                ):
                    registry.async_remove(ent.entity_id)
                    removed += 1
        if removed:
            _LOGGER.info("Pruned %d stale entity registry entries", removed)
        return removed

    async def _save_cache_now(self) -> None:
        """Snapshot current state to storage."""
        cached = CachedPanelConfig(
            max_zones=self.max_zones,
            max_partitions=self.max_partitions,
            max_users=self.max_users,
            zone_definitions={str(k): v for k, v in self.zone_definitions.items()},
            partition_enabled={str(k): v for k, v in self.partition_enabled.items()},
            zone_partition_assignment={
                str(k): list(v) for k, v in self.zone_partition_assignment.items()
            },
            zone_labels={str(k): v for k, v in self.zone_labels.items()},
            partition_labels={str(k): v for k, v in self.partition_labels.items()},
            zone_attributes_functional={
                str(k): v for k, v in self.zone_attributes_functional.items()
            },
            zone_attributes_physical={
                str(k): v for k, v in self.zone_attributes_physical.items()
            },
            zone_event_reporting={
                str(k): v for k, v in self.zone_event_reporting.items()
            },
            pgm_types={
                str(k): v for k, v in self.pgm_types.items()
            },
            pgm_partition_assignment={
                str(k): v for k, v in self.pgm_partition_assignment.items()
            },
        )
        cached.stamp()
        self.panel_config_fetched_at = cached.fetched_at
        try:
            await self._store.async_save(cached)
            _LOGGER.debug("Panel config cache saved")
        except Exception:  # pragma: no cover (best-effort)
            _LOGGER.exception("Failed to save panel config cache")

    # ---- accept / connection event loop ----

    async def _accept_loop(self) -> None:
        assert self._server is not None
        try:
            async for conn in self._server.connections():
                self._spawn(
                    self._handle_connection(conn),
                    name=f"dsc-conn-{conn.peer_addr[0]}",
                )
        except asyncio.CancelledError:
            return
        except Exception:  # pragma: no cover (defensive)
            _LOGGER.exception("DSC accept loop crashed")

    async def _handle_connection(self, conn: ITv2Connection) -> None:
        self._update_connection_state(CONN_STATE_HANDSHAKING)
        try:
            async for event in conn.events():
                self._dispatch_event(conn, event)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover (defensive)
            _LOGGER.exception("DSC connection handler crashed")
        finally:
            if self._active_conn is conn:
                self._active_conn = None
                self._update_connection_state(CONN_STATE_DISCONNECTED)

    @callback
    def _dispatch_event(self, conn: ITv2Connection, event: Any) -> None:
        if isinstance(event, Connected):
            self._active_conn = conn
            self.panel_session_id = event.session_id
            self.panel_firmware = (
                f"{event.panel_firmware_major}.{event.panel_firmware_minor:02d}"
            )
            # Wipe stale per-session state (zone open/closed, partition
            # status, capabilities: these will be re-pushed by the panel
            # within seconds). DO NOT wipe installer-mode-derived data
            # (zone_definitions, partition_enabled, labels, has_panel_config);
            # that's loaded from cache and survives reconnects.
            self.zones.clear()
            self.partitions.clear()
            self.max_zones = 0
            self.max_partitions = 0
            self.max_users = 0
            self._update_connection_state(CONN_STATE_CONNECTED)
            # A clean handshake means whatever credential issue was
            # previously surfaced (if any) is resolved.
            clear_auth_failed_issue(self.hass, self.entry_id)
            # Spawn the heartbeat keepalive...
            self._spawn(
                self._heartbeat_loop(conn),
                name=f"dsc-heartbeat-{conn.peer_addr[0]}",
            )
            # ...then proactively ask the panel for its current state so
            # entities populate immediately rather than waiting for the
            # ~60s natural broadcast cycle.
            self._spawn(
                self._initial_state_pull(conn),
                name=f"dsc-pull-{conn.peer_addr[0]}",
            )
            return
        if isinstance(event, Closed):
            self._update_connection_state(CONN_STATE_DISCONNECTED)
            return
        if isinstance(event, Failed):
            self._record_failure(event.reason)
            self._update_connection_state(CONN_STATE_FAILED)
            # If the failure wording matches an authentication problem
            # (decryption failure or panel-side rejection), surface a
            # Repairs issue so the user sees "needs attention" in the UI
            # instead of just a quiet "failed" sensor state.
            if is_auth_failure_reason(event.reason):
                set_auth_failed_issue(self.hass, self.entry_id, event.reason)
                # Also kick off HA's first-class reauthentication flow so
                # the user gets a "Reconfigure required" prompt directly
                # on the integration card (not just under Repairs).
                entry = self.hass.config_entries.async_get_entry(self.entry_id)
                start_reauth = getattr(entry, "async_start_reauth", None) if entry else None
                if start_reauth is not None:
                    start_reauth(self.hass, context={"reason": event.reason})
            return
        if isinstance(event, Notification):
            self._handle_notification(event)

    @callback
    def _handle_notification(self, event: Notification) -> None:
        msg = event.decoded
        # MultipleMessagePacket: expand and dispatch each sub-message
        # recursively so they're processed exactly as if the panel had
        # sent them individually.
        if isinstance(msg, MultipleMessagePacket):
            for sub in msg.messages:
                sub_event = Notification(
                    command=(
                        sub.command if isinstance(sub, DefaultMessage)
                        else getattr(sub, "COMMAND", 0)
                    ),
                    command_name=type(sub).__name__,
                    body=b"",
                    decoded=sub,
                )
                self._handle_notification(sub_event)
            return
        if isinstance(msg, ConnectionSystemCapabilities):
            self.max_zones = msg.max_zones
            self.max_partitions = msg.max_partitions
            self.max_users = msg.max_users
            _LOGGER.info(
                "Panel capabilities: %d zones, %d partitions, %d users",
                msg.max_zones, msg.max_partitions, msg.max_users,
            )
            async_dispatcher_send(
                self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
            )
            return
        if isinstance(msg, ModuleZoneStatus):
            self._update_zones(msg); return
        if isinstance(msg, NotificationLifestyleZoneStatus):
            # The Neo panel pushes zone open/close transitions via this
            # message in real time. Status: 0 = restored (closed),
            # 1 = open, 0xFF = unknown.
            zone = self._touch_zone(msg.zone_number)
            old_open = zone.is_open
            if msg.status == 1:
                zone.is_open = True
            elif msg.status == 0:
                zone.is_open = False
            self._send_zone_signal(zone.number)
            if zone.is_open != old_open:
                self._fire_event(EVENT_ZONE_CHANGE, {
                    "zone": zone.number,
                    "label": zone.label,
                    "is_open": zone.is_open,
                })
            return
        if isinstance(msg, SingleZoneBypassStatus):
            zone = self._touch_zone(msg.zone_number)
            zone.is_bypassed = bool(msg.bypass_status)
            self._send_zone_signal(zone.number)
            return
        if isinstance(msg, ModulePartitionStatus):
            self._update_partition_status(msg); return
        if isinstance(msg, NotificationPartitionReadyStatus):
            part = self._touch_partition(msg.partition_number)
            # ReadyToArm = 1, ReadyToForceArm = 2; anything else = not ready
            part.is_ready = msg.status in (1, 2)
            self._send_partition_signal(part.number)
            return
        if isinstance(msg, NotificationPartitionTroubleStatus):
            part = self._touch_partition(msg.partition)
            part.is_trouble = bool(msg.value)
            self._send_partition_signal(part.number); return
        if isinstance(msg, ModuleStatusSystemTroubleStatusNew):
            self._update_system_trouble(msg); return
        if isinstance(msg, NotificationPartitionBypassStatus):
            part = self._touch_partition(msg.partition)
            part.is_bypassed = bool(msg.value)
            self._send_partition_signal(part.number); return
        if isinstance(msg, NotificationPartitionAlarmMemory):
            part = self._touch_partition(msg.partition)
            part.is_alarm_in_memory = bool(msg.value)
            self._send_partition_signal(part.number); return
        if isinstance(msg, NotificationArmDisarm):
            self._handle_arm_disarm(msg); return
        if isinstance(msg, NotificationExitDelay):
            part = self._touch_partition(msg.partition)
            part.is_exit_delay = bool(msg.delay_flags & 0x80)
            part.exit_delay_seconds = msg.duration_in_seconds
            self._send_partition_signal(part.number); return
        if isinstance(msg, NotificationEntryDelay):
            part = self._touch_partition(msg.partition)
            part.is_entry_delay = bool(msg.delay_flags & 0x80)
            part.entry_delay_seconds = msg.duration_in_seconds
            self._send_partition_signal(part.number); return
        if isinstance(msg, NotificationLabelText):
            self._update_labels(msg); return
        if isinstance(msg, SectionReadResponse):
            self._handle_section_read_response(msg); return
        if isinstance(msg, ResponseTimeAndDate):
            self.panel_datetime = msg.date_time
            async_dispatcher_send(
                self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
            )
            return
        if isinstance(msg, NotificationDateTimeBroadcast):
            # The panel pushes its current date/time periodically. Cache
            # it so the diagnostic panel-time sensor stays fresh.
            dt_value = getattr(msg, "date_time", None)
            if dt_value is not None:
                self.panel_datetime = dt_value
                async_dispatcher_send(
                    self.hass, f"{SIGNAL_CONNECTION_UPDATE}_{self.entry_id}",
                )
            return
        # Known-but-uninteresting noise: silently absorb so we don't
        # spam the log with "Unhandled" warnings.
        if isinstance(
            msg,
            NotificationChimeBroadcast
            | NotificationBufferOverload
            | ProgrammingLeadInOut
            | CommandError,
        ):
            return
        # Command_Response from installer-mode interactions arrives as
        # an inbound notification too: log at debug, don't spam.
        if isinstance(msg, CommandResponse):
            _LOGGER.debug(
                "CommandResponse cmd_seq=%d code=%d",
                msg.command_sequence, msg.response_code,
            )
            return
        # Anything that reaches here is an inbound message we have no
        # handler for. Log at INFO with the wire command id and raw
        # payload so users diagnosing missing features (e.g. firmware-
        # specific trouble messages) can copy the hex out of their log.
        # ``DefaultMessage`` carries the actual command id + payload
        # captured before decoding; for everything else fall back to
        # the dispatcher event metadata.
        if isinstance(msg, DefaultMessage):
            _LOGGER.info(
                "Unhandled message cmd=0x%04X (%d bytes): %s%s",
                msg.command,
                len(msg.data),
                msg.data.hex(" "),
                f"  [decode error: {msg.deserialization_error}]"
                if msg.deserialization_error else "",
            )
            return
        _LOGGER.info(
            "Unhandled notification: %s (cmd=0x%04X, %d bytes): %s",
            event.command_name, event.command, len(event.body),
            event.body.hex(" "),
        )

    @callback
    def _update_system_trouble(
        self, msg: ModuleStatusSystemTroubleStatusNew,
    ) -> None:
        """Merge a system-trouble snapshot into ``self.trouble`` and fire events.

        Each byte in ``msg.group_bytes`` is the bitmap for the
        corresponding 1-based trouble group (Trouble 01..Trouble 12,
        plus any panel-specific tail bytes which we preserve under
        higher group IDs for diagnostics).
        """
        new_groups: dict[int, int] = {
            i + 1: b for i, b in enumerate(msg.group_bytes)
        }
        # Log the raw snapshot every time the panel sends one (DEBUG, so
        # it's quiet in normal operation). Critical for diagnosing
        # firmware-specific cases where the keypad's [*][2] menu shows a
        # trouble that none of our per-group entities reflect.
        _LOGGER.debug(
            "System trouble snapshot: %s",
            " ".join(f"{i + 1:02d}=0x{b:02X}" for i, b in enumerate(msg.group_bytes)),
        )
        old_groups = self.trouble.groups
        # Detect per-group transitions so automations can listen for
        # exactly the trouble that changed without diffing themselves.
        changes: list[tuple[int, int, int]] = []
        all_ids = set(old_groups) | set(new_groups)
        for gid in sorted(all_ids):
            old = old_groups.get(gid, 0)
            new = new_groups.get(gid, 0)
            if old != new:
                changes.append((gid, old, new))
        if not changes:
            return
        self.trouble = TroubleState(groups=new_groups)
        for gid, old, new in changes:
            label = TROUBLE_GROUP_LABELS.get(gid, f"Trouble {gid:02d}")
            self._fire_event(EVENT_TROUBLE_CHANGE, {
                "group": gid,
                "label": label,
                "active": bool(new),
                "old_bitmap": old,
                "new_bitmap": new,
            })
            _LOGGER.info(
                "Trouble group %d (%s) %s (0x%02X -> 0x%02X)",
                gid, label, "ACTIVE" if new else "RESTORED", old, new,
            )
        self._send_trouble_signal()

        # Update HA Repairs issues so actionable troubles get a
        # user-facing prompt (cellular trouble, AC loss, battery, ...).
        active_groups = {gid for gid, val in new_groups.items() if val != 0}
        sync_trouble_issues(self.hass, self.entry_id, active_groups)

    @callback
    def _handle_section_read_response(self, msg: SectionReadResponse) -> None:
        """Stash zone-definition / partition-enable bytes by section address.

        Section [001][N]         -> zone N's type byte (0 = NullZone = disabled)
        Section [200][1]         -> enabled-partition bitmap (LSB = partition 1)
        Section [200+P][byteIdx] -> partition P's zone-assignment bitmap chunk
                                    (8 zones per byte, MSB-first; byteIdx is
                                    1-based).
        """
        if not msg.section_address or not msg.section_data:
            return
        primary = msg.section_address[0]
        index = msg.section_address[1] if len(msg.section_address) > 1 else 0
        # First byte of section_data is the value for this item
        value = msg.section_data[0]
        if primary == 1 and index > 0:
            self.zone_definitions[index] = value
            _LOGGER.debug("Section [001][%d] = 0x%02x (zone definition)", index, value)
        elif primary == 7 and index > 0:
            # PGM-to-partition assignment for PGM `index`. Values 1-8 are
            # the assigned partition; 0 means unassigned. Only meaningful
            # for PGM types that support multi-partition operation.
            self.pgm_partition_assignment[index] = value
            _LOGGER.debug(
                "Section [007][%d] = 0x%02x (PGM %d partition assignment)",
                index, value, index,
            )
        elif primary == 9 and index > 0:
            # PGM type code for PGM `index`. 100 = Null PGM (unassigned).
            self.pgm_types[index] = value
            _LOGGER.debug(
                "Section [009][%d] = 0x%02x (PGM %d type code)",
                index, value, index,
            )
        elif primary == 2 and index > 0:
            # Two bytes per zone: byte 0 = functional flags, byte 1 = physical flags.
            self.zone_attributes_functional[index] = value
            if len(msg.section_data) > 1:
                self.zone_attributes_physical[index] = msg.section_data[1]
            _LOGGER.debug(
                "Section [002][%d] = 0x%02x/0x%02x (zone attributes func/phys)",
                index, value,
                self.zone_attributes_physical.get(index, 0),
            )
        elif primary == 307 and index > 0:
            self.zone_event_reporting[index] = value
            _LOGGER.debug(
                "Section [307][%d] = 0x%02x (zone event reporting)", index, value,
            )
        elif primary == 200 and index > 0:
            partition_count = min(self.max_partitions or 8, 8)
            self.partition_enabled = {
                partition: bool(value & (1 << (partition - 1)))
                for partition in range(1, partition_count + 1)
            }
            _LOGGER.debug(
                "Section [200][%d] = 0x%02x (enabled partitions=%s)",
                index, value,
                [p for p, enabled in self.partition_enabled.items() if enabled],
            )
        elif 201 <= primary <= 208 and index > 0:
            partition_no = primary - 200
            # byteIdx is 1-based in the section address; bit 0x80 maps to
            # the first zone in that group of 8.
            existing = set(self.zone_partition_assignment.get(partition_no, []))
            base_zone = (index - 1) * 8 + 1
            for bit in range(8):
                zone_no = base_zone + bit
                if zone_no > self.max_zones and self.max_zones:
                    break
                if value & (0x80 >> bit):
                    existing.add(zone_no)
            self.zone_partition_assignment[partition_no] = sorted(existing)
            _LOGGER.debug(
                "Section [%d][%d] = 0x%02x (partition %d zones=%s)",
                primary, index, value, partition_no,
                self.zone_partition_assignment[partition_no],
            )

    @callback
    def _handle_arm_disarm(self, msg: NotificationArmDisarm) -> None:
        part = self._touch_partition(msg.partition)
        part.last_user_id = msg.user_id
        if msg.arm_mode == int(ArmingMode.Disarm):
            part.is_armed = False
            part.arming_mode = int(ArmingMode.Disarm)
            part.is_exit_delay = False
            part.exit_delay_seconds = 0
            part.is_entry_delay = False
            part.entry_delay_seconds = 0
            self._fire_event(EVENT_DISARM, {
                "partition": msg.partition,
                "user_id": msg.user_id,
                "method": msg.method,
            })
            self._fire_event(EVENT_USER_ACTIVITY, {
                "partition": msg.partition,
                "user_id": msg.user_id,
                "action": "disarm",
                "method": msg.method,
            })
        else:
            part.is_armed = True
            part.arming_mode = msg.arm_mode
            self._fire_event(EVENT_ARM, {
                "partition": msg.partition,
                "user_id": msg.user_id,
                "arm_mode": msg.arm_mode,
                "method": msg.method,
            })
            self._fire_event(EVENT_USER_ACTIVITY, {
                "partition": msg.partition,
                "user_id": msg.user_id,
                "action": "arm",
                "arm_mode": msg.arm_mode,
                "method": msg.method,
            })
        self._send_partition_signal(part.number)

    @callback
    def _update_zones(self, msg: ModuleZoneStatus) -> None:
        size = max(1, msg.status_size_in_bytes)
        for i in range(msg.zone_count):
            zone_no = msg.zone_start + i
            offset = i * size
            if offset >= len(msg.zone_status_bytes):
                break
            raw = msg.zone_status_bytes[offset]
            zone = self._touch_zone(zone_no)
            old_open = zone.is_open
            zone.raw_status = raw
            zone.is_open        = bool(raw & ZoneStatusFlags.Open)
            zone.is_tamper      = bool(raw & ZoneStatusFlags.Tamper)
            zone.is_trouble     = bool(raw & ZoneStatusFlags.Fault)
            zone.is_low_battery = bool(raw & ZoneStatusFlags.LowBattery)
            zone.is_alarm       = bool(raw & ZoneStatusFlags.Alarm)
            zone.is_bypassed    = bool(raw & ZoneStatusFlags.Bypass)
            self._send_zone_signal(zone_no)
            if zone.is_open != old_open:
                self._fire_event(EVENT_ZONE_CHANGE, {
                    "zone": zone_no,
                    "label": zone.label,
                    "is_open": zone.is_open,
                })

    @callback
    def _update_partition_status(self, msg: ModulePartitionStatus) -> None:
        part = self._touch_partition(msg.partition)
        s1 = msg.partition_status[0] if msg.partition_status else 0
        s2 = msg.partition_status[1] if len(msg.partition_status) > 1 else 0
        s3 = msg.partition_status[2] if len(msg.partition_status) > 2 else 0
        old_in_alarm = part.is_in_alarm
        old_fire_pre_alert = part.is_fire_pre_alert
        part.raw_status1 = s1
        part.raw_status2 = s2
        part.raw_status3 = s3

        # Status1 - the low 5 bits encode the arm-state value (mixed
        # numeric/flag enum per NeoHub PartitionStatus1); the top three
        # bits are independent timing flags.
        mode_value = s1 & 0x1F
        # Per NeoHub: 0x01 Armed, 0x03 StayArmed, 0x05 ArmedAway,
        # 0x09 NightArmed, 0x11 ArmedWithNoEntryDelay, 0x10 WalkTest
        armed_values = {0x01, 0x03, 0x05, 0x09, 0x11}
        part.is_armed = mode_value in armed_values
        # Mode-value -> ArmingMode mapping (best-effort)
        mode_map = {
            0x01: ArmingMode.AwayArm,
            0x03: ArmingMode.StayArm,
            0x05: ArmingMode.AwayArm,
            0x09: ArmingMode.NightArm,
            0x11: ArmingMode.ArmWithNoEntryDelay,
        }
        if part.is_armed:
            part.arming_mode = int(mode_map.get(mode_value, ArmingMode.AwayArm))
        else:
            part.arming_mode = int(ArmingMode.Disarm)
        part.is_ready       = mode_value in (0x02, 0x04)
        part.is_walk_test   = (mode_value == 0x10)
        # Top three bits of status1 are timing flags
        part.is_exit_delay  = bool(s1 & 0x20)
        part.is_entry_delay = bool(s1 & 0x40)
        part.is_quick_exit  = bool(s1 & 0x80)

        # Status2 - pure flag byte (NeoHub PartitionStatus2)
        part.is_in_alarm           = bool(s2 & 0x01)
        part.is_trouble            = bool(s2 & 0x02)
        part.is_bypassed           = bool(s2 & 0x04)
        part.is_programming_mode   = bool(s2 & 0x08)
        part.is_alarm_in_memory    = bool(s2 & 0x10)
        part.is_door_chime_enabled = bool(s2 & 0x20)
        part.is_audible_bell       = bool(s2 & 0x40)

        # Status3 - fire pre-alert window (verify time before full alarm)
        part.is_fire_pre_alert = bool(s3 & 0x01)

        # Fire vs burglary inference: if any fire zone in this
        # partition currently has its alarm bit set, this is a fire
        # alarm; otherwise treat as burglary. Best-effort - the
        # ModulePartitionStatus byte doesn't distinguish.
        if part.is_in_alarm:
            fire_active = any(
                z.is_alarm and is_fire_zone(self.zone_definitions.get(zn, 0))
                for zn, z in self.zones.items()
            )
            part.is_fire_alarm = fire_active
            part.is_burglary_alarm = not fire_active
        else:
            part.is_fire_alarm = False
            part.is_burglary_alarm = False

        # Fire transitions
        if part.is_in_alarm != old_in_alarm:
            self._fire_event(EVENT_PARTITION_ALARM, {
                "partition": msg.partition,
                "active": part.is_in_alarm,
                "kind": (
                    "fire" if part.is_fire_alarm
                    else "burglary" if part.is_burglary_alarm
                    else "restored"
                ),
            })
        if part.is_fire_pre_alert != old_fire_pre_alert:
            self._fire_event(EVENT_FIRE_PRE_ALERT, {
                "partition": msg.partition,
                "active": part.is_fire_pre_alert,
            })
        self._send_partition_signal(part.number)

    @callback
    def _update_labels(self, msg: NotificationLabelText) -> None:
        # LabelCollection: Zone = 0xD1, Partition = 0xD3, User = 0xD9
        changed = False
        for i, label in enumerate(msg.labels):
            number = msg.start + i
            if number > msg.end:
                break
            label_str = (label or "").rstrip()
            if msg.collection == 0xD1:    # Zone
                changed |= self.zone_labels.get(number) != label_str
                self.zone_labels[number] = label_str
                if number in self.zones:
                    self.zones[number].label = label_str
                    self._send_zone_signal(number)
            elif msg.collection == 0xD3:  # Partition
                changed |= self.partition_labels.get(number) != label_str
                self.partition_labels[number] = label_str
                if number in self.partitions:
                    self.partitions[number].label = label_str
                    self._send_partition_signal(number)
            # User labels ignored: no entity surface for them yet
        if changed and self.has_panel_config:
            self._schedule_label_cache_save()
