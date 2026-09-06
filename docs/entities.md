# Entity reference

All entities attach to a single device: **DSC Neo `<integration-id>`**
(identifiers `{(dsc_neo_itv2, <entry_id>)}`, manufacturer `DSC`, model `Neo`,
`sw_version` = communicator firmware).

Every entity sets `_attr_has_entity_name = True` and `should_poll = False`;
state arrives via dispatcher signals from the coordinator, never by polling.

Unless noted with **available offline**, an entity is `unavailable` whenever the
panel connection state is not `connected`.

## `alarm_control_panel`

One per enabled partition.

| Property | Value |
|---|---|
| Unique id | `{entry_id}_partition_{n}` |
| Supported features | `ARM_AWAY`, `ARM_HOME`, `ARM_NIGHT` |
| `code_arm_required` | `False` when a user PIN is stored, otherwise `True` |
| `code_format` | `None` when a user PIN is stored, otherwise `NUMBER` |

### State mapping

Evaluated in priority order:

| Condition | State |
|---|---|
| partition in alarm | `triggered` |
| entry delay running | `pending` |
| exit delay running | `arming` |
| armed, mode is StayArm, StayArmNoEntryDelay or InstantStayArm | `armed_home` |
| armed, mode is NightArm or NightArmNoEntryDelay | `armed_night` |
| armed, any other mode | `armed_away` |
| otherwise | `disarmed` |

### Attributes

`is_walk_test`, `is_audible_bell`, `is_quick_exit`, `is_bypassed`,
`exit_delay_seconds`, `entry_delay_seconds`, `last_user_id`,
`open_zones` (list of `{zone, label}`), `bypassed_zones` (list of
`{zone, label}`), `raw_status1` / `raw_status2` / `raw_status3` (hex).

## `binary_sensor`

### Zone

One per enabled zone.

| Property | Value |
|---|---|
| Unique id | `{entry_id}_zone_{n}` |
| `is_on` | zone is open/faulted |
| Device class | zone definition -> label keyword -> `opening` |
| Available | zone enabled and panel connected |

Device class resolution (`_resolve_device_class`):

1. `zone_definitions.device_class_for_definition()`: maps the panel's zone
   definition byte (`[001][N]`) to `smoke`, `gas`, `carbon_monoxide`,
   `moisture`, `heat`, `cold`, `safety`, `tamper`, `problem` or `sound`.
2. `zone_labels.infer_device_class()`: longest-match keyword scan of the zone
   label (`door`, `window`, `motion`, `garage`, `smoke`, ...).
3. `opening`.

Attributes: `zone_number`, `label`, `zone_definition`, `zone_type`,
`is_fire_zone`, `is_24_hour_zone`, `is_tamper`, `is_trouble`, `is_low_battery`,
`is_alarm`, `is_bypassed`, `raw_status` (hex).

### Panel connection

| Property | Value |
|---|---|
| Unique id | `{entry_id}_connection` |
| Translation key | `panel_connection` |
| Device class | `connectivity` |
| Category | diagnostic |

Attributes: `connection_state`, `session_id`, `host`, `port`.

### Trouble (aggregate)

| Property | Value |
|---|---|
| Unique id | `{entry_id}_trouble_any` |
| Device class | `problem` |
| Category | diagnostic |

Attributes: `active_groups` (list of `{group, label, bitmap}`), `raw_groups`.

### Trouble group

One per trouble group reported by the panel.

| Property | Value |
|---|---|
| Unique id | `{entry_id}_trouble_group_{gg}` (2-digit, zero padded) |
| Name | `Trouble: <group label>` |
| Device class | `problem` |
| Category | diagnostic |

Attributes: `group_id`, `group_label`, `bitmap`, `bits_set`.

### Partition sub-states

Eleven per partition. Unique id `{entry_id}_partition_{n}_{kind}`; name is the
label alone for partition 1 and `<label> (partition n)` otherwise.

| Kind | Device class | Category |
|---|---|---|
| `alarm` | `safety` | - |
| `fire_alarm` | `smoke` | - |
| `burglary_alarm` | `safety` | - |
| `fire_pre_alert` | `smoke` | - |
| `alarm_in_memory` | `problem` | diagnostic |
| `trouble` | `problem` | diagnostic |
| `exit_delay` | `running` | diagnostic |
| `entry_delay` | `running` | diagnostic |
| `ready` | - | diagnostic |
| `chime_enabled` | - | diagnostic |
| `programming_mode` | - | diagnostic |

## `sensor`

### Panel-level

