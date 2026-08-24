# DSC PowerSeries Neo (ITv2) for Home Assistant

[![CI][ci-badge]][ci]
[![Validate][validate-badge]][validate]
[![hacs][hacs-badge]][hacs]
[![License: GPL v3][license-badge]][license]

Native, local push integration for DSC PowerSeries Neo alarm panels over the
ITv2 integration protocol. No Envisalink adapter and no cloud account required.

Home Assistant acts as the integration receiver: it listens on TCP, the panel's
TL280/TL2803G/3G2080 communicator dials in, and zone, partition, alarm and
trouble changes arrive as encrypted push events.

```
DSC HS2xxx panel                     Home Assistant
  \- TL280 communicator  --TCP-->    dsc_neo_itv2 listener :3072
     (ITv2, AES-128)                   |- alarm_control_panel.<partition>
                                       |- binary_sensor.<zone>  (push)
                                       |- switch.<zone>_bypass
                                       |- sensor.<partition>_open_zones
                                       \- Repairs issues for AC loss, battery, comms
```

## Features

The panel initiates the connection and streams state changes, so the integration
is `iot_class: local_push` with no polling anywhere. Traffic is encrypted; both
the ITv2 Type 1 and Type 2 AES-128 handshakes are implemented from scratch in
Python.

Setup needs little input. The config flow waits for the panel to dial in and
reads the Integration ID and encryption type from the handshake itself. Supply
the installer code as well and it reads zone definitions, partition
assignments, zone attributes and PGM types from the panel, so entities are
created with the correct device classes and disabled zones are skipped.

Entities cover alarm panels, zone binary sensors, bypass switches,
command-output buttons, per-partition status sensors and trouble sensors. AC
loss, low battery, service required and communications failures raise Repairs
issues, and there is a redacted diagnostics download for bug reports. Codes and
the listen address can be changed later through the reauth and reconfigure
flows without removing the integration. See
[known limitations](docs/known-limitations.md) for what is not implemented, in
particular the virtual keypad, access-code management and event-log retrieval.

## Supported hardware

| Component | Status |
|---|---|
| HS2016 / HS2032 / HS2064 / HS2128 panels | Supported |
| TL280(R)(E), TL2803G, 3G2080, LE2080 communicators | Supported (ITv2) |
| PowerSeries **Neo** only | The older PowerSeries (PC1616/1832/1864) speaks IT-100/serial, not supported |
| Up to 128 zones, 8 partitions | Supported |

> The integration has been developed and live-tested against an HS2032 with a
> TL280 communicator. Other Neo panels use the same ITv2 protocol and are
> expected to work. Reports welcome.

## Requirements

- Home Assistant 2024.11 or newer
- A DSC PowerSeries Neo panel with a TL280-family communicator on your LAN
- Installer access to the panel keypad (to enable the integration session and
  read the Integration ID)
- A static IP / reserved DHCP lease for the Home Assistant host
- `pycryptodome` (installed automatically by HA)

## Quick start

### 1. Install

Under HACS, add this repository as a custom repository (category
"Integration"), install it, then restart Home Assistant.

To install by hand, copy `custom_components/dsc_neo_itv2/` into your HA
`config/custom_components/` directory and restart.

### 2. Program the panel

At the keypad, in installer programming (`[*][8]` + installer code):

| Section | What to set |
|---|---|
| `[851][422]` | Integration ID. Read-only. Write the 12 digits down |
| `[851][425]` | Integration options. Set bits 3, 4, 5 (`--345---`) |
| `[851][426]` | Polling / notifications. Set bit 3 only (`--3-----`) |
| `[851][428]` | Integration server IP = the Home Assistant host IP |
| `[851][429]` | Integration server port, **in hex**. `0C00` = 3072 |
| `[851][700]` | Integration access key (Type 2), 32 hex chars |

Those are the group 1 sections. The panel has four integration groups, so if a
monitoring service already occupies one, use another. See
[docs/installation.md](docs/installation.md) for the full, annotated
walk-through.

### 3. Add the integration

**Settings > Devices & Services > Add Integration > DSC PowerSeries Neo (ITv2)**

1. Confirm the bind host (`0.0.0.0`) and port (`3072`). The flow pre-flight-binds
   the socket and reports whether the port is already in use.
2. Wait for the panel to dial in. The flow reads the Integration ID and
   encryption type from the handshake. Trigger it from the keypad if it doesn't
   happen on its own.
3. Enter the Type 2 access key, and optionally the Type 1 code, a user PIN for
   arm/disarm, and the installer code to enable panel-config reads.

### 4. Use it

Entities appear under a single **DSC Neo** device, and can be used in
automations, dashboards and scripts like any other Home Assistant entity.

## Entities

