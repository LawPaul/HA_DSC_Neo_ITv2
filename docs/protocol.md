# The DSC ITv2 protocol

Reference for the wire format implemented in
[`custom_components/dsc_neo_itv2/dsc_itv2/`](../custom_components/dsc_neo_itv2/dsc_itv2/).

This implementation is informed by the GPL-3.0
[NeoHub](https://github.com/BrianHumlicek/NeoHub) project and published
descriptions of DSC's integration protocol. It is documentation of an
interoperability interface, not a redistribution of vendor software.

## Layer 1: TLink transport framing

`tlink_framing.py`

```
<stuffed header> 0x7E <stuffed payload> 0x7F
```

Byte stuffing (applied to both regions):

| Raw | On the wire |
|---|---|
| `0x7D` | `0x7D 0x00` |
| `0x7E` | `0x7D 0x01` |
| `0x7F` | `0x7D 0x02` |

Because stuffing removes `0x7E`/`0x7F` from the interior, the first `0x7F` in a
stream buffer always terminates exactly one packet. There is no start-of-frame
marker.

For ITv2, the header is the panel's 12-byte ASCII **Integration ID**. The panel
sends it on the first packet; the client reuses the same value for everything it
sends afterwards.

API: `stuff()`, `unstuff()`, `pack(header, payload)`,
`extract_packet(buffer) -> (TLinkPacket | None, leftover)`,
`parse_all(buffer) -> (list[TLinkPacket], leftover)`.

`unstuff()` raises `ValueError` on a bare `0x7E`/`0x7F`, a truncated escape, or
an escape byte other than `0x00`/`0x01`/`0x02`.

## Layer 2: Encryption

`encryption.py`: AES-128-ECB, `PaddingMode.None`, zero-padding to the 16-byte
block. **Two independent keys per session**, one per direction.

### Type 2

Programming: `[851][700..703]`, a 32-hex-character access key.

```
access_key      = bytes.fromhex(code)              # the AES key itself
initializer     = 16 random bytes                  # per direction
session_key     = AES_encrypt(access_key, initializer)
```

Each side generates its own initializer, sends it in `RequestAccess`, and
derives the peer's session key from the received initializer. `configure_inbound()`
returns the local initializer; `configure_outbound(remote)` consumes the peer's.

### Type 1

Programming: `[851][422]` (Integration ID) + `[851][423]` (8-digit access code).

Key derivation for both values:

```
key = bytes.fromhex(value[:8] * 4)     # first 8 chars repeated 4x
```

The 48-byte initializer is `check_bytes(16) || ciphertext(32)`:

```
random32     = 32 random bytes
check_bytes  = random32[0::2]          # even indices -> 16 bytes
session_key  = random32[1::2]          # odd  indices -> 16 bytes
ciphertext   = AES_encrypt(access_code_key, random32)
```

The receiver decrypts the 32-byte half with the Integration-ID-derived key and
verifies `plaintext[0::2] == check_bytes`. A mismatch raises `ValueError` and
means the Integration ID or access code is wrong. This is the signal that
triggers a reauth flow.

Constants for reference: `NEOHUB_DEFAULT_TYPE2_HEX =
"12345678123456781234567812345678"`, `NEOHUB_DEFAULT_TYPE1_ACCESS_CODE =
"12345678"`. **Change both on any real installation.**

## Layer 3: ITv2 inner framing

`itv2_framing.py`, inside the (decrypted) TLink payload:

```
<length prefix> <packet bytes> <CRC-16 BE>
```

- `length = len(packet) + 2` (the `+2` covers the CRC).
- `length <= 0x7F` -> one byte.
- `length > 0x7F` -> two bytes: `(length >> 8) | 0x80`, then `length & 0xFF`.
  Maximum `0x7FFF`.
- CRC is computed over **the length bytes plus the packet bytes**.

The length field is what lets the receiver discard AES zero-padding.

`remove_framing()` raises `ValueError` on empty input, a truncated prefix,
`length < 2`, a prefix that overruns the buffer, or a CRC mismatch.

## Layer 4: CRC-16-CCITT

`crc.py`

| Parameter | Value |
|---|---|
| Polynomial | `0x1021` |
| Init | `0xFFFF` |
| Bit order | MSB-first |
| Final XOR | none |
| Stored as | 2 bytes, big-endian |

Also known as CRC-16-CCITT (False) / CRC-16-IBM-3740. `crc16_ccitt(b"123456789")`
== `0x29B1`.

API: `crc16_ccitt(data, length=None)`, `append_crc(bytearray)`,
`verify_crc(buffer)`.

## Layer 5: Packet header

`packet.py`

```
[0]     sender_sequence     our increment-per-send byte
[1]     receiver_sequence   echo of the peer's last sender_sequence
[2..3]  command             uint16, big-endian
[4..]   body
```

**Special case:** a packet of exactly 2 bytes is a `Simple_Ack` (command `0`)
carrying only the sequence pair: no command word, no body. `build_packet()`
emits this form automatically for `Simple_Ack` with an empty body.

`parse_packet()` raises `ValueError` for fewer than 2 bytes, and for 3 bytes
(ambiguous: neither an ack nor a full header).

For command-class messages the **first body byte is the command sequence**.
Notifications and acks do not carry it.

## Layer 6: Wire primitives

`serdes.py`. All readers are `(data, offset) -> (value, new_offset)`; all
writers append to a `bytearray`.

### Compact integer

```
[length:1][length significant bytes, big-endian]
```

Zero encodes as `01 00`: the length is never `0`. Negative values raise
`ValueError`.

### BCD strings

Two hex nibbles per byte. Accepts `0-9` and `A-F`. Variants: fixed length,
leading-length, and leading-length arrays (one shared byte width, N items).
Reads return uppercase.

### Unicode strings

UTF-16 **big-endian**, with a 1- or 2-byte leading length counted in bytes.
Fixed arrays are prefixed with a compact-int byte width and null-padded;
trailing nulls are stripped on read.

### Packed date/time (4 bytes)

| Bits | Field | Range |
|---|---|---|
| 31-27 | hour | 0-23 |
| 26-21 | minute | 0-59 |
| 20-15 | second | 0-59 |
| 14-9 | year - 2000 | 0-63 |
| 8-5 | month | 1-12 |
| 4-0 | day | 1-31 |

Years outside 2000-2063 raise `ValueError`.

### Variable-length prefix

Same 1-or-2-byte, high-bit-flag scheme as ITv2 framing. Used by
`MultipleMessagePacket` to delimit sub-messages. Max `0x7FFF`.

## Layer 7: Messages

`commands.py` defines `Command(IntEnum)` with 170 members. `name_of(value)`
returns the enum name, or `Unknown(0x....)` for values not in the enum.

`ACK_COMMANDS = {Simple_Ack (0), Command_Error (1281), Command_Response (1282)}`.

Selected commands:

| Value | Name |
|---|---|
| `0` | `Simple_Ack` |
| `562` | `Notification_Arming_Disarming` |
| `569` | `Notification_Partition_Ready_Status` |
| `575` | `Notification_Partition_Trouble_Status` |
| `1281` | `Command_Error` |
| `1282` | `Command_Response` |
| `1536` | `Connection_Poll` |
| `1546` | `Connection_Open_Session` |
| `1547` | `Connection_End_Session` |
| `1549` | `Connection_Software_Version` |
| `1550` | `Connection_Request_Access` |
| `1555` | `Connection_System_Capabilities` |
| `1796` | `Configuration_Enter` |
| `1825` | `Configuration_Installers_Section_Read` |
| `1826` | `Configuration_Installers_Section_Write` |
| `1857` | `Configuration_Write_Time_and_Date` |
| `1866` | `Configuration_Write_Single_Zone_Bypass_Write` |
| `2048` | `ModuleStatus_Command_Request` |
| `2065` | `ModuleStatus_Zone_Status` |
| `2066` | `ModuleStatus_Partition_Status` |
| `2082` | `ModuleStatus_System_Trouble_Status_New` |
| `2304` | `ModuleControl_Partition_Arm_Control` |
| `2305` | `ModuleControl_Partition_Disarm_Control` |
| `2312` | `ModuleControl_Activate_FAP` |
| `18225` | `Response_Time_and_Date` |

`messages.py` maps each command to a frozen dataclass with a class-level
`COMMAND`, registered in two tables:

```python
ENCODERS: dict[type, Callable[[Any], bytes]]
DECODERS: dict[int, Callable[[bytes], Any]]

encode_message(msg)            -> (command_id: int, body: bytes)
decode_message(command, body)  -> message instance | None
```

`encode_message` raises `KeyError` for an unregistered class. `decode_message`
returns `None` for an unknown command; the session layer then wraps it in a
`DefaultMessage` so unknown traffic is preserved rather than fatal.

## Status bitfields

`enums.py`

### `ZoneStatusFlags` (per-zone byte in `ModuleStatus_Zone_Status`)

| Bit | Meaning |
|---|---|
| `0x01` | Open |
| `0x02` | Tamper |
| `0x04` | Fault |
| `0x08` | Low battery |
| `0x10` | Delinquency |
| `0x20` | Alarm |
| `0x40` | Alarm memory |
| `0x80` | Bypass |

### `PartitionStatus1`

Mixed: the low nibble is a state value, the high bits are independent flags.

| Value | Meaning |
|---|---|
| `0x01` | Armed |
| `0x02` | Ready to arm |
| `0x03` | Stay armed |
| `0x04` | Ready to force arm |
| `0x05` | Armed away |
| `0x08` | Not ready to arm |
| `0x09` | Night armed |
| `0x10` | Walk test mode |
| `0x11` | Armed with no entry delay |
| `0x20` | Exit delay in progress |
| `0x40` | Entry delay in progress |
| `0x80` | Quick exit in progress |

### `PartitionStatus2` (true flags)

`0x01` in alarm, `0x02` troubles present, `0x04` zones bypassed,
`0x08` in programming mode, `0x10` alarms in memory, `0x20` door chime,
`0x40` audible bell/siren, `0x80` audible keypad buzzer.

### Error codes

`CommandResponseCode` (`0` = success) and `ITv2NackCode` cover the common
rejections: `InvalidAccessCode (17)`, `AccessCodeRequired (18)`,
`SystemBusy (19)`, `InvalidPartition (20)`, `FunctionNotAvailable (23)`,
`CommandTimeout (25)`, `InvalidProgrammingMode (16)`.

## Installer programming sections used

Read via `Configuration_Installers_Section_Read` after
`Configuration_Enter(programming_mode=InstallersProgramming)`.

| Section | Contents |
|---|---|
| `[001][N]` | Zone `N` definition byte (`0` = disabled) |
| `[002][N]` | Zone `N` functional (byte 0) and physical (byte 1) attributes |
| `[007][N]` | PGM `N` partition assignment |
| `[009][N]` | PGM `N` type code (`100` = null/unassigned) |
| `[200][1]` | Enabled-partition bitmap; bit `N-1` enables partition `N` (LSB-first) |
| `[201]`-`[208]` | Zone -> partition assignment for partitions 1-8 |
| `[307][N]` | Zone `N` event-reporting options |

PGM probing stops at `MAX_PGM_TO_PROBE = 32` (4 onboard + 3x HSM2208 + HSM2204
on a fully loaded HS2128); the protocol maximum is 164.
