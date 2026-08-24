"""Button platform: one-shot panel actions.

Buttons exposed:

  * **Reload panel config** (config category): re-read installer-mode
    sections so HA learns about zone/partition/label/PGM changes made
    at the keypad.
  * **Available command outputs** (per partition): fires the matching
    keypad ``[*][7][N]`` action. The button is labelled with the human-readable
    PGM type name pulled from the panel (e.g. ``Sensor reset``,
    ``Courtesy pulse``) when the panel-config read has captured PGM
    types ([009]) and partition assignments ([007]). Outputs with no
    matching programmed PGM are not created.

All buttons live under the single panel device so the UI keeps them
together.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    COMMAND_OUTPUT_PGM_TYPES,
    CONF_USER_PIN,
    CONN_STATE_CONNECTED,
    PGM_TYPE_NAMES,
    SIGNAL_CONNECTION_UPDATE,
)
from .coordinator import DscNeoCoordinator

_LOGGER = logging.getLogger(__name__)

# Command-output slot numbers exposed as keypad ``[*][7][N]`` triggers.
_COMMAND_OUTPUTS = tuple(sorted(COMMAND_OUTPUT_PGM_TYPES))

# Sensor-reset (PGM type 103) is the only command-output type that
# warrants a non-generic icon since users instinctively look for a
# smoke-detector icon when resetting latched smokes.
_SENSOR_RESET_PGM_TYPE = 103


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DscNeoCoordinator = entry.runtime_data
    added: set[tuple[str, int, int]] = set()

    @callback
    def _add_for_known_partitions() -> None:
        new: list[ButtonEntity] = []
        for partition_no in sorted(coordinator.partitions):
            if not coordinator.is_partition_enabled(partition_no):
                continue
            for output_no in _COMMAND_OUTPUTS:
                if not coordinator.command_output_pgms(partition_no, output_no):
                    continue
                key = ("command_output", partition_no, output_no)
                if key not in added:
                    added.add(key)
                    new.append(
                        DscNeoCommandOutputButton(
                            coordinator, entry, partition_no, output_no,
                        )
                    )
        if new:
            async_add_entities(new)

    async_add_entities([DscNeoReloadConfigButton(coordinator, entry)])
    _add_for_known_partitions()

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{SIGNAL_CONNECTION_UPDATE}_{entry.entry_id}",
            _add_for_known_partitions,
        )
    )


# ---------------------------------------------------------------------------
# Reload config
# ---------------------------------------------------------------------------


class DscNeoReloadConfigButton(ButtonEntity):
    """Press to re-read installer-mode panel config (zones/partitions/labels)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "reload_panel_config"

    def __init__(
        self, coordinator: DscNeoCoordinator, entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reload_config"
        self._attr_device_info = coordinator.panel_device_info

    @property
    def available(self) -> bool:
        return bool(self._coordinator.installer_code)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_fetched_at": self._coordinator.panel_config_fetched_at,
            "enabled_zones": sum(
                1 for d in self._coordinator.zone_definitions.values() if d != 0
            ),
            "enabled_partitions": sum(
                1 for e in self._coordinator.partition_enabled.values() if e
            ),
        }

    async def async_press(self) -> None:
        ok = await self._coordinator.async_reload_panel_config()
        if not ok:
            raise HomeAssistantError(
                "Cannot reload panel config - no installer code configured"
            )


# ---------------------------------------------------------------------------
# Command-output buttons (sensor reset + generic)
# ---------------------------------------------------------------------------


class _PartitionedCommandOutputButton(ButtonEntity):
    """Shared base for buttons that fire a command-output on a partition."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DscNeoCoordinator,
        entry: ConfigEntry,
        partition_number: int,
        output_number: int,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._partition_number = partition_number
        self._output_number = output_number
        self._attr_device_info = coordinator.panel_device_info

    @property
    def available(self) -> bool:
        return self._coordinator.connection_state == CONN_STATE_CONNECTED

    async def async_press(self) -> None:
        stored_pin = self._entry.data.get(CONF_USER_PIN, "") or ""
        try:
            await self._coordinator.async_command_output(
                output_number=self._output_number,
                partition=self._partition_number,
                access_code=stored_pin,
            )
        except RuntimeError as exc:
            raise HomeAssistantError(str(exc)) from exc

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_CONNECTION_UPDATE}_{self._entry.entry_id}",
                self.async_write_ha_state,
            )
        )


class DscNeoCommandOutputButton(_PartitionedCommandOutputButton):
    """Keypad ``[*][7][N]`` command-output trigger, labelled from panel programming.

    Looks up which PGMs in the panel are programmed to fire on this
    output slot (via ``coordinator.pgm_types`` from section [009] and
    ``coordinator.pgm_partition_assignment`` from section [007]) and
    uses the PGM type name from the installer manual to label the
    button. Slots without a matching programmed PGM are filtered by the
    platform and no entity is created for them.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: DscNeoCoordinator,
        entry: ConfigEntry,
        partition_number: int,
        output_number: int,
    ) -> None:
        super().__init__(coordinator, entry, partition_number, output_number)
        self._attr_unique_id = (
            f"{entry.entry_id}_partition_{partition_number}"
            f"_command_output_{output_number}"
        )

        # Resolve matching PGMs: ones whose type fires on this output
        # slot AND whose partition assignment matches this partition.
        # Missing assignment defaults to partition 1 (the panel's
        # factory default when [007] hasn't been programmed).
        matches = coordinator.command_output_pgms(partition_number, output_number)

        # Build label and icon from the matches.
        if matches:
            # Single match is the common case; use its type name. For
            # multiple matches (rare: e.g. two PGMs both wired as
            # Command Output 1) fall back to the generic slot name and
            # surface the list in attributes.
            if len(matches) == 1:
                type_code = matches[0][1]
                base = PGM_TYPE_NAMES.get(
                    type_code, f"Command output {output_number}",
                )
            else:
                base = f"Command output {output_number}"
            # Sensor reset gets a smoke-detector icon.
            if any(tc == _SENSOR_RESET_PGM_TYPE for _, tc in matches):
                self._attr_translation_key = "sensor_reset"
            else:
                self._attr_translation_key = "command_output"
            self._matches = matches
        else:
            # Defensive fallback for direct construction. The platform never
            # adds this entity when no programmed PGM matches the slot.
            base = f"Command output {output_number}"
            self._attr_translation_key = "command_output"
            self._matches = []

        self._attr_name = (
            base if partition_number == 1
            else f"{base} (partition {partition_number})"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "partition": self._partition_number,
            "output": self._output_number,
            "matching_pgms": [
                {
                    "pgm": pgm_no,
                    "type_code": type_code,
                    "type_name": PGM_TYPE_NAMES.get(type_code, "unknown"),
                }
                for pgm_no, type_code in self._matches
            ],
        }
