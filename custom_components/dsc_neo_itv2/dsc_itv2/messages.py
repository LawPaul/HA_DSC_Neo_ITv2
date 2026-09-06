"""Hand-coded ITv2 message bodies.

Each message corresponds to one ITv2 command. Messages are
``@dataclass(frozen=True)`` types with module-level ``encode_*`` and
``decode_*`` functions registered in ``ENCODERS`` / ``DECODERS``.

Sources:
  - NeoHub ``TLink/ITv2/Messages/*.cs`` (GPL-3.0)
  - DLS5 ``Tyco.TSP.Communications.ITV2.*`` (decompiled, used for
    cross-validating wire layouts)

Wire-format conventions (per NeoHub ``BinarySerializer``):
  * **byte / u16 BE**: declared as ``byte`` / ``ushort`` in C#
  * **CompactInt**: ``[len:1][N MSB-significant bytes]``
  * **BCDString**: fixed N bytes, each byte = two hex/decimal nibbles
  * **LeadingLengthBCDString**: ``[byte-len:1][BCD bytes]``
  * **LeadingLengthBCDString[]**: ``[byte-len:1][BCD1]...``  (fills to EOB)
  * **UnicodeString**: ``[len:1or2][UTF-16BE bytes]``
  * **FixedLengthUnicodeStringArray**: ``[CompactInt: byte-width][UTF-16BE x N]``
  * **LeadingLengthArray**: ``[len:1or2][bytes]``
  * **FixedArray(N)**: exactly N bytes
  * **(unbounded byte[])**: consumes to end of buffer; must be last
  * **DateTime**: 4-byte packed (5b hour, 6b min, 6b sec, 6b year-2000, 4b month, 5b day)

For command-class messages (subclasses of ``CommandMessageBase``), the
first body byte is ``CommandSequence``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import datetime as _dt
from typing import Any

from . import serdes as _s
from .commands import Command
from .enums import (
    PartitionStatus1,
    PartitionStatus2,
    PartitionStatus3,
)

# ===========================================================================
# Empty / handshake-trivial messages
# ===========================================================================


@dataclass(frozen=True)
class SimpleAck:
    COMMAND = Command.Simple_Ack


def encode_simple_ack(_msg: SimpleAck = SimpleAck()) -> bytes:
    return b""


def decode_simple_ack(body: bytes) -> SimpleAck:
    if body:
        raise ValueError(f"SimpleAck expects empty body, got {len(body)} bytes")
    return SimpleAck()


@dataclass(frozen=True)
class ConnectionPoll:
    COMMAND = Command.Connection_Poll


def encode_connection_poll(_msg: ConnectionPoll = ConnectionPoll()) -> bytes:
    return b""


def decode_connection_poll(body: bytes) -> ConnectionPoll:
    if body:
        raise ValueError(f"ConnectionPoll expects empty body, got {len(body)} bytes")
    return ConnectionPoll()


@dataclass(frozen=True)
class ConnectionEndSession:
    COMMAND = Command.Connection_End_Session


def encode_connection_end_session(_msg: ConnectionEndSession = ConnectionEndSession()) -> bytes:
    return b""


def decode_connection_end_session(body: bytes) -> ConnectionEndSession:
    if body:
        raise ValueError(
            f"ConnectionEndSession expects empty body, got {len(body)} bytes"
        )
    return ConnectionEndSession()


# ===========================================================================
# Acks / responses
# ===========================================================================


@dataclass(frozen=True)
class CommandResponse:
    """[command_sequence:1][response_code:1]"""
    COMMAND = Command.Command_Response
    command_sequence: int
    response_code: int = 0  # see :class:`CommandResponseCode`


def encode_command_response(msg: CommandResponse) -> bytes:
    return bytes([msg.command_sequence & 0xFF, msg.response_code & 0xFF])


def decode_command_response(body: bytes) -> CommandResponse:
    if len(body) < 2:
        raise ValueError(f"CommandResponse body too short: {len(body)} bytes")
    return CommandResponse(command_sequence=body[0], response_code=body[1])


@dataclass(frozen=True)
class CommandError:
    """[nack_command:u16 BE][nack_code:1]"""
    COMMAND = Command.Command_Error
    nack_command: int   # the ITv2 command ID being NACKed
    nack_code: int      # see :class:`ITv2NackCode`


def encode_command_error(msg: CommandError) -> bytes:
    out = bytearray()
    _s.write_u16_be(out, msg.nack_command)
    _s.write_u8(out, msg.nack_code)
    return bytes(out)


def decode_command_error(body: bytes) -> CommandError:
    nack_command, o = _s.read_u16_be(body, 0)
    nack_code, _o = _s.read_u8(body, o)
    return CommandError(nack_command=nack_command, nack_code=nack_code)


# ===========================================================================
# Session lifecycle
# ===========================================================================


@dataclass(frozen=True)
class OpenSession:
    """[cmd_seq:1][dev_type:1][dev_id:2][fw_version:2][proto:u16 BE]
       [tx_buf:u16 BE][rx_buf:u16 BE][unknown:2][encryption_type:1][trailing_data:*]"""
    COMMAND = Command.Connection_Open_Session
    command_sequence: int
    device_type: int
    device_id: bytes
    firmware_version: bytes
    protocol_version: int
    tx_buffer_size: int
    rx_buffer_size: int
    unknown: bytes = b"\x00\x00"
    encryption_type: int = 0
    # Firmware 5.63 appends opaque bytes that must survive the handshake echo.
    trailing_data: bytes = b""

    @property
    def firmware_major(self) -> int:
        """Major firmware version (BCD-encoded).

        Byte 0 of FirmwareVersion is the major version expressed as BCD,
        e.g. byte ``0x05`` -> major version 5. For two-digit major
        versions the high nibble is the tens digit and the low nibble
        the ones digit.

        .. note::
           NeoHub historically computed
           ``(byte0 << 4) | (byte1 >> 4)`` which yields nonsense values
           like "86" for a TL280 actually running firmware 5.61. Real
           panels send the version in BCD; matching the keypad display
           and the sticker on the communicator.I m
        """
        if len(self.firmware_version) < 2:
            return 0
        b = self.firmware_version[0]
        return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)

    @property
    def firmware_minor(self) -> int:
        """Minor firmware version (BCD-encoded).

        Byte 1 of FirmwareVersion is the minor revision in BCD.
        For ``0x61`` that's tens=6, ones=1 -> minor version 61.
        """
        if len(self.firmware_version) < 2:
            return 0
        b = self.firmware_version[1]
        return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


OPEN_SESSION_BODY_SIZE: int = 15  # Minimum size, excluding firmware extensions.


def encode_open_session(msg: OpenSession) -> bytes:
    if len(msg.device_id) != 2:
        raise ValueError("device_id must be 2 bytes")
    if len(msg.firmware_version) != 2:
        raise ValueError("firmware_version must be 2 bytes")
    if len(msg.unknown) != 2:
        raise ValueError("unknown must be 2 bytes")
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_u8(out, msg.device_type)
    out.extend(msg.device_id)
    out.extend(msg.firmware_version)
    _s.write_u16_be(out, msg.protocol_version)
    _s.write_u16_be(out, msg.tx_buffer_size)
    _s.write_u16_be(out, msg.rx_buffer_size)
    out.extend(msg.unknown)
    _s.write_u8(out, msg.encryption_type)
    out.extend(msg.trailing_data)
    return bytes(out)


def decode_open_session(body: bytes) -> OpenSession:
    if len(body) < OPEN_SESSION_BODY_SIZE:
        raise ValueError(
            f"OpenSession body too short: {len(body)} (need {OPEN_SESSION_BODY_SIZE})"
        )
    cmd_seq = body[0]
    dev_type = body[1]
    dev_id = bytes(body[2:4])
    fw = bytes(body[4:6])
    proto = int.from_bytes(body[6:8], "big")
    tx = int.from_bytes(body[8:10], "big")
    rx = int.from_bytes(body[10:12], "big")
    unk = bytes(body[12:14])
    enc = body[14]
    return OpenSession(
        command_sequence=cmd_seq,
        device_type=dev_type,
        device_id=dev_id,
        firmware_version=fw,
        protocol_version=proto,
        tx_buffer_size=tx,
        rx_buffer_size=rx,
        unknown=unk,
        encryption_type=enc,
        trailing_data=bytes(body[OPEN_SESSION_BODY_SIZE:]),
    )


@dataclass(frozen=True)
class RequestAccess:
    """[cmd_seq:1][len:1][initializer bytes]"""
    COMMAND = Command.Connection_Request_Access
    command_sequence: int
    initializer: bytes = field(default_factory=bytes)


def encode_request_access(msg: RequestAccess) -> bytes:
    if len(msg.initializer) > 0xFF:
        raise ValueError(f"initializer too long: {len(msg.initializer)}")
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_leading_length_bytes(out, msg.initializer, 1)
    return bytes(out)


def decode_request_access(body: bytes) -> RequestAccess:
    if len(body) < 2:
        raise ValueError(f"RequestAccess body too short: {len(body)}")
    cmd_seq = body[0]
    init, _o = _s.read_leading_length_bytes(body, 1, 1)
    return RequestAccess(command_sequence=cmd_seq, initializer=init)


@dataclass(frozen=True)
class ConnectionSoftwareVersion:
    """11 fixed bytes describing communicator firmware + protocol."""
    COMMAND = Command.Connection_Software_Version
    major_version: int = 0
    minor_version: int = 0
    build_number: int = 0
    release_number: int = 0
    protocol_major_version: int = 0
    protocol_minor_version: int = 0
    product_id_1: int = 0
    product_id_2: int = 0
    market_id: int = 0
    customer_id: int = 0
    approval_id: int = 0


def encode_connection_software_version(msg: ConnectionSoftwareVersion) -> bytes:
    out = bytearray()
    for v in (msg.major_version, msg.minor_version, msg.build_number, msg.release_number,
              msg.protocol_major_version, msg.protocol_minor_version,
              msg.product_id_1, msg.product_id_2,
              msg.market_id, msg.customer_id, msg.approval_id):
        _s.write_u8(out, v)
    return bytes(out)


def decode_connection_software_version(body: bytes) -> ConnectionSoftwareVersion:
    if len(body) < 11:
        raise ValueError(f"ConnectionSoftwareVersion body too short: {len(body)}")
    return ConnectionSoftwareVersion(
        major_version=body[0], minor_version=body[1],
        build_number=body[2], release_number=body[3],
        protocol_major_version=body[4], protocol_minor_version=body[5],
        product_id_1=body[6], product_id_2=body[7],
        market_id=body[8], customer_id=body[9], approval_id=body[10],
    )


@dataclass(frozen=True)
class ConnectionSystemCapabilities:
    """All six CompactInt values."""
    COMMAND = Command.Connection_System_Capabilities
    max_zones: int = 0
    max_users: int = 0
    max_partitions: int = 0
    max_fobs: int = 0
    max_prox_tags: int = 0
    max_outputs: int = 0


def encode_connection_system_capabilities(msg: ConnectionSystemCapabilities) -> bytes:
    out = bytearray()
    for v in (msg.max_zones, msg.max_users, msg.max_partitions,
              msg.max_fobs, msg.max_prox_tags, msg.max_outputs):
        _s.write_compact_int(out, v)
    return bytes(out)


def decode_connection_system_capabilities(body: bytes) -> ConnectionSystemCapabilities:
    o = 0
    mz, o = _s.read_compact_int(body, o)
    mu, o = _s.read_compact_int(body, o)
    mp, o = _s.read_compact_int(body, o)
    mf, o = _s.read_compact_int(body, o)
    mpt, o = _s.read_compact_int(body, o)
    mo, _o = _s.read_compact_int(body, o)
    return ConnectionSystemCapabilities(
        max_zones=mz, max_users=mu, max_partitions=mp,
        max_fobs=mf, max_prox_tags=mpt, max_outputs=mo,
    )


# ===========================================================================
# Module status (panel->us notifications)
# ===========================================================================


@dataclass(frozen=True)
class ModuleZoneStatus:
    """[CompactInt: ZoneStart][CompactInt: ZoneCount][byte: StatusSize=1][bytes...]"""
    COMMAND = Command.ModuleStatus_Zone_Status
    zone_start: int
    zone_count: int
    status_size_in_bytes: int = 1
    zone_status_bytes: bytes = b""


def encode_module_zone_status(msg: ModuleZoneStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.zone_start)
    _s.write_compact_int(out, msg.zone_count)
    _s.write_u8(out, msg.status_size_in_bytes)
    out.extend(msg.zone_status_bytes)
    return bytes(out)


def decode_module_zone_status(body: bytes) -> ModuleZoneStatus:
    o = 0
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    size, o = _s.read_u8(body, o)
    rest = bytes(body[o:])
    return ModuleZoneStatus(
        zone_start=start, zone_count=count,
        status_size_in_bytes=size, zone_status_bytes=rest,
    )


@dataclass(frozen=True)
class ModulePartitionStatus:
    """[CompactInt: Partition][LeadingLengthArray: PartitionStatus bytes]"""
    COMMAND = Command.ModuleStatus_Partition_Status
    partition: int
    partition_status: bytes = b""

    @property
    def status1(self) -> PartitionStatus1:
        return PartitionStatus1(self.partition_status[0]) if self.partition_status else PartitionStatus1(0)

    @property
    def status2(self) -> PartitionStatus2:
        return PartitionStatus2(self.partition_status[1]) if len(self.partition_status) > 1 else PartitionStatus2(0)

    @property
    def status3(self) -> PartitionStatus3:
        return PartitionStatus3(self.partition_status[2]) if len(self.partition_status) > 2 else PartitionStatus3(0)


def encode_module_partition_status(msg: ModulePartitionStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_leading_length_bytes(out, msg.partition_status, 1)
    return bytes(out)


def decode_module_partition_status(body: bytes) -> ModulePartitionStatus:
    o = 0
    part, o = _s.read_compact_int(body, o)
    status, _o = _s.read_leading_length_bytes(body, o, 1)
    return ModulePartitionStatus(partition=part, partition_status=status)


@dataclass(frozen=True)
class SingleZoneBypassStatus:
    """[CompactInt: ZoneNumber][byte: BypassStatus]"""
    COMMAND = Command.ModuleStatus_Single_Zone_Bypass_Status
    zone_number: int
    bypass_status: int  # see :class:`BypassStatus`


def encode_single_zone_bypass_status(msg: SingleZoneBypassStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.zone_number)
    _s.write_u8(out, msg.bypass_status)
    return bytes(out)


def decode_single_zone_bypass_status(body: bytes) -> SingleZoneBypassStatus:
    o = 0
    zn, o = _s.read_compact_int(body, o)
    bp, _o = _s.read_u8(body, o)
    return SingleZoneBypassStatus(zone_number=zn, bypass_status=bp)


@dataclass(frozen=True)
class GlobalStatusResponse:
    """[bytes...] (opaque blob)"""
    COMMAND = Command.ModuleStatus_Global_Status
    data: bytes = b""


def encode_global_status_response(msg: GlobalStatusResponse) -> bytes:
    return msg.data


def decode_global_status_response(body: bytes) -> GlobalStatusResponse:
    return GlobalStatusResponse(data=bytes(body))


# ===========================================================================
# Control (arm/disarm/bypass)
# ===========================================================================


@dataclass(frozen=True)
class PartitionArm:
    """[cmd_seq:1][CompactInt: Partition][byte: ArmMode][BCDString: AccessCode]"""
    COMMAND = Command.ModuleControl_Partition_Arm_Control
    command_sequence: int
    partition: int
    arm_mode: int
    access_code: str = ""


def encode_partition_arm(msg: PartitionArm) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.arm_mode)
    # Unbounded BCD: pad to even length, no length prefix
    digits = msg.access_code or ""
    if len(digits) % 2 != 0:
        digits += "0"
    _s.write_bcd_fixed(out, digits, len(digits) // 2)
    return bytes(out)


def decode_partition_arm(body: bytes) -> PartitionArm:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    arm_mode, o = _s.read_u8(body, o)
    rest = body[o:]
    code, _o = _s.read_bcd_fixed(rest, 0, len(rest))
    return PartitionArm(
        command_sequence=cmd_seq, partition=partition,
        arm_mode=arm_mode, access_code=code,
    )


@dataclass(frozen=True)
class PartitionDisarm:
    """[cmd_seq:1][CompactInt: Partition][BCDString: AccessCode]"""
    COMMAND = Command.ModuleControl_Partition_Disarm_Control
    command_sequence: int
    partition: int
    access_code: str = ""


def encode_partition_disarm(msg: PartitionDisarm) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    digits = msg.access_code or ""
    if len(digits) % 2 != 0:
        digits += "0"
    _s.write_bcd_fixed(out, digits, len(digits) // 2)
    return bytes(out)


def decode_partition_disarm(body: bytes) -> PartitionDisarm:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    rest = body[o:]
    code, _o = _s.read_bcd_fixed(rest, 0, len(rest))
    return PartitionDisarm(
        command_sequence=cmd_seq, partition=partition, access_code=code,
    )


@dataclass(frozen=True)
class SingleZoneBypassWrite:
    """[cmd_seq:1][CompactInt: Partition][CompactInt: ZoneNumber][byte: BypassState]"""
    COMMAND = Command.Configuration_Write_Single_Zone_Bypass_Write
    command_sequence: int
    partition: int
    zone_number: int
    bypass_state: int  # 0 = unbypass, 1 = bypass


def encode_single_zone_bypass_write(msg: SingleZoneBypassWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    _s.write_compact_int(out, msg.zone_number)
    _s.write_u8(out, msg.bypass_state)
    return bytes(out)


def decode_single_zone_bypass_write(body: bytes) -> SingleZoneBypassWrite:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    zone, o = _s.read_compact_int(body, o)
    state, _o = _s.read_u8(body, o)
    return SingleZoneBypassWrite(
        command_sequence=cmd_seq, partition=partition,
        zone_number=zone, bypass_state=state,
    )


# ===========================================================================
# CommandRequestMessage (0x0800): typed status-request wrapper
#
# NeoHub pattern (verified):
#   var msg = CommandRequestMessage { Request = new <SomeType> { ... } }
# The panel decodes the embedded message and responds with the matching
# typed message as a new inbound transaction (not as a CommandResponse
# body). Examples:
#   * Request = ConnectionSystemCapabilities()        -> panel sends ConnectionSystemCapabilities
#   * Request = ModulePartitionStatus(partition=N)    -> panel sends ModulePartitionStatus
#   * Request = ModuleZoneStatus(start=1, count=N)    -> panel sends ModuleZoneStatus
#   * Request = NotificationLabelText(collection=Zone, start=A, end=B) -> labels
#
# Wire format:
#   [cmd_seq:1][embedded_cmd_id:u16 BE][embedded_body:N]
# ===========================================================================


@dataclass(frozen=True)
class CommandRequestMessage:
    """Status-request envelope (NeoHub-verified pattern).

    ``request`` is any other dataclass instance from this module (must
    be in ``ENCODERS``). The receiver pattern: panel responds with the
    typed message we asked for, decoded by our normal dispatch.
    """
    COMMAND = Command.ModuleStatus_Command_Request
    command_sequence: int
    request: Any


def encode_command_request_message(msg: CommandRequestMessage) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    embedded_cmd, embedded_body = encode_message(msg.request)
    _s.write_u16_be(out, embedded_cmd)
    out.extend(embedded_body)
    return bytes(out)


def decode_command_request_message(body: bytes) -> CommandRequestMessage:
    """Decode an inbound CommandRequestMessage (rare: usually only
    interesting when echoed back by the panel for debugging)."""
    cmd_seq, o = _s.read_u8(body, 0)
    if len(body) - o < 2:
        return CommandRequestMessage(
            command_sequence=cmd_seq,
            request=DefaultMessage(command=0, data=bytes(body[o:])),
        )
    embedded_cmd, o = _s.read_u16_be(body, o)
    embedded_body = bytes(body[o:])
    decoder = DECODERS.get(embedded_cmd)
    if decoder is not None:
        try:
            embedded = decoder(embedded_body)
        except Exception as exc:  # pragma: no cover (fall back to opaque)
            embedded = DefaultMessage(
                command=embedded_cmd, data=embedded_body,
                deserialization_error=str(exc),
            )
    else:
        embedded = DefaultMessage(command=embedded_cmd, data=embedded_body)
    return CommandRequestMessage(
        command_sequence=cmd_seq, request=embedded,
    )


# ===========================================================================
# Configuration / programming
# ===========================================================================


@dataclass(frozen=True)
class ConfigurationEnter:
    """[cmd_seq:1][CompactInt: Partition][byte: ProgMode][LeadingLengthBCDString: AccessCode][byte: ReadWrite]

    Note: AccessCode bytes are raw decimal digits (one byte per digit, value 0-9)
    NOT ASCII or hex-packed BCD. Per dsc-itv2-client verified behaviour.
    """
    COMMAND = Command.Configuration_Enter
    command_sequence: int
    partition: int = 1
    programming_mode: int = 0  # see :class:`ProgrammingMode`
    access_code: str = ""
    read_write: int = 0        # see :class:`ReadWriteAccess`


def encode_configuration_enter(msg: ConfigurationEnter) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.programming_mode)
    _s.write_bcd_leading_length(out, msg.access_code)
    _s.write_u8(out, msg.read_write)
    return bytes(out)


def decode_configuration_enter(body: bytes) -> ConfigurationEnter:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    prog_mode, o = _s.read_u8(body, o)
    code, o = _s.read_bcd_leading_length(body, o)
    rw, _o = _s.read_u8(body, o)
    return ConfigurationEnter(
        command_sequence=cmd_seq, partition=partition,
        programming_mode=prog_mode, access_code=code, read_write=rw,
    )


@dataclass(frozen=True)
class ConfigurationExit:
    """[cmd_seq:1][CompactInt: Partition]"""
    COMMAND = Command.Configuration_Exit
    command_sequence: int
    partition: int = 1


def encode_configuration_exit(msg: ConfigurationExit) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    return bytes(out)


def decode_configuration_exit(body: bytes) -> ConfigurationExit:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, _o = _s.read_compact_int(body, o)
    return ConfigurationExit(command_sequence=cmd_seq, partition=partition)


@dataclass(frozen=True)
class ProgrammingLeadInOut:
    """[CompactInt: Partition][byte: Programming][CompactInt: UserCode]
       [byte: Access][byte: Mode][packed DateTime: 4 bytes]"""
    COMMAND = Command.Configuration_Panel_Programming_Lead_InOut
    partition: int
    programming: int          # LeadIn / LeadOut
    user_code: int
    access: int               # see :class:`ProgrammingLeadInOutAccess`
    mode: int                 # see :class:`ProgrammingMode`
    date_time: _dt.datetime


def encode_programming_lead_in_out(msg: ProgrammingLeadInOut) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.programming)
    _s.write_compact_int(out, msg.user_code)
    _s.write_u8(out, msg.access)
    _s.write_u8(out, msg.mode)
    _s.write_packed_datetime(out, msg.date_time)
    return bytes(out)


def decode_programming_lead_in_out(body: bytes) -> ProgrammingLeadInOut:
    o = 0
    partition, o = _s.read_compact_int(body, o)
    programming, o = _s.read_u8(body, o)
    user_code, o = _s.read_compact_int(body, o)
    access, o = _s.read_u8(body, o)
    mode, o = _s.read_u8(body, o)
    dt_value, _o = _s.read_packed_datetime(body, o)
    return ProgrammingLeadInOut(
        partition=partition, programming=programming, user_code=user_code,
        access=access, mode=mode, date_time=dt_value,
    )


# ===========================================================================
# Notifications
# ===========================================================================


@dataclass(frozen=True)
class NotificationText:
    """[UnicodeString(len-prefix=1): Message]"""
    COMMAND = Command.Notification_Text
    message: str = ""


def encode_notification_text(msg: NotificationText) -> bytes:
    out = bytearray()
    _s.write_unicode_string(out, msg.message, 1)
    return bytes(out)


def decode_notification_text(body: bytes) -> NotificationText:
    msg_text, _o = _s.read_unicode_string(body, 0, 1)
    return NotificationText(message=msg_text)


@dataclass(frozen=True)
class NotificationDateTimeBroadcast:
    """[packed DateTime: 4 bytes]"""
    COMMAND = Command.Notification_Time_Date_Broadcast
    date_time: _dt.datetime


def encode_notification_date_time_broadcast(msg: NotificationDateTimeBroadcast) -> bytes:
    out = bytearray()
    _s.write_packed_datetime(out, msg.date_time)
    return bytes(out)


def decode_notification_date_time_broadcast(body: bytes) -> NotificationDateTimeBroadcast:
    dt_value, _o = _s.read_packed_datetime(body, 0)
    return NotificationDateTimeBroadcast(date_time=dt_value)


@dataclass(frozen=True)
class NotificationChimeBroadcast:
    """[opaque bytes]"""
    COMMAND = Command.Notification_Chime_Broadcast
    data: bytes = b""


def encode_notification_chime_broadcast(msg: NotificationChimeBroadcast) -> bytes:
    return msg.data


def decode_notification_chime_broadcast(body: bytes) -> NotificationChimeBroadcast:
    return NotificationChimeBroadcast(data=bytes(body))


@dataclass(frozen=True)
class NotificationArmDisarm:
    """[CompactInt: Partition][byte: ArmMode][byte: Method][CompactInt: UserId]"""
    COMMAND = Command.Notification_Arming_Disarming
    partition: int
    arm_mode: int      # see :class:`ArmingMode`
    method: int        # see :class:`ArmDisarmMethod`
    user_id: int


def encode_notification_arm_disarm(msg: NotificationArmDisarm) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.arm_mode)
    _s.write_u8(out, msg.method)
    _s.write_compact_int(out, msg.user_id)
    return bytes(out)


def decode_notification_arm_disarm(body: bytes) -> NotificationArmDisarm:
    o = 0
    partition, o = _s.read_compact_int(body, o)
    arm_mode, o = _s.read_u8(body, o)
    method, o = _s.read_u8(body, o)
    user_id, _o = _s.read_compact_int(body, o)
    return NotificationArmDisarm(
        partition=partition, arm_mode=arm_mode, method=method, user_id=user_id,
    )


@dataclass(frozen=True)
class NotificationExitDelay:
    """[CompactInt: Partition][byte: DelayFlags][CompactInt: DurationInSeconds]"""
    COMMAND = Command.Notification_Exit_Delay
    partition: int
    delay_flags: int       # see :class:`ExitDelayFlags`
    duration_in_seconds: int


def encode_notification_exit_delay(msg: NotificationExitDelay) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.delay_flags)
    _s.write_compact_int(out, msg.duration_in_seconds)
    return bytes(out)


def decode_notification_exit_delay(body: bytes) -> NotificationExitDelay:
    o = 0
    partition, o = _s.read_compact_int(body, o)
    flags, o = _s.read_u8(body, o)
    duration, _o = _s.read_compact_int(body, o)
    return NotificationExitDelay(
        partition=partition, delay_flags=flags, duration_in_seconds=duration,
    )


@dataclass(frozen=True)
class NotificationLifestyleZoneStatus:
    """[CompactInt: ZoneNumber][byte: Status]"""
    COMMAND = Command.Notification_Life_Style_Zone_Status
    zone_number: int
    status: int  # see :class:`LifestyleZoneStatus`


def encode_notification_lifestyle_zone_status(msg: NotificationLifestyleZoneStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.zone_number)
    _s.write_u8(out, msg.status)
    return bytes(out)


def decode_notification_lifestyle_zone_status(body: bytes) -> NotificationLifestyleZoneStatus:
    zone, o = _s.read_compact_int(body, 0)
    status, _o = _s.read_u8(body, o)
    return NotificationLifestyleZoneStatus(zone_number=zone, status=status)


@dataclass(frozen=True)
class NotificationPartitionReadyStatus:
    """[CompactInt: PartitionNumber][byte: Status]"""
    COMMAND = Command.Notification_Partition_Ready_Status
    partition_number: int
    status: int  # see :class:`PartitionReadyStatus`


def encode_notification_partition_ready_status(msg: NotificationPartitionReadyStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition_number)
    _s.write_u8(out, msg.status)
    return bytes(out)


def decode_notification_partition_ready_status(body: bytes) -> NotificationPartitionReadyStatus:
    part, o = _s.read_compact_int(body, 0)
    status, _o = _s.read_u8(body, o)
    return NotificationPartitionReadyStatus(partition_number=part, status=status)


@dataclass(frozen=True)
class NotificationLabelText:
    """[CompactInt: Collection][CompactInt: Start][CompactInt: End]
       [FixedLengthUnicodeStringArray: Labels]

    Bound to ``Configuration_Notification_Configuration`` (0x0771): the panel
    uses this command for label broadcasts after a configuration change.
    """
    COMMAND = Command.Configuration_Notification_Configuration
    collection: int  # see :class:`LabelCollection`
    start: int
    end: int
    labels: tuple[str, ...] = ()


def encode_notification_label_text(msg: NotificationLabelText) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.collection)
    _s.write_compact_int(out, msg.start)
    _s.write_compact_int(out, msg.end)
    _s.write_fixed_unicode_string_array(out, list(msg.labels))
    return bytes(out)


def decode_notification_label_text(body: bytes) -> NotificationLabelText:
    o = 0
    coll, o = _s.read_compact_int(body, o)
    start, o = _s.read_compact_int(body, o)
    end, o = _s.read_compact_int(body, o)
    labels, _o = _s.read_fixed_unicode_string_array(body, o)
    return NotificationLabelText(
        collection=coll, start=start, end=end, labels=tuple(labels),
    )


@dataclass(frozen=True)
class NotificationAccessCode:
    """[byte: UpdatedParameter][CompactInt: NumRecords]
       [CompactInt: Start][CompactInt: End]"""
    COMMAND = Command.Notification_Access_Code_Update
    updated_parameter: int   # flags - see :class:`NotificationAccessCodeParameter`
    num_records_updated: int
    start: int
    end: int


def encode_notification_access_code(msg: NotificationAccessCode) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.updated_parameter)
    _s.write_compact_int(out, msg.num_records_updated)
    _s.write_compact_int(out, msg.start)
    _s.write_compact_int(out, msg.end)
    return bytes(out)


def decode_notification_access_code(body: bytes) -> NotificationAccessCode:
    o = 0
    param, o = _s.read_u8(body, o)
    n, o = _s.read_compact_int(body, o)
    start, o = _s.read_compact_int(body, o)
    end, _o = _s.read_compact_int(body, o)
    return NotificationAccessCode(
        updated_parameter=param, num_records_updated=n, start=start, end=end,
    )


# ===========================================================================
# Access code management
# ===========================================================================


@dataclass(frozen=True)
class AccessCodeReadRequest:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count]"""
    COMMAND = Command.Configuration_Read_Access_Code
    command_sequence: int
    access_code_start: int
    access_code_count: int


