# Configuration

All configuration is via the UI. There is **no YAML configuration**. The
integration is config-entry only.

## Config-entry data

| Key | Const | Type | Default | Purpose |
|---|---|---|---|---|
| `host` | `CONF_HOST` | str | `0.0.0.0` | Interface the ITv2 listener binds to |
| `port` | `CONF_PORT` | int | `3072` | TCP port the panel dials |
| `integration_id` | `CONF_INTEGRATION_ID` | str | *(learned)* | 12-digit ID from `[851][422]`; also the entry's unique id |
| `type2_key` | `CONF_TYPE2_KEY` | str | `12345678...` (32 hex) | Type 2 access key from `[851][700]` |
| `type1_key` | `CONF_TYPE1_KEY` | str | *(empty)* | Type 1 8-digit access code from `[851][423]` |
| `user_pin` | `CONF_USER_PIN` | str | *(empty)* | Stored arm/disarm code |
| `installer_code` | `CONF_INSTALLER_CODE` | str | `5555` | Enables installer-mode panel-config reads |

## Validation rules

Enforced by `_validate_secrets()` in `config_flow.py` on every flow that accepts
codes (initial confirm, options, reauth):

| Field | Rule | Error key |
|---|---|---|
| Type 2 key | exactly 32 hexadecimal characters, if non-empty | `invalid_type2_key` |
| Type 1 key | exactly 8 digits, if non-empty | `invalid_type1_key` |
| User PIN | 4-6 digits, if non-empty | `invalid_user_pin` |
| Installer code | 4-6 digits, if non-empty | `invalid_installer_code` |

Bind errors from step 1 use `port_in_use`, `host_unavailable`, `bind_denied`,
`bind_failed`. Probe errors from step 2 use `timeout_no_panel`, `bind_failed`,
`probe_failed`.

## Flows

### Initial setup: `user` -> `wait` -> `confirm`

Described in [installation.md](installation.md#4-add-the-integration-in-home-assistant).

### Options: `init`

**Settings > Devices & Services > DSC PowerSeries Neo > Configure**

Re-collects the four code fields. Host and port are not editable here. Use
Reconfigure for those. Saving triggers a reload of the config entry via the
entry update listener.

### Reconfigure: `async_step_reconfigure`

Open the entry's overflow menu and choose **Reconfigure**. This re-runs the full
`user` -> `wait` -> `confirm` sequence against the existing entry, so you can
move Home Assistant to a new IP or change the port.

Safety check: if the panel that dials in during the `wait` step reports a
different Integration ID from the one the entry already has, the flow aborts
with `reconfigure_panel_changed` rather than silently re-pointing the entry at
another panel.

### Reauth: `reauth_confirm`

Started automatically by the coordinator when the panel rejects our
credentials. `repairs.is_auth_failure_reason()` classifies a disconnect reason
as an auth failure when it contains any of:

- `decryption failed`
- `panel rejected opensession`
- `panel rejected our requestaccess`

A Repairs issue (`<entry_id>_auth_failed`, severity **error**) is raised
alongside the reauth flow. Both clear once a session establishes successfully.

## Stored panel configuration

When an installer code is configured, the coordinator briefly enters installer
programming mode and reads:

| Section | Cached as | Used for |
|---|---|---|
| `[001][N]` | `zone_definitions` | Zone device classes; hiding disabled zones (definition `0`) |
| `[002][N]` | `zone_attributes_functional` / `zone_attributes_physical` | Bypassable / chime / EOL attributes |
| `[007][N]` | `pgm_partition_assignment` | Which partition a PGM belongs to |
| `[009][N]` | `pgm_types` | Discovering and naming command-output buttons |
| `[201]`-`[208]` | `zone_partition_assignment` | Grouping zones under partitions |
| `[307][N]` | `zone_event_reporting` | Diagnostic attributes |

The result is persisted to `.storage/dsc_neo_itv2.<entry_id>.json` (store
version 1) by `PanelConfigStore`, so entities come back correctly after a
restart without re-entering programming mode.

Refresh it on demand with the **Reload panel config** button or the
`dsc_neo_itv2.reload_panel_config` service after changing panel programming.

> Entering installer programming makes keypads display "Installer Programming"
> for a few seconds and, on some configurations, suspends alarm reporting for
> that window. The integration only does this on demand, never on a timer.

## Without an installer code

Everything still works, with reduced fidelity:

- Zone device classes fall back to keyword inference from the zone label
  (`zone_labels.infer_device_class()`), then to `opening`.
- Every zone number up to the panel's reported capability is treated as
  potentially enabled, so unused zones may appear as entities.
- All four command-output buttons remain available with generic labels because
  the integration cannot inspect which PGMs are programmed.
- The **Reload panel config** button is unavailable.

## Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.dsc_neo_itv2: debug
    custom_components.dsc_neo_itv2.dsc_itv2: debug
```

The protocol logger is very chatty on a live panel. Enable it only while
diagnosing, and remember packet dumps may contain key material. Scrub before
sharing.