| Platform | Entity | Per |
|---|---|---|
| `alarm_control_panel` | Partition arm/disarm with away / home / night | partition |
| `binary_sensor` | Zone state (device class inferred from zone definition, then label) | zone |
| `binary_sensor` | Panel connectivity | panel |
| `binary_sensor` | Trouble (aggregate) + one per trouble group | panel |
| `binary_sensor` | Alarm, fire alarm, burglary alarm, fire pre-alert, alarm in memory, trouble, exit delay, entry delay, ready, chime, programming mode | partition |
| `sensor` | Communicator firmware, connection state, panel time | panel |
| `sensor` | Open-zone count, bypassed-zone count | partition |
| `sensor` | Fire status, burglary status, system status (enums) | partition |
| `switch` | Zone bypass (bypassable zones only) | zone |
| `button` | Command outputs, filtered and labelled from PGM programming when available | partition |
| `button` | Reload panel config | panel |

Full reference: [docs/entities.md](docs/entities.md).

## Services

| Service | What it does |
|---|---|
| `dsc_neo_itv2.bypass_zone` | Bypass / unbypass a single zone |
| `dsc_neo_itv2.quick_exit` | Trigger Quick Exit on a partition |
| `dsc_neo_itv2.activate_fap` | Fire / Auxiliary / Panic keypad alarm |
| `dsc_neo_itv2.command_output` | Fire PGM command output 1-4 (`[*][7][N]`) |
| `dsc_neo_itv2.sensor_reset` | Reset latched smoke / glass-break sensors (`[*][7][2]`) |
| `dsc_neo_itv2.set_date_time` | Write the panel clock |
| `dsc_neo_itv2.reload_panel_config` | Re-read installer programming from the panel |

Full reference with examples: [docs/services.md](docs/services.md).

## Documentation

| Doc | Contents |
|---|---|
| [Installation](docs/installation.md) | Panel programming sections, HACS + manual install, first run |
| [Configuration](docs/configuration.md) | Every config-flow field, options, reauth, reconfigure |
| [Entities](docs/entities.md) | Complete entity reference with unique-id patterns and attributes |
| [Services](docs/services.md) | Service schemas and automation examples |
| [Architecture](docs/architecture.md) | How the integration is wired: server -> codec -> coordinator -> entities |
| [Protocol](docs/protocol.md) | The ITv2 wire format: TLink framing, CRC, AES handshake, message layer |
| [Troubleshooting](docs/troubleshooting.md) | Symptom -> cause -> fix table, debug logging, diagnostics |
| [Known limitations](docs/known-limitations.md) | What this integration deliberately does not do |
| [Development](docs/development.md) | Local setup, running the test suite, contributing |

## Security notes

This integration handles alarm-system credentials. Please read
[docs/troubleshooting.md#security](docs/troubleshooting.md#security) before
filing issues.

- The Type 1/Type 2 access keys, integration ID, user PIN and installer code are
  stored in the Home Assistant config entry. They are redacted from diagnostics
  downloads and never logged.
- The integration binds a listening TCP socket. Bind to a specific interface
  rather than `0.0.0.0` if your HA host is multi-homed, and do not expose port
  3072 to the internet.
- A stored user PIN removes the arm/disarm code prompt in the UI. Leave it blank
  if you want Home Assistant to require the code on every action.
- Never paste unredacted diagnostics, packet captures, or your Integration ID
  or access keys into a public issue.

## Attribution and licence

Licensed under GPL-3.0-or-later. See [LICENSE](LICENSE).

The `dsc_itv2/` protocol layer was written in Python for this project. It draws
on Brian Humlicek's [NeoHub](https://github.com/BrianHumlicek/NeoHub) and
[DSC-TLink](https://github.com/BrianHumlicek/DSC-TLink), both GPL-3.0, and on
analysis of the ITv2 protocol as implemented by DSC's own tooling, undertaken
solely to make the panel interoperate with Home Assistant. Parts of the framing
and handshake logic derive from GPL-3.0 sources, so this repository is GPL-3.0.

No DSC or Johnson Controls source code, binaries or keys are included or
redistributed here. What this layer encodes is the wire format itself: command
numbers, field layouts and framing rules. Individual modules cite where each
detail came from.

DSC, PowerSeries and Neo are trademarks of Johnson Controls / Tyco. This project
is not affiliated with, endorsed by, or supported by them. See
[custom_components/dsc_neo_itv2/brand/README.md](custom_components/dsc_neo_itv2/brand/README.md)
for the brand-asset trademark notice.

## Disclaimer

This software talks to a life-safety device. It is provided without warranty
of any kind. Do not rely on it as your only means of monitoring or controlling
your alarm system, and do not use it to replace a professionally monitored
central station connection.

[ci]: https://github.com/LawPaul/HA_DSC_Neo_ITv2/actions/workflows/ci.yaml
[ci-badge]: https://github.com/LawPaul/HA_DSC_Neo_ITv2/actions/workflows/ci.yaml/badge.svg
[validate]: https://github.com/LawPaul/HA_DSC_Neo_ITv2/actions/workflows/validate.yaml
[validate-badge]: https://github.com/LawPaul/HA_DSC_Neo_ITv2/actions/workflows/validate.yaml/badge.svg
[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[license]: LICENSE
[license-badge]: https://img.shields.io/badge/License-GPLv3-blue.svg
