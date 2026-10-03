# Services

Every service takes `entry_id`: the config entry to act on. In the UI this is a
config-entry picker; in YAML it is the entry id string. Field schemas live in
[`services.yaml`](../custom_components/dsc_neo_itv2/services.yaml).

Services raise `HomeAssistantError` when the panel rejects the command or the
connection is down, and `ServiceValidationError` when `entry_id` doesn't
resolve to a loaded DSC Neo entry.

## `dsc_neo_itv2.bypass_zone`

Bypass or unbypass a single zone.

The integration enters the normal user bypass menu (`[*][1]`) with the stored
user PIN before writing the bypass, then exits the menu. Configure a user PIN
when the panel requires one for bypassing. This does not enter installer
programming. Concurrent bypass requests are serialized so their menu sessions
do not interleave. Bypass state remains panel-reported rather than optimistic;
check the zone's bypass switch for confirmation.

| Field | Required | Type | Notes |
|---|---|---|---|
| `entry_id` | yes | config entry | |
| `partition` | yes | 1-8 | |
| `zone` | yes | 1-128 | |
| `bypass` | yes | bool | `true` bypasses, `false` clears |

```yaml
action: dsc_neo_itv2.bypass_zone
data:
  entry_id: 01HE5J3MTQB5R5MJDXXXXXX
  partition: 1
  zone: 5
  bypass: true
```

24-hour and keyswitch zones cannot be bypassed by the panel; the equivalent
switch entity is not created for them.

## `dsc_neo_itv2.quick_exit`

Re-arm with a temporary exit delay so someone can step out without disarming.

| Field | Required | Type | Default |
|---|---|---|---|
| `entry_id` | yes | config entry | |
| `partition` | no | 1-8 | `1` |

## `dsc_neo_itv2.activate_fap`

Trigger a Fire, Auxiliary (medical) or Panic (police) alarm. Equivalent to
holding the F/A/P keys on a keypad.

| Field | Required | Type | Default |
|---|---|---|---|
| `entry_id` | yes | config entry | |
| `partition` | no | 1-8 | `1` |
| `type` | yes | `fire` \| `auxiliary` \| `panic` | |

> **This reports to your central station if monitoring is configured.** Do not
> wire it to a test automation.

## `dsc_neo_itv2.command_output`

Fire a panel programmable output. Equivalent to `[*][7][N]`.

| Field | Required | Type | Default |
|---|---|---|---|
| `entry_id` | yes | config entry | |
| `partition` | no | 1-8 | `1` |
| `output` | yes | 1-4 | |

The stored user PIN is sent automatically. What physically happens depends on
how the PGM is programmed in `[009]`: door strike, garage pulse, chime, etc.
Output 2 has a dedicated `sensor_reset` service which is usually clearer.

## `dsc_neo_itv2.sensor_reset`

Reset latched alarm sensors (2-wire smoke detectors, glass-break). Equivalent to
`[*][7][2]`. Requires PGM2 to be programmed as "Sensor Reset" (type `103`) in
section `[009]`.

| Field | Required | Type | Default |
|---|---|---|---|
| `entry_id` | yes | config entry | |
| `partition` | no | 1-8 | `1` |

## `dsc_neo_itv2.set_date_time`

Write the panel clock. Omit `datetime` to use "now".

| Field | Required | Type |
|---|---|---|
| `entry_id` | yes | config entry |
| `datetime` | no | datetime |

Useful as a nightly automation: Neo panels drift and a wrong clock produces
wrong event-log timestamps.

```yaml
alias: Sync alarm panel clock
triggers:
  - trigger: time
    at: "03:15:00"
actions:
  - action: dsc_neo_itv2.set_date_time
    data:
      entry_id: 01HE5J3MTQB5R5MJDXXXXXX
mode: single
```

The panel's packed date/time format only encodes years 2000-2063.

## `dsc_neo_itv2.reload_panel_config`

Re-read installer-only programming (zone definitions, partition enables, labels,
PGM types) and refresh the cache.

| Field | Required | Type |
|---|---|---|
| `entry_id` | yes | config entry |

Raises `HomeAssistantError` if no installer code is configured for the entry.

> Briefly enters installer programming mode. Keypads will display *Installer
> Programming* for a few seconds. Run it after changing zone or partition
> programming so Home Assistant learns about the change without a reinstall.

## Automation examples

### Announce which zones are open when arming fails

```yaml
alias: Alarm not ready
triggers:
  - trigger: state
    entity_id: binary_sensor.dsc_neo_partition_1_ready
    to: "off"
actions:
  - action: notify.mobile_app
    data:
      message: >
        Cannot arm. Open zones:
        {{ state_attr('alarm_control_panel.dsc_neo_partition_1', 'open_zones')
           | map(attribute='label') | join(', ') }}
```

### Bypass the garage zone when arming at night

```yaml
alias: Night arm with garage bypassed
triggers:
  - trigger: state
    entity_id: alarm_control_panel.dsc_neo_partition_1
    to: armed_night
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.garage_door_bypass
```

### Reset smoke detectors after a fire alarm clears

```yaml
alias: Reset smokes after fire alarm
triggers:
  - trigger: state
    entity_id: sensor.dsc_neo_partition_1_fire_status
    from: alarm
    to: ok
actions:
  - delay: "00:00:30"
  - action: dsc_neo_itv2.sensor_reset
    data:
      entry_id: 01HE5J3MTQB5R5MJDXXXXXX
      partition: 1
```

### React to panel troubles

```yaml
alias: Alarm panel trouble
triggers:
  - trigger: event
    event_type: dsc_neo_itv2_trouble_change
actions:
  - action: persistent_notification.create
    data:
      title: Alarm panel trouble
      message: "{{ trigger.event.data }}"
```