| Entity | Unique id | Notes |
|---|---|---|
| Communicator firmware | `{entry_id}_firmware` | diagnostic, **available offline** |
| Connection state | `{entry_id}_connection_state` | `enum`: `disconnected`, `handshaking`, `connected`, `failed`. **available offline**. Attributes `host`, `port`, `session_id` |
| Panel time | `{entry_id}_panel_time` | `timestamp`, **disabled by default**, **available offline** |

### Per partition

| Entity | Unique id | Notes |
|---|---|---|
| Open zones | `{entry_id}_partition_{n}_open_zones` | measurement; attribute `open_zones` |
| Bypassed zones | `{entry_id}_partition_{n}_bypassed_zones` | measurement; attribute `bypassed_zones` |
| Fire status | `{entry_id}_partition_{n}_fire_status` | enum `ok` \| `trouble` \| `pre_alert` \| `alarm` |
| Burglary status | `{entry_id}_partition_{n}_burglary_status` | enum `disarmed` \| `not_ready` \| `ready` \| `exit_delay` \| `armed` \| `entry_delay` \| `alarm` |
| System status | `{entry_id}_partition_{n}_system_status` | enum `ok` \| `armed` \| `trouble` \| `alarm` |

Enum sensors evaluate highest-severity-first, so `alarm` always wins.

## `switch`

### Zone bypass

One per enabled zone that is neither a keyswitch nor a 24-hour zone
(24-hour zones cannot be bypassed by the panel).

| Property | Value |
|---|---|
| Unique id | `{entry_id}_zone_{n}_bypass` |
| Translation key | `zone_bypass` |
| Category | config |
| Name | `<zone label> bypass`, or `Zone <n> bypass` |
| `is_on` | zone is currently bypassed |

Attributes: `zone_number`, `label`.

Turning the switch on/off issues `Configuration_Write_Single_Zone_Bypass_Write`
for that partition + zone.

## `button`

### Command outputs

With installer-code data, one per command output that has a matching programmed
PGM on the enabled partition (`[*][7][1..4]`). Without an installer code, all
four generic controls are exposed.

| Property | Value |
|---|---|
| Unique id | `{entry_id}_partition_{n}_command_output_{k}` |
| Category | config |

The name is resolved from the panel's PGM programming: the integration looks up
which PGMs are programmed with a type code that responds to `[*][7][k]`
(`COMMAND_OUTPUT_PGM_TYPES` in `const.py`) and are also assigned to this
partition.

- Exactly one match -> the button is named after `PGM_TYPE_NAMES[type_code]`
  (e.g. "Sensor reset", "Command output 1").
- Multiple matches -> generic `Command output k`.
- No matches -> no entity is created for that output.

This filtering requires the installer-code panel read. Without an installer
code, all four generic command-output buttons remain enabled so users can still
trigger a known output manually.

Icon: `mdi:smoke-detector-variant` for Sensor Reset (PGM type 103), otherwise
`mdi:electric-switch`.

Attributes: `partition`, `output`, `matching_pgms` (list of
`{pgm, type_code, type_name}`).

### Reload panel config

| Property | Value |
|---|---|
| Unique id | `{entry_id}_reload_config` |
| Translation key | `reload_panel_config` |
| Category | config |
| Available | only when an installer code is configured |

Attributes: `last_fetched_at`, `enabled_zones`, `enabled_partitions`.

## Events

The integration fires these on the HA event bus:

| Event | Fired when |
|---|---|
| `dsc_neo_itv2_alarm` | any alarm condition |
| `dsc_neo_itv2_arm` | a partition is armed |
| `dsc_neo_itv2_disarm` | a partition is disarmed |
| `dsc_neo_itv2_zone_change` | a zone changes state |
| `dsc_neo_itv2_trouble_change` | a trouble group toggles |
| `dsc_neo_itv2_partition_alarm` | a partition enters/exits alarm |
| `dsc_neo_itv2_fire_pre_alert` | fire-alarm verification window opens |
| `dsc_neo_itv2_user_activity` | per-user keypad activity |

## Repairs issues

| Issue id | Trouble group | Severity |
|---|---|---|
| `{entry_id}_trouble_group_01` | Service required | warning |
| `{entry_id}_trouble_group_02` | Battery | warning |
| `{entry_id}_trouble_group_04` | AC loss | warning |
| `{entry_id}_trouble_group_11` | Communications | error |
| `{entry_id}_auth_failed` | - | error |

Issues are created and deleted idempotently as the panel's trouble bitmap
changes, and all are cleared on unload.
