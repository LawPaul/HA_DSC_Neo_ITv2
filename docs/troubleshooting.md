# Troubleshooting

## Quick diagnosis

Check `sensor.<panel>_connection_state` first. It is available even when the
panel is offline.

| Value | Meaning |
|---|---|
| `disconnected` | Listener is up, panel has not dialled in |
| `handshaking` | Panel connected, handshake in progress |
| `connected` | Session established, notifications flowing |
| `failed` | Handshake or session failed. Check the log and Repairs |

## Symptom -> cause -> fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `timeout_no_panel` during setup | Panel isn't dialling Home Assistant | Verify the receiver IP/port in `[851]`, confirm *Integration over Ethernet* is enabled, force a reconnect by exiting/re-entering installer programming |
| `port_in_use` | Another process owns the port | `ss -ltnp \| grep 3072`; pick another port and update `[851]` to match |
| `bind_denied` | Port < 1024, or container lacks the capability | Use a port >= 1024 |
| `host_unavailable` | The bind address isn't on this machine | Use `0.0.0.0` or a real interface IP |
| Stuck in `handshaking`, then `failed` | Wrong Type 2 key or Type 1 ID/code | Re-check `[851][700]` (Type 2) or `[851][422]`+`[851][423]` (Type 1). A Type 1 check-byte mismatch is logged explicitly |
| Reauth prompt keeps returning | Credentials still wrong | Compare against the keypad values character by character; the Type 2 key is 32 hex characters, not 32 decimal digits |
| Connects, then drops every few minutes | Poll interval / another integration session already in use | Check `[851][429]`; make sure DLS, Alarm.com or another ITv2 client isn't holding the same session slot |
| Zones missing | No installer code, or those zones have definition `0` | Add the installer code in Configure, then press **Reload panel config** |
| Zones exist but are `unavailable` | Zone disabled in `[001][N]`, or panel disconnected | Reload panel config; check connection state |
| Wrong zone device classes | No installer code, so classes are inferred from labels | Add the installer code and reload panel config |
| Zone labels are blank or wrong | Labels not programmed at the keypad | Program them in `[000][001]`, then reload panel config |
| Arming does nothing | No user PIN stored and no code supplied | Enter the code in the UI prompt, or store a user PIN in Configure |
| `Invalid access code` in the log | Stored user PIN is not valid on the panel | Verify the PIN at the keypad; check its partition permissions |
| Bypass switch missing for a zone | 24-hour or keyswitch zone | Expected: the panel does not allow bypassing those |
| Command-output button missing | Installer-code discovery found no matching PGM | Program the PGM in `[009]`, then reload panel config |
| Command output does nothing | PGM programmed but not wired, or wrong partition | Check `[009][N]` type and `[007][N]` partition |
| Keypads show "Installer Programming" briefly | A panel-config read is running | Expected. Only happens on demand or first setup |
| Panel time drifts | Neo panels drift | Automate `dsc_neo_itv2.set_date_time` nightly |
| Entities orphaned after re-programming | Stale registry entries | Reload panel config. Pruning runs automatically afterwards |

## Repairs issues

| Issue | Meaning | Action |
|---|---|---|
| Service required | Panel `[*][2]` service trouble | Check the keypad trouble menu |
| Battery | Backup battery low/absent | Replace the panel battery |
| AC loss | Mains power lost | Check the transformer and breaker |
| Communications | Panel cannot reach its receiver | Check network/cellular path |
| Authentication failed | Wrong integration credentials | Follow the reauth prompt |

Trouble issues clear themselves when the panel clears the underlying trouble.

## Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.dsc_neo_itv2: debug
    custom_components.dsc_neo_itv2.coordinator: debug
    custom_components.dsc_neo_itv2.dsc_itv2: debug
```

Reload the `logger` integration (or restart) to apply. The `dsc_itv2` logger
emits per-packet detail and will fill your log quickly on a busy panel. Turn it
off once you have what you need.

## Diagnostics

**Settings > Devices & Services > DSC PowerSeries Neo > overflow menu > Download
diagnostics.**

Includes the config entry (redacted), connection state, capabilities, all zone
and partition state, and the trouble bitmap. The following are replaced with
`**REDACTED**`: `integration_id`, `type1_key`, `type2_key`, `user_pin`,
`installer_code`, `session_id`, plus anything keyed `access_code`,
`type1_access_code`, `type2_access_code_hex`.

`last_user_id` is not redacted. It is a user slot number (1-1000), not a
credential.

## Network checks

```bash
# Is the listener up?
ss -ltnp | grep 3072

# Is the panel reaching it?
sudo tcpdump -ni any tcp port 3072
```

If you run Home Assistant in Docker, the port must be published
(`-p 3072:3072`) or the container must use host networking. In a VM, check the
hypervisor's NAT/bridge configuration.

## Security

- **Never paste unredacted diagnostics, packet captures, `[851][422]`,
  `[851][700]`, your installer code or user PINs into a GitHub issue.** Anyone
  with the Integration ID and access key can arm, disarm and reprogram your
  panel.
- Do not port-forward the ITv2 port from the internet. If you need remote
  access, use a VPN.
- Redaction covers credentials, not your zone and partition labels. Those are
  read from the panel and included in the diagnostics download verbatim,
  because the device-class inference depends on them. They describe the layout
  of your home, so edit them out before attaching the file to a public issue if
  that bothers you.
- On a multi-homed host, bind to the specific LAN interface rather than
  `0.0.0.0`.
- Change the factory defaults: Type 2 key `12345678...`, Type 1 access code
  `12345678`, installer code `5555`. All are published values.
- Storing a user PIN in the config entry means anyone who can call
  `alarm_control_panel.alarm_disarm` in Home Assistant can disarm your alarm.
  Leave it blank if you want HA to require the code every time.

## Reporting a bug

Include:

1. Home Assistant version, integration version, installation method
2. Panel model and communicator model + firmware (from the firmware sensor)
3. What you expected vs. what happened
4. Relevant `custom_components.dsc_neo_itv2` debug log lines, scrubbed
5. A redacted diagnostics download

<https://github.com/LawPaul/HA_DSC_Neo_ITv2/issues>