def encode_access_code_read_request(msg: AccessCodeReadRequest) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    return bytes(out)


def decode_access_code_read_request(body: bytes) -> AccessCodeReadRequest:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, _o = _s.read_compact_int(body, o)
    return AccessCodeReadRequest(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
    )


@dataclass(frozen=True)
class AccessCodeReadResponse:
    """[CompactInt: Start][CompactInt: Count][BCD-array: AccessCodes]"""
    COMMAND = Command.Response_Access_Code
    access_code_start: int
    access_code_count: int
    access_codes: tuple[str, ...] = ()


def encode_access_code_read_response(msg: AccessCodeReadResponse) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_bcd_array_leading_length(out, list(msg.access_codes))
    return bytes(out)


def decode_access_code_read_response(body: bytes) -> AccessCodeReadResponse:
    o = 0
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    codes, _o = _s.read_bcd_array_leading_length(body, o)
    return AccessCodeReadResponse(
        access_code_start=start, access_code_count=count,
        access_codes=tuple(codes),
    )


@dataclass(frozen=True)
class AccessCodeWrite:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count][BCD-array: AccessCodes]"""
    COMMAND = Command.Configuration_Write_Access_Code
    command_sequence: int
    access_code_start: int
    access_code_count: int
    access_codes: tuple[str, ...] = ()


def encode_access_code_write(msg: AccessCodeWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_bcd_array_leading_length(out, list(msg.access_codes))
    return bytes(out)


def decode_access_code_write(body: bytes) -> AccessCodeWrite:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    codes, _o = _s.read_bcd_array_leading_length(body, o)
    return AccessCodeWrite(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
        access_codes=tuple(codes),
    )


@dataclass(frozen=True)
class AccessCodeAttributeReadRequest:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count]"""
    COMMAND = Command.Configuration_Read_Access_Code_Attribute
    command_sequence: int
    access_code_start: int
    access_code_count: int


