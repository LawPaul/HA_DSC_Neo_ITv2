# Known limitations

## Protocol / panel

- PowerSeries Neo only. Classic PowerSeries panels (PC1616/1832/1864) speak
  the IT-100 serial protocol, not ITv2. They are not supported and will not be.
- One integration session per slot. The Neo supports four integration
  sessions. If DLS, Alarm.com or another ITv2 client already holds the slot
  you've configured, connections will fight. Give Home Assistant its own
  session.
- No discovery. The panel dials out to a fixed receiver IP; there is nothing
  to discover on the network. DHCP/SSDP/zeroconf discovery is not implemented.
- Home Assistant must have a stable IP. A DHCP lease change silently breaks
  the connection until you re-program `[851]`.
- The virtual keypad is not implemented. The message types exist in the protocol
  layer as opaque stubs, but LCD text, keypad LEDs and key injection are not
  surfaced as entities.
- Access-code management is not exposed. Read/write of user codes exists in
  the protocol layer but is deliberately not surfaced as a service.
- Event log retrieval is not implemented.
- Panel date/time only spans 2000-2063, a hard limit of the 4-byte packed
  format.

## Panel-config reads

- Reading zone definitions, partition assignment and PGM types requires
  briefly entering installer programming mode. Keypads display "Installer
  Programming" for a few seconds, and some configurations suspend reporting
  during that window. This is why it only runs on demand and on first setup,
  never on a schedule.
- Without an installer code the integration falls back to label-keyword device
  class inference and cannot hide disabled zones.
- PGM probing stops at PGM 32 (`MAX_PGM_TO_PROBE`). The protocol maximum is 164,
  but 32 covers a fully-loaded HS2128 and keeps the read to a few seconds.
- Zone-follower PGM types (201-216) are not enumerated and won't produce
  meaningful command-output button labels.

## Entities

- 24-hour and keyswitch zones get no bypass switch. The panel refuses to
  bypass them.
- Command-output buttons require PGM programming data from an installer-code
  panel read for automatic filtering. Without an installer code all four
  generic buttons remain enabled because availability cannot be inspected.
- `sensor.<panel>_panel_time` is disabled by default because it changes
  every minute and bloats the recorder.
- Partition sub-state binary sensors are mostly diagnostic-category and hidden
  from the default dashboard.

## Quality-scale gaps

Currently outstanding:

| Rule | Status |
|---|---|
| `brands` | Local `brand/` assets ship with the integration (HA 2026.3+); no `home-assistant/brands` PR submitted |
| `discovery` / `discovery-update-info` | Not applicable / not implemented |
| `entity-disabled-by-default` | Some noisy diagnostic entities could still be disabled by default |
| `exception-translations` | `HomeAssistantError` messages are inline English strings |
| `strict-typing` | Modern hints throughout, but no full `mypy --strict` pass yet |

## Safety

This integration talks to a **life-safety device**.

- It is not a replacement for professional central-station monitoring.
- Home Assistant restarts, network outages and integration bugs will all cause
  the connection to drop. The panel keeps protecting the premises regardless,
  but Home Assistant's view of it will be stale or unavailable.
- `activate_fap` triggers a real alarm that reports to the central station.
- Do not build automations that disarm the system based on untrusted input.