def encode_access_code_attribute_read_request(msg: AccessCodeAttributeReadRequest) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    return bytes(out)


def decode_access_code_attribute_read_request(body: bytes) -> AccessCodeAttributeReadRequest:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, _o = _s.read_compact_int(body, o)
    return AccessCodeAttributeReadRequest(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
    )


@dataclass(frozen=True)
class AccessCodeAttributeReadResponse:
    """[CompactInt: Start][CompactInt: Count][byte: DataWidth][bytes: Attributes]"""
    COMMAND = Command.Response_Access_Code_Attribute
    access_code_start: int
    access_code_count: int
    data_width: int = 1
    attributes: bytes = b""


def encode_access_code_attribute_read_response(msg: AccessCodeAttributeReadResponse) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_u8(out, msg.data_width)
    out.extend(msg.attributes)
    return bytes(out)


def decode_access_code_attribute_read_response(body: bytes) -> AccessCodeAttributeReadResponse:
    o = 0
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    dw, o = _s.read_u8(body, o)
    return AccessCodeAttributeReadResponse(
        access_code_start=start, access_code_count=count,
        data_width=dw, attributes=bytes(body[o:]),
    )


@dataclass(frozen=True)
class AccessCodeAttributeWrite:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count][byte: DataWidth][bytes: Attributes]"""
    COMMAND = Command.Configuration_Write_Access_Code_Attribute
    command_sequence: int
    access_code_start: int
    access_code_count: int
    data_width: int = 1
    attributes: bytes = b""


def encode_access_code_attribute_write(msg: AccessCodeAttributeWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_u8(out, msg.data_width)
    out.extend(msg.attributes)
    return bytes(out)


def decode_access_code_attribute_write(body: bytes) -> AccessCodeAttributeWrite:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    dw, o = _s.read_u8(body, o)
    return AccessCodeAttributeWrite(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
        data_width=dw, attributes=bytes(body[o:]),
    )


@dataclass(frozen=True)
class AccessCodePartitionAssignmentReadRequest:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count]"""
    COMMAND = Command.Configuration_Read_Access_Code_Partition_Assignment
    command_sequence: int
    access_code_start: int
    access_code_count: int


def encode_access_code_partition_assignment_read_request(
    msg: AccessCodePartitionAssignmentReadRequest,
) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    return bytes(out)


def decode_access_code_partition_assignment_read_request(
    body: bytes,
) -> AccessCodePartitionAssignmentReadRequest:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, _o = _s.read_compact_int(body, o)
    return AccessCodePartitionAssignmentReadRequest(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
    )


@dataclass(frozen=True)
class AccessCodePartitionAssignmentReadResponse:
    """[CompactInt: Start][CompactInt: Count][byte: DataWidth][bytes: CountxDataWidth bitmask]"""
    COMMAND = Command.Response_Access_Code_Partition_Assignment
    access_code_start: int
    access_code_count: int
    data_width: int = 1
    partition_bitmask: bytes = b""

    def partition_assignments(self) -> list[list[int]]:
        """Decode the flat bitmask into per-user 1-indexed partition lists."""
        if self.data_width == 0:
            return [[] for _ in range(self.access_code_count)]
        result: list[list[int]] = []
        for u in range(self.access_code_count):
            offset = u * self.data_width
            parts: list[int] = []
            for i in range(self.data_width):
                if offset + i >= len(self.partition_bitmask):
                    break
                bitmap = self.partition_bitmask[offset + i]
                for bit in range(8):
                    if bitmap & (1 << bit):
                        parts.append(i * 8 + bit + 1)
            result.append(parts)
        return result


def encode_access_code_partition_assignment_read_response(
    msg: AccessCodePartitionAssignmentReadResponse,
) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_u8(out, msg.data_width)
    out.extend(msg.partition_bitmask)
    return bytes(out)


def decode_access_code_partition_assignment_read_response(
    body: bytes,
) -> AccessCodePartitionAssignmentReadResponse:
    o = 0
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    dw, o = _s.read_u8(body, o)
    return AccessCodePartitionAssignmentReadResponse(
        access_code_start=start, access_code_count=count,
        data_width=dw, partition_bitmask=bytes(body[o:]),
    )


@dataclass(frozen=True)
class AccessCodePartitionAssignmentWrite:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count][byte: DataWidth][bytes: bitmask]"""
    COMMAND = Command.Configuration_Write_Access_Code_Partition_Assignment
    command_sequence: int
    access_code_start: int
    access_code_count: int
    data_width: int = 1
    partition_bitmask: bytes = b""


def encode_access_code_partition_assignment_write(msg: AccessCodePartitionAssignmentWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_u8(out, msg.data_width)
    out.extend(msg.partition_bitmask)
    return bytes(out)


def decode_access_code_partition_assignment_write(
    body: bytes,
) -> AccessCodePartitionAssignmentWrite:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    dw, o = _s.read_u8(body, o)
    return AccessCodePartitionAssignmentWrite(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
        data_width=dw, partition_bitmask=bytes(body[o:]),
    )


@dataclass(frozen=True)
class AccessCodeLabelWrite:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count][byte: Format=0x03]
       Per label (Count times): [byte: LabelByteLength][UTF-16BE bytes (space-padded to 14 chars = 28 bytes)]
    """
    COMMAND = Command.Configuration_Write_Access_Codes_Label
    LABEL_CHAR_LENGTH = 14   # NeoHub constant
    command_sequence: int
    access_code_start: int
    access_code_count: int
    labels: tuple[str, ...] = ()
    format: int = 0x03


def _normalize_label(s: str) -> str:
    if len(s) >= AccessCodeLabelWrite.LABEL_CHAR_LENGTH:
        return s[: AccessCodeLabelWrite.LABEL_CHAR_LENGTH]
    return s.ljust(AccessCodeLabelWrite.LABEL_CHAR_LENGTH)


def encode_access_code_label_write(msg: AccessCodeLabelWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.access_code_start)
    _s.write_compact_int(out, msg.access_code_count)
    _s.write_u8(out, msg.format)
    for raw in msg.labels:
        normalized = _normalize_label(raw or "")
        encoded = normalized.encode("utf-16-be")
        _s.write_leading_length_bytes(out, encoded, 1)
    return bytes(out)


def decode_access_code_label_write(body: bytes) -> AccessCodeLabelWrite:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    fmt, o = _s.read_u8(body, o)
    labels: list[str] = []
    for _ in range(count):
        if o >= len(body):
            break
        encoded, o = _s.read_leading_length_bytes(body, o, 1)
        labels.append(encoded.decode("utf-16-be").rstrip())
    return AccessCodeLabelWrite(
        command_sequence=cmd_seq,
        access_code_start=start, access_code_count=count,
        labels=tuple(labels), format=fmt,
    )


# ===========================================================================
# User code configuration
# ===========================================================================


@dataclass(frozen=True)
class UserCodeConfigurationReadRequest:
    """[cmd_seq:1][CompactInt: Start][CompactInt: Count]"""
    COMMAND = Command.Configuration_Read_User_Code_Configuration
    command_sequence: int
    user_code_start: int
    user_code_count: int


def encode_user_code_configuration_read_request(msg: UserCodeConfigurationReadRequest) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.user_code_start)
    _s.write_compact_int(out, msg.user_code_count)
    return bytes(out)


def decode_user_code_configuration_read_request(body: bytes) -> UserCodeConfigurationReadRequest:
    cmd_seq, o = _s.read_u8(body, 0)
    start, o = _s.read_compact_int(body, o)
    count, _o = _s.read_compact_int(body, o)
    return UserCodeConfigurationReadRequest(
        command_sequence=cmd_seq,
        user_code_start=start, user_code_count=count,
    )


@dataclass(frozen=True)
class UserCodeConfigurationReadResponse:
    """[CompactInt: Start][CompactInt: Count][byte: DataWidth][bytes: CodeType per user]"""
    COMMAND = Command.Response_User_Code_Configuration
    user_code_start: int
    user_code_count: int
    data_width: int = 1
    code_types: bytes = b""


def encode_user_code_configuration_read_response(msg: UserCodeConfigurationReadResponse) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.user_code_start)
    _s.write_compact_int(out, msg.user_code_count)
    _s.write_u8(out, msg.data_width)
    out.extend(msg.code_types)
    return bytes(out)


def decode_user_code_configuration_read_response(body: bytes) -> UserCodeConfigurationReadResponse:
    o = 0
    start, o = _s.read_compact_int(body, o)
    count, o = _s.read_compact_int(body, o)
    dw, o = _s.read_u8(body, o)
    return UserCodeConfigurationReadResponse(
        user_code_start=start, user_code_count=count,
        data_width=dw, code_types=bytes(body[o:]),
    )


# ===========================================================================
# Section read/write (installers programming)
#
# The body uses a self-describing flag byte:
#
#   bit 0: IndexIsUsed: append 1 byte for Index
#   bit 1: CountIsUsed: append 1 byte for Count
#   bit 2: ModuleNumberIsUsed: prepend 1 byte for Module (after flags)
#   bit 3: VirtualSectionNumber  (informational)
#   bits 4-6 (>>4 & 0x07): extra section-address words beyond the first
#   bit 7: MaskIsUsed        (informational)
#
# Layout: [flags:1] [Module:1?] [SectionAddr:2 x (1 + subSectionCount)]
#         [Index:1?] [Count:1?] [SectionData:rest]
# ===========================================================================


_SEC_FLAG_INDEX_USED  = 0x01
_SEC_FLAG_COUNT_USED  = 0x02
_SEC_FLAG_MODULE_USED = 0x04


def _encode_section_body(
    *,
    module: int | None,
    section_address: tuple[int, ...],
    index: int,
    count: int,
    data: bytes,
) -> bytes:
    if not section_address:
        raise ValueError("section_address must have at least one element")
    if len(section_address) > 8:
        raise ValueError("section_address cannot have more than 8 elements")
    use_index = index > 0
    use_count = count > 1
    flags = 0
    if module is not None:
        flags |= _SEC_FLAG_MODULE_USED
    if use_index:
        flags |= _SEC_FLAG_INDEX_USED
    if use_count:
        flags |= _SEC_FLAG_COUNT_USED
    flags |= (len(section_address) - 1) << 4
    out = bytearray()
    _s.write_u8(out, flags)
    if module is not None:
        _s.write_u8(out, module & 0xFF)
    for word in section_address:
        _s.write_u16_be(out, word)
    if use_index:
        _s.write_u8(out, index)
    if use_count:
        _s.write_u8(out, count)
    out.extend(data)
    return bytes(out)


def _decode_section_body(body: bytes) -> dict[str, Any]:
    if not body:
        raise ValueError("empty section body")
    flags = body[0]
    o = 1
    module: int | None = None
    if flags & _SEC_FLAG_MODULE_USED:
        if o >= len(body):
            raise ValueError("section body truncated at Module")
        module = body[o]
        o += 1
    extra_words = (flags >> 4) & 0x07
    address_words = 1 + extra_words
    if o + address_words * 2 > len(body):
        raise ValueError("section body truncated in SectionAddress")
    section_address = []
    for _ in range(address_words):
        word, o = _s.read_u16_be(body, o)
        section_address.append(word)
    index = 0
    if flags & _SEC_FLAG_INDEX_USED:
        if o >= len(body):
            raise ValueError("section body truncated at Index")
        index = body[o]; o += 1
    count = 1
    if flags & _SEC_FLAG_COUNT_USED:
        if o >= len(body):
            raise ValueError("section body truncated at Count")
        count = body[o]; o += 1
    return dict(
        module=module,
        section_address=tuple(section_address),
        index=index,
        count=count,
        data=bytes(body[o:]),
    )


@dataclass(frozen=True)
class SectionRead:
    """SectionRead command (no trailing data)."""
    COMMAND = Command.Configuration_Installers_Section_Read
    command_sequence: int
    section_address: tuple[int, ...]
    module: int | None = None
    index: int = 0
    count: int = 1


def encode_section_read(msg: SectionRead) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    out.extend(_encode_section_body(
        module=msg.module, section_address=msg.section_address,
        index=msg.index, count=msg.count, data=b"",
    ))
    return bytes(out)


def decode_section_read(body: bytes) -> SectionRead:
    cmd_seq, _o = _s.read_u8(body, 0)
    fields = _decode_section_body(body[1:])
    return SectionRead(
        command_sequence=cmd_seq,
        section_address=fields["section_address"],
        module=fields["module"], index=fields["index"], count=fields["count"],
    )


@dataclass(frozen=True)
class SectionReadResponse:
    """SectionReadResponse: same layout but includes SectionData trailing."""
    COMMAND = Command.Response_Installers_Section_Read
    section_address: tuple[int, ...]
    section_data: bytes
    module: int | None = None
    index: int = 0
    count: int = 1


def encode_section_read_response(msg: SectionReadResponse) -> bytes:
    return _encode_section_body(
        module=msg.module, section_address=msg.section_address,
        index=msg.index, count=msg.count, data=msg.section_data,
    )


def decode_section_read_response(body: bytes) -> SectionReadResponse:
    fields = _decode_section_body(body)
    return SectionReadResponse(
        section_address=fields["section_address"],
        section_data=fields["data"],
        module=fields["module"], index=fields["index"], count=fields["count"],
    )


@dataclass(frozen=True)
class SectionWrite:
    """SectionWrite command: same shape as Read but includes data to write."""
    COMMAND = Command.Configuration_Installers_Section_Write
    command_sequence: int
    section_address: tuple[int, ...]
    section_data: bytes
    module: int | None = None
    index: int = 0
    count: int = 1


def encode_section_write(msg: SectionWrite) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    out.extend(_encode_section_body(
        module=msg.module, section_address=msg.section_address,
        index=msg.index, count=msg.count, data=msg.section_data,
    ))
    return bytes(out)


def decode_section_write(body: bytes) -> SectionWrite:
    cmd_seq, _o = _s.read_u8(body, 0)
    fields = _decode_section_body(body[1:])
    return SectionWrite(
        command_sequence=cmd_seq,
        section_address=fields["section_address"],
        section_data=fields["data"],
        module=fields["module"], index=fields["index"], count=fields["count"],
    )


# ===========================================================================
# Tier A: high-value HA messages
#
# Confidently-typed messages whose layout can be inferred from existing
# patterns or from explicit DLS5/NeoHub semantics. Bodies marked ``STUB``
# are typed wrappers around an opaque ``raw_body``. They correctly route
# inbound messages by command ID so the layer above sees a meaningful
# class name + bytes, but the field decomposition is reserved for later
# verification against real-panel captures.
# ===========================================================================


# --- Trouble / alarm-memory / bus-status (STUB bodies) ---------------------

@dataclass(frozen=True)
class _OpaqueBody:
    """Mixin marker for messages whose body shape is not yet decoded."""
    raw_body: bytes = b""


@dataclass(frozen=True)
class ModuleStatusAlarmMemoryInformation(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Alarm_Memory_Information


@dataclass(frozen=True)
class ModuleStatusBusStatus(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Bus_Status


@dataclass(frozen=True)
class ModuleStatusTroubleDetail(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Trouble_Detail


# System-wide trouble groups, indexed by group_id (1-based, matches the
# HS2032 manual ``Trouble 01..Trouble 12`` keypad menu / [*][2] display).
# Source: DSC HS2032 Reference Manual, "Table 4-4: Trouble Indications".
TROUBLE_GROUP_LABELS: dict[int, str] = {
    1:  "Service Required",
    2:  "Battery Trouble",
    3:  "Bus Voltage",
    4:  "AC Trouble",
    5:  "Device Faults",
    6:  "Device Low Battery",
    7:  "Device Tampers",
    8:  "RF Delinquency",
    9:  "Module Supervisory",
    10: "Module Tamper",
    11: "Communications",
    12: "Not Networked",
}


@dataclass(frozen=True)
class ModuleStatusSystemTroubleStatusNew:
    """System-wide trouble snapshot, one bitmap byte per trouble group.

    The reply payload is a raw byte array; each byte represents one
    trouble group from the panel's ``[*][2]`` menu (HS2032 Table 4-4:
    ``Trouble 01..Trouble 12``). A non-zero byte means at least one
    sub-item in that group is active; the exact bit-to-item mapping is
    panel-dependent and best confirmed against live captures.

    Attributes
    ----------
    group_bytes : tuple[int, ...]
        Raw per-group bitmap bytes as received. Index 0 = Trouble 01,
        index 1 = Trouble 02, etc. May be shorter or longer than 12
        depending on firmware; coordinator treats missing groups as 0
        and unknown extra bytes as opaque (kept for diagnostics).
    """
    group_bytes: tuple[int, ...] = ()
    COMMAND = Command.ModuleStatus_System_Trouble_Status_New

    @property
    def raw_body(self) -> bytes:  # diagnostic alias used by some callers
        return bytes(self.group_bytes)

    @property
    def any_active(self) -> bool:
        return any(b != 0 for b in self.group_bytes)

    def group(self, group_id: int) -> int:
        """Return the raw bitmap for trouble group ``group_id`` (1-based).

        Returns 0 when the panel didn't include that group in the reply.
        """
        idx = group_id - 1
        if 0 <= idx < len(self.group_bytes):
            return self.group_bytes[idx]
        return 0


def encode_module_status_system_trouble_status_new(
    msg: ModuleStatusSystemTroubleStatusNew,
) -> bytes:
    return bytes(msg.group_bytes)


def decode_module_status_system_trouble_status_new(
    body: bytes,
) -> ModuleStatusSystemTroubleStatusNew:
    return ModuleStatusSystemTroubleStatusNew(group_bytes=tuple(body))


@dataclass(frozen=True)
class ModuleStatusTroubleDetailNotification(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Trouble_Detail_Notification


@dataclass(frozen=True)
class ModuleStatusZoneBypassStatus(_OpaqueBody):
    """STUB: multi-zone bypass status; differs from SingleZoneBypassStatus."""
    COMMAND = Command.ModuleStatus_Zone_Bypass_Status


@dataclass(frozen=True)
class ModuleStatusZoneAlarmStatus(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Zone_Alarm_Status


@dataclass(frozen=True)
class ModuleStatusMiscNonZoneAlarmStatus(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Miscellaneous_NonZone_Alarm_Status


@dataclass(frozen=True)
class ModuleStatusDiagnosticStatus(_OpaqueBody):
    """STUB: body shape needs real-panel verification."""
    COMMAND = Command.ModuleStatus_Diagnostic_Status


@dataclass(frozen=True)
class ModuleStatusDoorChimeStatus(_OpaqueBody):
    """STUB: door-chime status; opaque until verified."""
    COMMAND = Command.ModuleStatus_Door_Chime_Status


# --- Partition-scoped notifications (typed: [CompactInt:Partition][byte: flags/status]) ---
#
# These all share NeoHub's standard partition-event shape:
#   [CompactInt: Partition][byte: status/flags]
# Confirmed for several from NeoHub's notification handlers; the
# remaining ones in this group follow the same convention.


@dataclass(frozen=True)
class _PartitionByteStatus:
    """Common [CompactInt:Partition][byte:value] shape (4 helpers below)."""
    partition: int
    value: int


def _encode_partition_byte_status(msg: _PartitionByteStatus) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.value)
    return bytes(out)


def _decode_partition_byte_status_for(cls, body: bytes):
    o = 0
    partition, o = _s.read_compact_int(body, o)
    value, _o = _s.read_u8(body, o)
    return cls(partition=partition, value=value)


@dataclass(frozen=True)
class NotificationPartitionAlarmMemory(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Alarm_Memory


@dataclass(frozen=True)
class NotificationPartitionTroubleStatus(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Trouble_Status


@dataclass(frozen=True)
class NotificationPartitionBypassStatus(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Bypass_Status


@dataclass(frozen=True)
class NotificationPartitionBusyStatus(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Busy_Status


@dataclass(frozen=True)
class NotificationPartitionAudibleBell(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Audible_Bell


@dataclass(frozen=True)
class NotificationPartitionBlankStatus(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Blank_Status


@dataclass(frozen=True)
class NotificationPartitionQuickExit(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags]"""
    COMMAND = Command.Notification_Partition_Quick_Exit


@dataclass(frozen=True)
class NotificationSystemTest(_PartitionByteStatus):
    """[CompactInt:Partition][byte:flags] (best-effort layout)"""
    COMMAND = Command.Notification_System_Test


# Encoders/decoders factory bindings for the partition-byte family
def encode_notification_partition_alarm_memory(msg: NotificationPartitionAlarmMemory) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_alarm_memory(body: bytes) -> NotificationPartitionAlarmMemory:
    return _decode_partition_byte_status_for(NotificationPartitionAlarmMemory, body)


def encode_notification_partition_trouble_status(msg: NotificationPartitionTroubleStatus) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_trouble_status(body: bytes) -> NotificationPartitionTroubleStatus:
    return _decode_partition_byte_status_for(NotificationPartitionTroubleStatus, body)


def encode_notification_partition_bypass_status(msg: NotificationPartitionBypassStatus) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_bypass_status(body: bytes) -> NotificationPartitionBypassStatus:
    return _decode_partition_byte_status_for(NotificationPartitionBypassStatus, body)


def encode_notification_partition_busy_status(msg: NotificationPartitionBusyStatus) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_busy_status(body: bytes) -> NotificationPartitionBusyStatus:
    return _decode_partition_byte_status_for(NotificationPartitionBusyStatus, body)


def encode_notification_partition_audible_bell(msg: NotificationPartitionAudibleBell) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_audible_bell(body: bytes) -> NotificationPartitionAudibleBell:
    return _decode_partition_byte_status_for(NotificationPartitionAudibleBell, body)


def encode_notification_partition_blank_status(msg: NotificationPartitionBlankStatus) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_blank_status(body: bytes) -> NotificationPartitionBlankStatus:
    return _decode_partition_byte_status_for(NotificationPartitionBlankStatus, body)


def encode_notification_partition_quick_exit(msg: NotificationPartitionQuickExit) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_partition_quick_exit(body: bytes) -> NotificationPartitionQuickExit:
    return _decode_partition_byte_status_for(NotificationPartitionQuickExit, body)


def encode_notification_system_test(msg: NotificationSystemTest) -> bytes:
    return _encode_partition_byte_status(msg)


def decode_notification_system_test(body: bytes) -> NotificationSystemTest:
    return _decode_partition_byte_status_for(NotificationSystemTest, body)


# --- Notification_Entry_Delay (mirror of Notification_Exit_Delay) -----------


@dataclass(frozen=True)
class NotificationEntryDelay:
    """[CompactInt:Partition][byte:DelayFlags][CompactInt:DurationInSeconds]

    Mirror of :class:`NotificationExitDelay`; same flags semantics apply.
    """
    COMMAND = Command.Notification_Entry_Delay
    partition: int
    delay_flags: int
    duration_in_seconds: int


def encode_notification_entry_delay(msg: NotificationEntryDelay) -> bytes:
    out = bytearray()
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.delay_flags)
    _s.write_compact_int(out, msg.duration_in_seconds)
    return bytes(out)


def decode_notification_entry_delay(body: bytes) -> NotificationEntryDelay:
    o = 0
    partition, o = _s.read_compact_int(body, o)
    flags, o = _s.read_u8(body, o)
    duration, _o = _s.read_compact_int(body, o)
    return NotificationEntryDelay(
        partition=partition, delay_flags=flags, duration_in_seconds=duration,
    )


# --- Other Tier A notifications (typed where shape is obvious; STUB else) ---


@dataclass(frozen=True)
class NotificationEnrolmentStatus(_OpaqueBody):
    """STUB: device-enrollment event; body shape needs verification."""
    COMMAND = Command.Notification_Enrolment_Status


@dataclass(frozen=True)
class NotificationAccessCodeLength:
    """[byte: length]"""
    COMMAND = Command.Notification_Access_Code_Length
    access_code_length: int


def encode_notification_access_code_length(msg: NotificationAccessCodeLength) -> bytes:
    return bytes([msg.access_code_length & 0xFF])


def decode_notification_access_code_length(body: bytes) -> NotificationAccessCodeLength:
    if not body:
        raise ValueError("NotificationAccessCodeLength expects 1-byte body")
    return NotificationAccessCodeLength(access_code_length=body[0])


@dataclass(frozen=True)
class NotificationSignalStrength(_OpaqueBody):
    """STUB: communicator signal-strength readout."""
    COMMAND = Command.Notification_Signal_Strength


@dataclass(frozen=True)
class NotificationTemperatureBroadcast(_OpaqueBody):
    """STUB: wireless-sensor temperature broadcasts."""
    COMMAND = Command.Notification_Temperature_Broadcast


@dataclass(frozen=True)
class NotificationPartitionBannerNotification(_OpaqueBody):
    """STUB: partition banner text update."""
    COMMAND = Command.Notification_Partition_Banner_Notification


@dataclass(frozen=True)
class NotificationPartitionBuzzerTypeExtended(_OpaqueBody):
    """STUB: extended partition buzzer notification."""
    COMMAND = Command.Notification_Partition_Buzzer_Type_Extended


@dataclass(frozen=True)
class NotificationPartitionBasedGeneralActivity(_OpaqueBody):
    """STUB: partition-scoped general activity."""
    COMMAND = Command.Notification_Partition_Based_General_Activity


@dataclass(frozen=True)
class NotificationCommandOutputActivationExtended(_OpaqueBody):
    """STUB: command-output activation event."""
    COMMAND = Command.Notification_Command_Output_Activation_Extended


@dataclass(frozen=True)
class NotificationArmingPreAlert(_OpaqueBody):
    """STUB: arming-preview pre-alert."""
    COMMAND = Command.Notification_Arming_PreAlert


@dataclass(frozen=True)
class NotificationMiscellaneousPreAlert(_OpaqueBody):
    """STUB: miscellaneous pre-alert."""
    COMMAND = Command.Notification_Miscellaneous_PreAlert


@dataclass(frozen=True)
class NotificationBufferOverload(_OpaqueBody):
    """STUB: panel buffer-overload signal."""
    COMMAND = Command.Notification_Buffer_Overload


@dataclass(frozen=True)
class NotificationAnyLifeSafetyEvent(_OpaqueBody):
    """STUB: any life-safety communicable event."""
    COMMAND = Command.Notification_Any_Life_Safety_Communicable_Event


@dataclass(frozen=True)
class NotificationPanelDisplayConfiguration(_OpaqueBody):
    """STUB: panel display config update."""
    COMMAND = Command.Notification_Panel_Display_Configuration


# --- Control commands (Tier A) ---------------------------------------------


@dataclass(frozen=True)
class ModuleControlPartitionQuickExit:
    """[cmd_seq:1][CompactInt:Partition] (mirror of PartitionDisarm shape)."""
    COMMAND = Command.ModuleControl_Partition_Quick_Exit
    command_sequence: int
    partition: int = 1


def encode_module_control_partition_quick_exit(msg: ModuleControlPartitionQuickExit) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    return bytes(out)


def decode_module_control_partition_quick_exit(body: bytes) -> ModuleControlPartitionQuickExit:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, _o = _s.read_compact_int(body, o)
    return ModuleControlPartitionQuickExit(
        command_sequence=cmd_seq, partition=partition,
    )


@dataclass(frozen=True)
class ModuleControlActivateFAP:
    """[cmd_seq:1][CompactInt:Partition][byte:FAPType]

    ``FAPType``: 0 = Fire, 1 = Auxiliary, 2 = Panic (per panel manual).
    Layout per dsc-itv2-protocol observations.
    """
    COMMAND = Command.ModuleControl_Activate_FAP
    command_sequence: int
    partition: int = 1
    fap_type: int = 0


def encode_module_control_activate_fap(msg: ModuleControlActivateFAP) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    _s.write_u8(out, msg.fap_type)
    return bytes(out)


def decode_module_control_activate_fap(body: bytes) -> ModuleControlActivateFAP:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    fap_type, _o = _s.read_u8(body, o)
    return ModuleControlActivateFAP(
        command_sequence=cmd_seq, partition=partition, fap_type=fap_type,
    )


@dataclass(frozen=True)
class ModuleControlEnableDLSWindow:
    """[cmd_seq:1][CompactInt:Partition][BCDString:AccessCode]

    Mirror of PartitionArm/Disarm shape; opens the DLS download window
    for installer access. Best-effort layout: verify with capture.
    """
    COMMAND = Command.ModuleControl_Enable_DLS_Window
    command_sequence: int
    partition: int = 1
    access_code: str = ""


def encode_module_control_enable_dls_window(msg: ModuleControlEnableDLSWindow) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    digits = msg.access_code or ""
    if len(digits) % 2 != 0:
        digits += "0"
    _s.write_bcd_fixed(out, digits, len(digits) // 2)
    return bytes(out)


def decode_module_control_enable_dls_window(body: bytes) -> ModuleControlEnableDLSWindow:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    rest = body[o:]
    code, _o = _s.read_bcd_fixed(rest, 0, len(rest))
    return ModuleControlEnableDLSWindow(
        command_sequence=cmd_seq, partition=partition, access_code=code,
    )


@dataclass(frozen=True)
class ModuleControlCommandOutput:
    """[cmd_seq:1][CompactInt:Partition][CompactInt:OutputNumber]
    [byte:ActivationState][BCDString:AccessCode]

    ``activation_state=1`` triggers the selected command output.
    """
    COMMAND = Command.ModuleControl_Command_Output
    command_sequence: int
    partition: int = 1
    output_number: int = 1
    activation_state: int = 1
    access_code: str = ""


def encode_module_control_command_output(msg: ModuleControlCommandOutput) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_compact_int(out, msg.partition)
    _s.write_compact_int(out, msg.output_number)
    _s.write_u8(out, msg.activation_state)
    digits = msg.access_code or ""
    if len(digits) % 2 != 0:
        digits += "0"
    _s.write_bcd_fixed(out, digits, len(digits) // 2)
    return bytes(out)


def decode_module_control_command_output(body: bytes) -> ModuleControlCommandOutput:
    cmd_seq, o = _s.read_u8(body, 0)
    partition, o = _s.read_compact_int(body, o)
    output_number, o = _s.read_compact_int(body, o)
    activation_state, o = _s.read_u8(body, o)
    rest = body[o:]
    code, _o = _s.read_bcd_fixed(rest, 0, len(rest))
    return ModuleControlCommandOutput(
        command_sequence=cmd_seq, partition=partition,
        output_number=output_number, activation_state=activation_state,
        access_code=code,
    )


# --- Time-and-Date config ---------------------------------------------------


@dataclass(frozen=True)
class ConfigurationReadTimeAndDate:
    """[cmd_seq:1]: empty body request."""
    COMMAND = Command.Configuration_Read_Time_and_Date
    command_sequence: int


def encode_configuration_read_time_and_date(msg: ConfigurationReadTimeAndDate) -> bytes:
    return bytes([msg.command_sequence & 0xFF])


def decode_configuration_read_time_and_date(body: bytes) -> ConfigurationReadTimeAndDate:
    if len(body) < 1:
        raise ValueError("ConfigurationReadTimeAndDate body too short")
    return ConfigurationReadTimeAndDate(command_sequence=body[0])


@dataclass(frozen=True)
class ConfigurationWriteTimeAndDate:
    """[cmd_seq:1][packed DateTime:4]"""
    COMMAND = Command.Configuration_Write_Time_and_Date
    command_sequence: int
    date_time: _dt.datetime


def encode_configuration_write_time_and_date(msg: ConfigurationWriteTimeAndDate) -> bytes:
    out = bytearray()
    _s.write_u8(out, msg.command_sequence)
    _s.write_packed_datetime(out, msg.date_time)
    return bytes(out)


def decode_configuration_write_time_and_date(body: bytes) -> ConfigurationWriteTimeAndDate:
    cmd_seq, o = _s.read_u8(body, 0)
    dt_value, _o = _s.read_packed_datetime(body, o)
    return ConfigurationWriteTimeAndDate(command_sequence=cmd_seq, date_time=dt_value)


@dataclass(frozen=True)
class ResponseTimeAndDate:
    """[packed DateTime:4] (response payload for 0x0731)."""
    COMMAND = Command.Response_Time_and_Date
    date_time: _dt.datetime


def encode_response_time_and_date(msg: ResponseTimeAndDate) -> bytes:
    out = bytearray()
    _s.write_packed_datetime(out, msg.date_time)
    return bytes(out)


def decode_response_time_and_date(body: bytes) -> ResponseTimeAndDate:
    dt_value, _o = _s.read_packed_datetime(body, 0)
    return ResponseTimeAndDate(date_time=dt_value)


# ===========================================================================
# Tier B: additional Read/Write settings + VirtualKeypad
#
# All STUB bodies: registered with their command IDs for correct
# dispatch + meaningful logging. Replace bodies once verified.
# ===========================================================================


# --- Late-to-open / auto-arm / voice-dialler / bypass-zone settings ---------


@dataclass(frozen=True)
class _CommandSeqOpaque:
    """[cmd_seq:1][opaque...]"""
    command_sequence: int
    raw_body: bytes = b""


def _encode_cmdseq_opaque(msg) -> bytes:
    return bytes([msg.command_sequence & 0xFF]) + msg.raw_body


def _decode_cmdseq_opaque_for(cls, body: bytes):
    if not body:
        raise ValueError(f"{cls.__name__} body too short")
    return cls(command_sequence=body[0], raw_body=bytes(body[1:]))


def _make_request_stub(cls_name: str, command: Command):
    cls = dataclass(frozen=True)(type(
        cls_name, (_CommandSeqOpaque,), {"COMMAND": command, "__doc__": "STUB"},
    ))
    enc = lambda msg, _c=cls: _encode_cmdseq_opaque(msg)
    dec = lambda body, _c=cls: _decode_cmdseq_opaque_for(_c, body)
    return cls, enc, dec


def _make_response_stub(cls_name: str, command: Command):
    cls = dataclass(frozen=True)(type(
        cls_name, (_OpaqueBody,), {"COMMAND": command, "__doc__": "STUB"},
    ))
    enc = lambda msg, _c=cls: msg.raw_body
    dec = lambda body, _c=cls: _c(raw_body=bytes(body))
    return cls, enc, dec


# Generate Tier B Configuration_Read/Write stubs in bulk
_TIERB_REQUESTS: list[tuple[str, Command]] = [
    ("ConfigurationReadLateToOpen",                Command.Configuration_Read_Late_To_Open),
    ("ConfigurationReadLateToOpenTime",            Command.Configuration_Read_Late_To_Open_Time),
    ("ConfigurationReadVoiceDialler",              Command.Configuration_Read_Voice_dialler),
    ("ConfigurationReadBypassZone",                Command.Configuration_Read_Bypass_Zone),
    ("ConfigurationReadAutoArmTime",               Command.Configuration_Read_Auto_Arm_Time),
    ("ConfigurationReadLateToOpenTimeSingleDay",   Command.Configuration_Read_Late_To_Open_Time_Single_Day),
    ("ConfigurationReadAutoArmTimeReadSingleDay",  Command.Configuration_Read_Auto_Arm_Time_Read_Single_Day),
    ("ConfigurationReadEventReportingConfiguration",
        Command.Configuration_Read_Event_Reporting_Configuration),
    ("ConfigurationWriteLateToOpen",               Command.Configuration_Write_Late_To_Open),
    ("ConfigurationWriteLateToOpenTime",           Command.Configuration_Write_Late_To_Open_Time),
    ("ConfigurationWriteVoiceDialler",             Command.Configuration_Write_Voice_dialler),
    ("ConfigurationWriteBypassZone",               Command.Configuration_Write_Bypass_Zone),
    ("ConfigurationWriteAutoArmTimeProgram",       Command.Configuration_Write_Auto_Arm_Time_Program),
    ("ConfigurationWriteGroupBypass",              Command.Configuration_Write_Group_Bypass),
    ("ConfigurationWriteProximityTagProgramming",  Command.Configuration_Write_Proximity_Tag_Programming),
    ("ConfigurationWriteLateToOpenTimeSingleDay",  Command.Configuration_Write_Late_To_Open_Time_Single_Day),
    ("ConfigurationWriteAutoArmTimeProgramSingleDay",
        Command.Configuration_Write_Auto_Arm_Time_Program_Single_Day),
    ("ConfigurationWriteResetProgrammingToFactoryDefault",
        Command.Configuration_Write_Reset_Programming_to_Factory_Default),
    ("ConfigurationWriteEventReportingConfiguration",
        Command.Configuration_Write_Event_Reporting_Configuration),
]

_TIERB_RESPONSES: list[tuple[str, Command]] = [
    ("ResponseLateToOpen",                  Command.Response_Late_To_Open),
    ("ResponseLateToOpenTime",              Command.Response_Late_To_Open_Time),
    ("ResponseVoiceDialler",                Command.Response_Voice_Dialler),
    ("ResponseBypassZone",                  Command.Response_Bypass_Zone),
    ("ResponseAutoArmTime",                 Command.Response_Auto_Arm_Time),
    ("ResponseLateToOpenTimeSingleDay",     Command.Response_Late_To_Open_Time_Single_Day),
    ("ResponseAutoArmTimeReadSingleDay",    Command.Response_Auto_Arm_Time_Read_Single_Day),
    ("ResponseEventReportingConfigurationRead",
        Command.Response_Event_Reporting_Configuration_Read),
]


# Auto-generate the Tier-B request/response classes and bind to module
# globals so they're importable like any other message type.
_TIERB_BINDINGS: dict[type, tuple[Callable[[Any], bytes], Callable[[bytes], Any]]] = {}

for _name, _cmd in _TIERB_REQUESTS:
    _cls, _enc, _dec = _make_request_stub(_name, _cmd)
    globals()[_name] = _cls
    _TIERB_BINDINGS[_cls] = (_enc, _dec)

for _name, _cmd in _TIERB_RESPONSES:
    _cls, _enc, _dec = _make_response_stub(_name, _cmd)
    globals()[_name] = _cls
    _TIERB_BINDINGS[_cls] = (_enc, _dec)

del _name, _cmd, _cls, _enc, _dec


# ===========================================================================
# Tier C: STUBs for everything else in the ITv2 enum
#
# Catch-all coverage for command IDs we can't reasonably type without a
# live-panel capture: firmware upgrade, event-buffer logging, image
# transfer, access-level transitions, the deprecated/internal variants
# we don't actively replace, and the long-tail of ModuleControl_*.
#
# Two factories used:
#   * _make_request_stub  -> :class:`_CommandSeqOpaque`  (cmd_seq is the
#     first body byte)
#   * _make_response_stub -> :class:`_OpaqueBody`        (no cmd_seq)
#
# Heuristic for which factory to use: anything documented as a command
# (panel-bound request) gets the cmd-seq variant; anything documented as
# a notification, response, or panel-initiated event gets the opaque
# variant.
# ===========================================================================


# (Name, Command, "cmdseq" or "opaque")
_TIERC: list[tuple[str, Command, str]] = [
    # --- Logging (event-buffer + SMS) (10) ---
    ("LoggingGetEventBufferInformation",            Command.Logging_Get_Event_Buffer_information,             "cmdseq"),
    ("LoggingEventBufferReadByEventNumber",         Command.Logging_Event_Buffer_Read_by_Event_Number,         "cmdseq"),
    ("LoggingSingleEventBufferRead",                Command.Logging_Single_Event_Buffer_Read,                  "cmdseq"),
    ("LoggingSMSPreviewRead",                       Command.Logging_SMS_Preview_Read,                          "cmdseq"),
    ("LoggingSingleSMSTextRead",                    Command.Logging_Single_SMS_Text_Read,                      "cmdseq"),
    ("LoggingDeleteSMS",                            Command.Logging_Delete_SMS,                                "cmdseq"),
    ("LoggingEventBufferReadByDateTimeStamp",       Command.Logging_Event_Buffer_Read_by_Date_Time_Stamp,      "cmdseq"),
    ("LoggingPanelEventBufferNotification",         Command.Logging_Panel_Event_Buffer_Notification,           "opaque"),
    ("LoggingUnreadSMSNotification",                Command.Logging_Unread_SMS_Notification,                   "opaque"),
    ("LoggingPanelCommunicableEventTxNotification", Command.Logging_Panel_Communicable_Event_Tx_Notification,  "opaque"),

    # --- Firmware upgrade (7) ---
    ("UpgradeFirmwareAvailable",                Command.Upgrade_Firmware_Available,                 "opaque"),
    ("UpgradeStartDownload",                    Command.Upgrade_Start_Download,                     "cmdseq"),
    ("UpgradeCancelDownload",                   Command.Upgrade_Cancel_Download,                    "cmdseq"),
    ("UpgradeDownloadComplete",                 Command.Upgrade_Download_Complete,                  "opaque"),
    ("UpgradeFirmwareDataPacket",               Command.Upgrade_Firmware_Data_Packet,               "cmdseq"),
    ("UpgradeFirmwareAvailableCritical",        Command.Upgrade_Firmware_Available_Critical,        "opaque"),
    ("UpgradeFirmwareUpdateStatusNotification", Command.Upgrade_Firmware_Update_Status_Notification, "opaque"),

    # --- Access level transitions (3) ---
    ("AccessLevelEnter",     Command.AccessLevel_Enter,       "cmdseq"),
    ("AccessLevelExit",      Command.AccessLevel_Exit,        "cmdseq"),
    ("AccessLevelLeadInOut", Command.AccessLevel_Lead_InOut,  "opaque"),

    # --- Connection internals (5) ---
    ("ConnectionInformBufferSizes",                Command.Connection_Inform_TX_and_RX_buffer_sizes,             "opaque"),
    ("ConnectionPassThroughITv2Command",           Command.Connection_Pass_Through_ITv2_Command,                 "cmdseq"),
    ("ConnectionPanelStatus",                      Command.Connection_Panel_Status,                              "opaque"),
    ("ConnectionDeviceEnrolment",                  Command.Connection_Device_Enrolment,                          "opaque"),
    ("ConnectionEncapsulatedCommandForLongPackets",Command.Connection_Encapsulated_Command_for_Long_Packets,     "opaque"),

    # --- Configuration internals / deprecated (2) ---
    ("ConfigurationEnterDeprecated",   Command.Configuration_Enter_Deprecated,   "cmdseq"),
    ("ConfigurationAccessCodeWrapper", Command.Configuration_Access_Code_Wrapper, "cmdseq"),

    # --- Notification deprecated variants (3) ---
    ("NotificationCommandOutputActivationDeprecated",
        Command.Notification_Command_Output_Activation_Deprecated, "opaque"),
    ("NotificationGeneralActivityDeprecated",
        Command.Notification_General_Activity_Deprecated, "opaque"),
    ("NotificationPartitionBuzzerTypeDeprecated",
        Command.Notification_Partition_Buzzer_Type_Deprecated, "opaque"),

    # --- ModuleStatus deprecated + envelope: 2 (CommandRequestMessage is typed) ---
    ("ModuleStatusSystemTroubleStatusDeprecated", Command.ModuleStatus_System_Trouble_Status_Deprecated, "opaque"),
    ("ModuleStatusGroupedTroubleStatusDeprecated",Command.ModuleStatus_Grouped_Trouble_Status_Deprecated,"opaque"),

    # --- ModuleControl long-tail (12) ---
    ("ModuleControlSystemTestingDeprecated",        Command.ModuleControl_System_Testing_Deprecated,        "cmdseq"),
    ("ModuleControlDoorChimeStatusWriteDeprecated", Command.ModuleControl_Door_Chime_Status_Write_Deprecated,"cmdseq"),
    ("ModuleControlUserCallup",                     Command.ModuleControl_User_Callup,                      "cmdseq"),
    ("ModuleControlAutoArmEnableDisable",           Command.ModuleControl_Auto_Arm_EnableDisable,           "cmdseq"),
    ("ModuleControlInitiateLabelBroadcast",         Command.ModuleControl_Initiate_Label_Broadcast,         "cmdseq"),
    ("ModuleControlSilenceTroublesDeprecated",      Command.ModuleControl_Silence_Troubles_Deprecated,      "cmdseq"),
    ("ModuleControlUserActivity",                   Command.ModuleControl_User_Activity,                    "cmdseq"),
    ("ModuleControlPartitionBannerDisplay",         Command.ModuleControl_Partition_Banner_Display,         "cmdseq"),
    ("ModuleControlPartitionBuzzerControl",         Command.ModuleControl_Partition_Buzzer_Control,         "cmdseq"),
    ("ModuleControlVirtualZoneControl",             Command.ModuleControl_Virtual_Zone_Control,             "cmdseq"),
    ("ModuleControlSetPartitionMode",               Command.ModuleControl_Set_Partition_Mode,               "cmdseq"),
    ("ModuleControlDoorChimeStatusWriteExtended",   Command.ModuleControl_Door_Chime_Status_Write_Extended, "cmdseq"),

    # --- Image transfer (2) ---
    ("ImageTransferImageFileHeaderCommand",  Command.ImageTransfer_Image_File_Header_Command,   "cmdseq"),
    ("ImageTransferFileTransferDataBlocks",  Command.ImageTransfer_File_Transfer_Data_Blocks,   "cmdseq"),

    # --- Event-buffer / SMS responses (5) ---
    # Note: 0x4101 is aliased between Read_By_Event_Number and
    # Read_By_Date_Time_Stamp in the DLS5 enum. Both map to the same
    # Command value, so we register a single class+decoder for that ID
    # via Response_Event_Buffer_Read_By_Event_Number; the
    # _By_Date_Time_Stamp alias dispatches to the same record.
    ("ResponseGetEventBufferInformation",       Command.Response_Get_Event_Buffer_Information,       "opaque"),
    ("ResponseEventBufferReadByEventNumber",    Command.Response_Event_Buffer_Read_By_Event_Number,  "opaque"),
    ("ResponseSingleEventBufferRead",           Command.Response_Single_Event_Buffer_Read,           "opaque"),
    ("ResponseSMSPreviewRead",                  Command.Response_SMS_Preview_Read,                   "opaque"),
    ("ResponseSingleSMSTextRead",               Command.Response_Single_SMS_Text_Read,               "opaque"),
]


_TIERC_BINDINGS: dict[type, tuple[Callable[[Any], bytes], Callable[[bytes], Any]]] = {}

for _name, _cmd, _kind in _TIERC:
    if _kind == "cmdseq":
        _cls, _enc, _dec = _make_request_stub(_name, _cmd)
    elif _kind == "opaque":
        _cls, _enc, _dec = _make_response_stub(_name, _cmd)
    else:  # pragma: no cover (defensive)
        raise ValueError(f"unknown Tier-C kind for {_name}: {_kind}")
    globals()[_name] = _cls
    _TIERC_BINDINGS[_cls] = (_enc, _dec)

del _name, _cmd, _kind, _cls, _enc, _dec


# --- Configuration notification stubs (panel->us) ---------------------------


@dataclass(frozen=True)
class ConfigurationNotificationZoneAssignmentConfiguration(_OpaqueBody):
    """STUB: zone-assignment configuration broadcast."""
    COMMAND = Command.Configuration_Notification_Zone_Assignment_Configuration


@dataclass(frozen=True)
class ConfigurationNotificationPartitionAssignment(_OpaqueBody):
    """STUB: partition-assignment notification."""
    COMMAND = Command.Configuration_Notification_Partition_Assignment


@dataclass(frozen=True)
class ConfigurationNotificationVirtualZoneToZoneAssignment(_OpaqueBody):
    """STUB: virtual-zone to zone-assignment notification."""
    COMMAND = Command.Configuration_Notification_Virtual_Zone_to_Zone_Assignment


@dataclass(frozen=True)
class ConfigurationNotificationConfigurationExtend(_OpaqueBody):
    """STUB: extended notification configuration."""
    COMMAND = Command.Configuration_Notification_Configuration_Extend


# --- VirtualKeypad ----------------------------------------------------------


@dataclass(frozen=True)
class VirtualKeypadControl(_CommandSeqOpaque):
    """STUB: virtual-keypad control command. Body likely contains keypad
    addressing + a key-press code or LCD-display request."""
    COMMAND = Command.VirtualKeypad_Control


def encode_virtual_keypad_control(msg: VirtualKeypadControl) -> bytes:
    return _encode_cmdseq_opaque(msg)


def decode_virtual_keypad_control(body: bytes) -> VirtualKeypadControl:
    return _decode_cmdseq_opaque_for(VirtualKeypadControl, body)


@dataclass(frozen=True)
class VirtualKeypadNotificationKeyPressed(_OpaqueBody):
    """STUB: virtual keypad reporting a key press."""
    COMMAND = Command.VirtualKeypad_Notification_Key_Pressed


@dataclass(frozen=True)
class VirtualKeypadNotificationLCDUpdate(_OpaqueBody):
    """STUB: virtual keypad LCD update (text + line numbers)."""
    COMMAND = Command.VirtualKeypad_Notification_LCD_Update


@dataclass(frozen=True)
class VirtualKeypadNotificationLCDCursor(_OpaqueBody):
    """STUB: virtual keypad cursor position."""
    COMMAND = Command.VirtualKeypad_Notification_LCD_Cursor


@dataclass(frozen=True)
class VirtualKeypadNotificationLEDStatus(_OpaqueBody):
    """STUB: virtual keypad LED status bitfield."""
    COMMAND = Command.VirtualKeypad_Notification_LED_Status


# ===========================================================================
# Multi-message + default fallback
# ===========================================================================


@dataclass(frozen=True)
class MultipleMessagePacket:
    """[ {variable-length-prefix}{sub-msg incl. command} ]*

    Each sub-message is a complete ITv2 application message (command word +
    body) wrapped with the same 1-or-2-byte variable-length prefix used by
    ITv2 framing.
    """
    COMMAND = Command.Connection_Encapsulated_Command_for_Multiple_Packets
    messages: tuple[Any, ...] = ()


def encode_multiple_message_packet(msg: MultipleMessagePacket) -> bytes:
    out = bytearray()
    for sub in msg.messages:
        cmd, body = encode_message(sub)
        sub_bytes = bytearray()
        _s.write_u16_be(sub_bytes, cmd)
        sub_bytes.extend(body)
        _s.write_variable_length_prefix(out, len(sub_bytes))
        out.extend(sub_bytes)
    return bytes(out)


def decode_multiple_message_packet(body: bytes) -> MultipleMessagePacket:
    subs: list[Any] = []
    o = 0
    while o < len(body):
        length, o = _s.read_variable_length_prefix(body, o)
        if o + length > len(body):
            raise ValueError("MultipleMessagePacket sub-message truncated")
        sub_bytes = body[o:o + length]
        o += length
        if len(sub_bytes) < 2:
            subs.append(DefaultMessage(command=0, data=bytes(sub_bytes)))
            continue
        cmd = (sub_bytes[0] << 8) | sub_bytes[1]
        sub_body = bytes(sub_bytes[2:])
        decoder = DECODERS.get(cmd)
        if decoder is None:
            subs.append(DefaultMessage(command=cmd, data=sub_body))
        else:
            try:
                subs.append(decoder(sub_body))
            except Exception as exc:  # pragma: no cover (defensive fallback)
                subs.append(DefaultMessage(
                    command=cmd, data=sub_body,
                    deserialization_error=str(exc),
                ))
    return MultipleMessagePacket(messages=tuple(subs))


@dataclass(frozen=True)
class DefaultMessage:
    """Fallback used when an inbound command has no registered decoder."""
    command: int = 0
    data: bytes = b""
    deserialization_error: str | None = None


# ===========================================================================
# Dispatch tables
# ===========================================================================


ENCODERS: dict[type, Callable[[Any], bytes]] = {
    SimpleAck: encode_simple_ack,
    ConnectionPoll: encode_connection_poll,
    ConnectionEndSession: encode_connection_end_session,
    CommandResponse: encode_command_response,
    CommandError: encode_command_error,
    OpenSession: encode_open_session,
    RequestAccess: encode_request_access,
    ConnectionSoftwareVersion: encode_connection_software_version,
    ConnectionSystemCapabilities: encode_connection_system_capabilities,
    ModuleZoneStatus: encode_module_zone_status,
    ModulePartitionStatus: encode_module_partition_status,
    SingleZoneBypassStatus: encode_single_zone_bypass_status,
    GlobalStatusResponse: encode_global_status_response,
    PartitionArm: encode_partition_arm,
    PartitionDisarm: encode_partition_disarm,
    SingleZoneBypassWrite: encode_single_zone_bypass_write,
    CommandRequestMessage: encode_command_request_message,
    ConfigurationEnter: encode_configuration_enter,
    ConfigurationExit: encode_configuration_exit,
    ProgrammingLeadInOut: encode_programming_lead_in_out,
    NotificationText: encode_notification_text,
    NotificationDateTimeBroadcast: encode_notification_date_time_broadcast,
    NotificationChimeBroadcast: encode_notification_chime_broadcast,
    NotificationArmDisarm: encode_notification_arm_disarm,
    NotificationExitDelay: encode_notification_exit_delay,
    NotificationLifestyleZoneStatus: encode_notification_lifestyle_zone_status,
    NotificationPartitionReadyStatus: encode_notification_partition_ready_status,
    NotificationLabelText: encode_notification_label_text,
    NotificationAccessCode: encode_notification_access_code,
    AccessCodeReadRequest: encode_access_code_read_request,
    AccessCodeReadResponse: encode_access_code_read_response,
    AccessCodeWrite: encode_access_code_write,
    AccessCodeAttributeReadRequest: encode_access_code_attribute_read_request,
    AccessCodeAttributeReadResponse: encode_access_code_attribute_read_response,
    AccessCodeAttributeWrite: encode_access_code_attribute_write,
    AccessCodePartitionAssignmentReadRequest: encode_access_code_partition_assignment_read_request,
    AccessCodePartitionAssignmentReadResponse: encode_access_code_partition_assignment_read_response,
    AccessCodePartitionAssignmentWrite: encode_access_code_partition_assignment_write,
    AccessCodeLabelWrite: encode_access_code_label_write,
    UserCodeConfigurationReadRequest: encode_user_code_configuration_read_request,
    UserCodeConfigurationReadResponse: encode_user_code_configuration_read_response,
    SectionRead: encode_section_read,
    SectionReadResponse: encode_section_read_response,
    SectionWrite: encode_section_write,
    MultipleMessagePacket: encode_multiple_message_packet,

    # --- Tier A: typed messages ---
    NotificationEntryDelay: encode_notification_entry_delay,
    NotificationPartitionAlarmMemory: encode_notification_partition_alarm_memory,
    NotificationPartitionTroubleStatus: encode_notification_partition_trouble_status,
    NotificationPartitionBypassStatus: encode_notification_partition_bypass_status,
    NotificationPartitionBusyStatus: encode_notification_partition_busy_status,
    NotificationPartitionAudibleBell: encode_notification_partition_audible_bell,
    NotificationPartitionBlankStatus: encode_notification_partition_blank_status,
    NotificationPartitionQuickExit: encode_notification_partition_quick_exit,
    NotificationSystemTest: encode_notification_system_test,
    NotificationAccessCodeLength: encode_notification_access_code_length,
    ModuleControlPartitionQuickExit: encode_module_control_partition_quick_exit,
    ModuleControlActivateFAP: encode_module_control_activate_fap,
    ModuleControlEnableDLSWindow: encode_module_control_enable_dls_window,
    ModuleControlCommandOutput: encode_module_control_command_output,
    ConfigurationReadTimeAndDate: encode_configuration_read_time_and_date,
    ConfigurationWriteTimeAndDate: encode_configuration_write_time_and_date,
    ResponseTimeAndDate: encode_response_time_and_date,

    # --- Tier A: STUB (opaque-body) message types ---
    ModuleStatusAlarmMemoryInformation:        lambda m: m.raw_body,
    ModuleStatusBusStatus:                     lambda m: m.raw_body,
    ModuleStatusTroubleDetail:                 lambda m: m.raw_body,
    ModuleStatusSystemTroubleStatusNew:        encode_module_status_system_trouble_status_new,
    ModuleStatusTroubleDetailNotification:     lambda m: m.raw_body,
    ModuleStatusZoneBypassStatus:              lambda m: m.raw_body,
    ModuleStatusZoneAlarmStatus:               lambda m: m.raw_body,
    ModuleStatusMiscNonZoneAlarmStatus:        lambda m: m.raw_body,
    ModuleStatusDiagnosticStatus:              lambda m: m.raw_body,
    ModuleStatusDoorChimeStatus:               lambda m: m.raw_body,
    NotificationEnrolmentStatus:               lambda m: m.raw_body,
    NotificationSignalStrength:                lambda m: m.raw_body,
    NotificationTemperatureBroadcast:          lambda m: m.raw_body,
    NotificationPartitionBannerNotification:   lambda m: m.raw_body,
    NotificationPartitionBuzzerTypeExtended:   lambda m: m.raw_body,
    NotificationPartitionBasedGeneralActivity: lambda m: m.raw_body,
    NotificationCommandOutputActivationExtended: lambda m: m.raw_body,
    NotificationArmingPreAlert:                lambda m: m.raw_body,
    NotificationMiscellaneousPreAlert:         lambda m: m.raw_body,
    NotificationBufferOverload:                lambda m: m.raw_body,
    NotificationAnyLifeSafetyEvent:            lambda m: m.raw_body,
    NotificationPanelDisplayConfiguration:     lambda m: m.raw_body,

    # --- Tier B: configuration notifications ---
    ConfigurationNotificationZoneAssignmentConfiguration:   lambda m: m.raw_body,
    ConfigurationNotificationPartitionAssignment:           lambda m: m.raw_body,
    ConfigurationNotificationVirtualZoneToZoneAssignment:   lambda m: m.raw_body,
    ConfigurationNotificationConfigurationExtend:           lambda m: m.raw_body,

    # --- Tier B: virtual keypad ---
    VirtualKeypadControl:                          encode_virtual_keypad_control,
    VirtualKeypadNotificationKeyPressed:           lambda m: m.raw_body,
    VirtualKeypadNotificationLCDUpdate:            lambda m: m.raw_body,
    VirtualKeypadNotificationLCDCursor:            lambda m: m.raw_body,
    VirtualKeypadNotificationLEDStatus:            lambda m: m.raw_body,
}

# Splice in the auto-generated Tier-B read/write request/response stubs
for _cls, (_enc, _dec) in _TIERB_BINDINGS.items():
    ENCODERS[_cls] = _enc
# Splice in the auto-generated Tier-C catch-all stubs
for _cls, (_enc, _dec) in _TIERC_BINDINGS.items():
    ENCODERS[_cls] = _enc
del _cls, _enc, _dec


DECODERS: dict[int, Callable[[bytes], Any]] = {
    int(Command.Simple_Ack): decode_simple_ack,
    int(Command.Connection_Poll): decode_connection_poll,
    int(Command.Connection_End_Session): decode_connection_end_session,
    int(Command.Command_Response): decode_command_response,
    int(Command.Command_Error): decode_command_error,
    int(Command.Connection_Open_Session): decode_open_session,
    int(Command.Connection_Request_Access): decode_request_access,
    int(Command.Connection_Software_Version): decode_connection_software_version,
    int(Command.Connection_System_Capabilities): decode_connection_system_capabilities,
    int(Command.ModuleStatus_Zone_Status): decode_module_zone_status,
    int(Command.ModuleStatus_Partition_Status): decode_module_partition_status,
    int(Command.ModuleStatus_Single_Zone_Bypass_Status): decode_single_zone_bypass_status,
    int(Command.ModuleStatus_Global_Status): decode_global_status_response,
    int(Command.ModuleControl_Partition_Arm_Control): decode_partition_arm,
    int(Command.ModuleControl_Partition_Disarm_Control): decode_partition_disarm,
    int(Command.Configuration_Write_Single_Zone_Bypass_Write): decode_single_zone_bypass_write,
    int(Command.ModuleStatus_Command_Request): decode_command_request_message,
    int(Command.Configuration_Enter): decode_configuration_enter,
    int(Command.Configuration_Exit): decode_configuration_exit,
    int(Command.Configuration_Panel_Programming_Lead_InOut): decode_programming_lead_in_out,
    int(Command.Notification_Text): decode_notification_text,
    int(Command.Notification_Time_Date_Broadcast): decode_notification_date_time_broadcast,
    int(Command.Notification_Chime_Broadcast): decode_notification_chime_broadcast,
    int(Command.Notification_Arming_Disarming): decode_notification_arm_disarm,
    int(Command.Notification_Exit_Delay): decode_notification_exit_delay,
    int(Command.Notification_Life_Style_Zone_Status): decode_notification_lifestyle_zone_status,
    int(Command.Notification_Partition_Ready_Status): decode_notification_partition_ready_status,
    int(Command.Configuration_Notification_Configuration): decode_notification_label_text,
    int(Command.Notification_Access_Code_Update): decode_notification_access_code,
    int(Command.Configuration_Read_Access_Code): decode_access_code_read_request,
    int(Command.Response_Access_Code): decode_access_code_read_response,
    int(Command.Configuration_Write_Access_Code): decode_access_code_write,
    int(Command.Configuration_Read_Access_Code_Attribute): decode_access_code_attribute_read_request,
    int(Command.Response_Access_Code_Attribute): decode_access_code_attribute_read_response,
    int(Command.Configuration_Write_Access_Code_Attribute): decode_access_code_attribute_write,
    int(Command.Configuration_Read_Access_Code_Partition_Assignment):
        decode_access_code_partition_assignment_read_request,
    int(Command.Response_Access_Code_Partition_Assignment):
        decode_access_code_partition_assignment_read_response,
    int(Command.Configuration_Write_Access_Code_Partition_Assignment):
        decode_access_code_partition_assignment_write,
    int(Command.Configuration_Write_Access_Codes_Label): decode_access_code_label_write,
    int(Command.Configuration_Read_User_Code_Configuration):
        decode_user_code_configuration_read_request,
    int(Command.Response_User_Code_Configuration): decode_user_code_configuration_read_response,
    int(Command.Configuration_Installers_Section_Read): decode_section_read,
    int(Command.Response_Installers_Section_Read): decode_section_read_response,
    int(Command.Configuration_Installers_Section_Write): decode_section_write,
    int(Command.Connection_Encapsulated_Command_for_Multiple_Packets):
        decode_multiple_message_packet,

    # --- Tier A: typed messages ---
    int(Command.Notification_Entry_Delay):              decode_notification_entry_delay,
    int(Command.Notification_Partition_Alarm_Memory):   decode_notification_partition_alarm_memory,
    int(Command.Notification_Partition_Trouble_Status): decode_notification_partition_trouble_status,
    int(Command.Notification_Partition_Bypass_Status):  decode_notification_partition_bypass_status,
    int(Command.Notification_Partition_Busy_Status):    decode_notification_partition_busy_status,
    int(Command.Notification_Partition_Audible_Bell):   decode_notification_partition_audible_bell,
    int(Command.Notification_Partition_Blank_Status):   decode_notification_partition_blank_status,
    int(Command.Notification_Partition_Quick_Exit):     decode_notification_partition_quick_exit,
    int(Command.Notification_System_Test):              decode_notification_system_test,
    int(Command.Notification_Access_Code_Length):       decode_notification_access_code_length,
    int(Command.ModuleControl_Partition_Quick_Exit):    decode_module_control_partition_quick_exit,
    int(Command.ModuleControl_Activate_FAP):            decode_module_control_activate_fap,
    int(Command.ModuleControl_Enable_DLS_Window):       decode_module_control_enable_dls_window,
    int(Command.ModuleControl_Command_Output):          decode_module_control_command_output,
    int(Command.Configuration_Read_Time_and_Date):      decode_configuration_read_time_and_date,
    int(Command.Configuration_Write_Time_and_Date):     decode_configuration_write_time_and_date,
    int(Command.Response_Time_and_Date):                decode_response_time_and_date,

    # --- Tier A: STUB decoders (returns typed wrapper with raw bytes) ---
    int(Command.ModuleStatus_Alarm_Memory_Information):
        lambda b: ModuleStatusAlarmMemoryInformation(raw_body=bytes(b)),
    int(Command.ModuleStatus_Bus_Status):
        lambda b: ModuleStatusBusStatus(raw_body=bytes(b)),
    int(Command.ModuleStatus_Trouble_Detail):
        lambda b: ModuleStatusTroubleDetail(raw_body=bytes(b)),
    int(Command.ModuleStatus_System_Trouble_Status_New):
        decode_module_status_system_trouble_status_new,
    int(Command.ModuleStatus_Trouble_Detail_Notification):
        lambda b: ModuleStatusTroubleDetailNotification(raw_body=bytes(b)),
    int(Command.ModuleStatus_Zone_Bypass_Status):
        lambda b: ModuleStatusZoneBypassStatus(raw_body=bytes(b)),
    int(Command.ModuleStatus_Zone_Alarm_Status):
        lambda b: ModuleStatusZoneAlarmStatus(raw_body=bytes(b)),
    int(Command.ModuleStatus_Miscellaneous_NonZone_Alarm_Status):
        lambda b: ModuleStatusMiscNonZoneAlarmStatus(raw_body=bytes(b)),
    int(Command.ModuleStatus_Diagnostic_Status):
        lambda b: ModuleStatusDiagnosticStatus(raw_body=bytes(b)),
    int(Command.ModuleStatus_Door_Chime_Status):
        lambda b: ModuleStatusDoorChimeStatus(raw_body=bytes(b)),
    int(Command.Notification_Enrolment_Status):
        lambda b: NotificationEnrolmentStatus(raw_body=bytes(b)),
    int(Command.Notification_Signal_Strength):
        lambda b: NotificationSignalStrength(raw_body=bytes(b)),
    int(Command.Notification_Temperature_Broadcast):
        lambda b: NotificationTemperatureBroadcast(raw_body=bytes(b)),
    int(Command.Notification_Partition_Banner_Notification):
        lambda b: NotificationPartitionBannerNotification(raw_body=bytes(b)),
    int(Command.Notification_Partition_Buzzer_Type_Extended):
        lambda b: NotificationPartitionBuzzerTypeExtended(raw_body=bytes(b)),
    int(Command.Notification_Partition_Based_General_Activity):
        lambda b: NotificationPartitionBasedGeneralActivity(raw_body=bytes(b)),
    int(Command.Notification_Command_Output_Activation_Extended):
        lambda b: NotificationCommandOutputActivationExtended(raw_body=bytes(b)),
    int(Command.Notification_Arming_PreAlert):
        lambda b: NotificationArmingPreAlert(raw_body=bytes(b)),
    int(Command.Notification_Miscellaneous_PreAlert):
        lambda b: NotificationMiscellaneousPreAlert(raw_body=bytes(b)),
    int(Command.Notification_Buffer_Overload):
        lambda b: NotificationBufferOverload(raw_body=bytes(b)),
    int(Command.Notification_Any_Life_Safety_Communicable_Event):
        lambda b: NotificationAnyLifeSafetyEvent(raw_body=bytes(b)),
    int(Command.Notification_Panel_Display_Configuration):
        lambda b: NotificationPanelDisplayConfiguration(raw_body=bytes(b)),

    # --- Tier B: configuration notifications ---
    int(Command.Configuration_Notification_Zone_Assignment_Configuration):
        lambda b: ConfigurationNotificationZoneAssignmentConfiguration(raw_body=bytes(b)),
    int(Command.Configuration_Notification_Partition_Assignment):
        lambda b: ConfigurationNotificationPartitionAssignment(raw_body=bytes(b)),
    int(Command.Configuration_Notification_Virtual_Zone_to_Zone_Assignment):
        lambda b: ConfigurationNotificationVirtualZoneToZoneAssignment(raw_body=bytes(b)),
    int(Command.Configuration_Notification_Configuration_Extend):
        lambda b: ConfigurationNotificationConfigurationExtend(raw_body=bytes(b)),

    # --- Tier B: virtual keypad ---
    int(Command.VirtualKeypad_Control):                  decode_virtual_keypad_control,
    int(Command.VirtualKeypad_Notification_Key_Pressed):
        lambda b: VirtualKeypadNotificationKeyPressed(raw_body=bytes(b)),
    int(Command.VirtualKeypad_Notification_LCD_Update):
        lambda b: VirtualKeypadNotificationLCDUpdate(raw_body=bytes(b)),
    int(Command.VirtualKeypad_Notification_LCD_Cursor):
        lambda b: VirtualKeypadNotificationLCDCursor(raw_body=bytes(b)),
    int(Command.VirtualKeypad_Notification_LED_Status):
        lambda b: VirtualKeypadNotificationLEDStatus(raw_body=bytes(b)),
}

# Splice in the auto-generated Tier-B read/write request/response decoders
for _cls, (_enc, _dec) in _TIERB_BINDINGS.items():
    DECODERS[int(_cls.COMMAND)] = _dec
# Splice in the auto-generated Tier-C catch-all decoders
for _cls, (_enc, _dec) in _TIERC_BINDINGS.items():
    DECODERS[int(_cls.COMMAND)] = _dec
del _cls, _enc, _dec


def encode_message(msg: Any) -> tuple[int, bytes]:
    """Encode a message instance, returning ``(command_id, body_bytes)``."""
    encoder = ENCODERS.get(type(msg))
    if encoder is None:
        raise KeyError(f"no encoder registered for {type(msg).__name__}")
    return int(msg.COMMAND), encoder(msg)


def decode_message(command: int, body: bytes):
    """Decode a body for a known command. Returns ``None`` if unknown."""
    decoder = DECODERS.get(command)
    if decoder is None:
        return None
    return decoder(body)
