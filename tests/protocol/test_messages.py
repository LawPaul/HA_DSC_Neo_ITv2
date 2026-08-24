"""Message encode/decode round-trips through the ENCODERS/DECODERS registries."""

from __future__ import annotations

import dataclasses
import datetime as dt

from dsc_itv2 import messages as m
from dsc_itv2.commands import Command
import pytest

ROUNDTRIP_CASES = [
    m.SimpleAck(),
    m.ConnectionPoll(),
    m.ConnectionEndSession(),
    m.CommandResponse(command_sequence=0x11, response_code=0),
    m.CommandResponse(command_sequence=0xFF, response_code=17),
    m.CommandError(nack_command=int(Command.Connection_Poll), nack_code=2),
    m.RequestAccess(command_sequence=3, initializer=bytes(range(16))),
    m.ConnectionSystemCapabilities(
        max_zones=128, max_users=95, max_partitions=8,
        max_fobs=32, max_prox_tags=32, max_outputs=4,
    ),
    m.ModuleZoneStatus(
        zone_start=1, zone_count=8, status_size_in_bytes=1,
        zone_status_bytes=bytes([0x01, 0x00, 0x80, 0x20, 0x00, 0x00, 0x00, 0x00]),
    ),
    m.ModulePartitionStatus(partition=1, partition_status=bytes([0x05, 0x02, 0x00])),
    m.SingleZoneBypassStatus(zone_number=7, bypass_status=1),
    m.SingleZoneBypassWrite(
        command_sequence=1, partition=1, zone_number=12, bypass_state=1
    ),
    m.PartitionArm(command_sequence=2, partition=1, arm_mode=2, access_code="1234"),
    m.PartitionDisarm(command_sequence=3, partition=1, access_code="1234"),
    m.ModuleControlActivateFAP(command_sequence=4, partition=1, fap_type=0),
    m.ModuleControlPartitionQuickExit(command_sequence=5, partition=2),
    m.ModuleControlCommandOutput(
        command_sequence=6, partition=1, output_number=1,
        activation_state=1, access_code="1234",
    ),
    m.NotificationArmDisarm(partition=1, arm_mode=2, method=0, user_id=40),
    m.NotificationPartitionReadyStatus(partition_number=1, status=1),
    m.NotificationExitDelay(partition=1, delay_flags=0x01, duration_in_seconds=60),
    m.NotificationEntryDelay(partition=1, delay_flags=0x01, duration_in_seconds=30),
    m.NotificationText(message="System Trouble"),
    m.NotificationDateTimeBroadcast(date_time=dt.datetime(2024, 6, 15, 13, 45, 30)),
    m.ResponseTimeAndDate(date_time=dt.datetime(2030, 1, 2, 3, 4, 5)),
    m.ConfigurationWriteTimeAndDate(
        command_sequence=6, date_time=dt.datetime(2024, 12, 31, 23, 59, 59)
    ),
    m.NotificationLabelText(
        collection=0, start=1, end=3, labels=("Front Door", "Back Door", "Garage")
    ),
    m.ModuleStatusSystemTroubleStatusNew(group_bytes=(0x00, 0x02, 0x00, 0x08)),
]


def _ids(cases):
    return [type(c).__name__ + f"#{i}" for i, c in enumerate(cases)]


@pytest.mark.parametrize("msg", ROUNDTRIP_CASES, ids=_ids(ROUNDTRIP_CASES))
def test_encode_decode_roundtrip(msg):
    command, body = m.encode_message(msg)
    assert command == int(msg.COMMAND)
    assert m.decode_message(command, body) == msg


@pytest.mark.parametrize("msg", ROUNDTRIP_CASES, ids=_ids(ROUNDTRIP_CASES))
def test_encoding_is_deterministic(msg):
    assert m.encode_message(msg) == m.encode_message(msg)


@pytest.mark.parametrize("msg", ROUNDTRIP_CASES, ids=_ids(ROUNDTRIP_CASES))
def test_messages_are_frozen_dataclasses(msg):
    assert dataclasses.is_dataclass(msg)
    fields = dataclasses.fields(msg)
    if not fields:
        pytest.skip("no fields to mutate")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(msg, fields[0].name, 0)


# --------------------------------------------------------------------------
# Specific wire layouts
# --------------------------------------------------------------------------


def test_simple_ack_has_an_empty_body():
    assert m.encode_message(m.SimpleAck()) == (int(Command.Simple_Ack), b"")


def test_command_response_is_two_bytes():
    _, body = m.encode_message(m.CommandResponse(command_sequence=0x11, response_code=0))
    assert body == b"\x11\x00"


def test_command_error_carries_a_big_endian_command_word():
    _, body = m.encode_message(m.CommandError(nack_command=0x0600, nack_code=3))
    assert body == b"\x06\x00\x03"


def test_partition_arm_body_starts_with_the_command_sequence():
    _, body = m.encode_message(
        m.PartitionArm(command_sequence=0x2A, partition=1, arm_mode=2, access_code="1234")
    )
    assert body[0] == 0x2A


def test_command_output_uses_verified_activation_layout():
    assert m.encode_message(
        m.ModuleControlCommandOutput(
            command_sequence=0xAA,
            partition=1,
            output_number=1,
            activation_state=1,
            access_code="1234",
        )
    ) == (
        int(Command.ModuleControl_Command_Output),
        bytes.fromhex("AA 01 01 01 01 01 12 34"),
    )


def test_zone_status_bytes_survive_untouched():
    raw = bytes([0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80])
    msg = m.ModuleZoneStatus(
        zone_start=1, zone_count=8, status_size_in_bytes=1, zone_status_bytes=raw
    )
    decoded = m.decode_message(*m.encode_message(msg))
    assert decoded.zone_status_bytes == raw


def test_partition_status_exposes_the_three_status_bytes():
    msg = m.ModulePartitionStatus(partition=1, partition_status=bytes([0x05, 0x02, 0x01]))
    assert msg.status1 == 0x05
    assert msg.status2 == 0x02
    assert msg.status3 == 0x01


def test_trouble_status_group_accessors():
    msg = m.ModuleStatusSystemTroubleStatusNew(group_bytes=(0x00, 0x02, 0x00, 0x00))
    assert msg.any_active
    assert msg.group(2) == 0x02
    assert msg.group(1) == 0x00


def test_trouble_status_is_inactive_when_every_group_is_zero():
    assert not m.ModuleStatusSystemTroubleStatusNew(group_bytes=(0, 0, 0, 0)).any_active


# --------------------------------------------------------------------------
# Registry behaviour
# --------------------------------------------------------------------------


def test_registries_are_populated_and_symmetric_in_size():
    assert len(m.ENCODERS) > 100
    assert len(m.DECODERS) > 100


def test_every_encoder_class_declares_a_command():
    missing = [cls.__name__ for cls in m.ENCODERS if not hasattr(cls, "COMMAND")]
    assert missing == []


def test_decoder_keys_are_uint16_wire_values():
    assert all(isinstance(k, int) and 0 <= k <= 0xFFFF for k in m.DECODERS)


def test_unknown_commands_decode_to_none():
    assert m.decode_message(0x9999, b"\x01\x02") is None


def test_encoding_an_unregistered_object_raises():
    class NotAMessage:
        COMMAND = 0x1234

    with pytest.raises(KeyError):
        m.encode_message(NotAMessage())


def test_default_message_preserves_unknown_traffic():
    msg = m.DefaultMessage(command=0x9999, data=b"\xde\xad", deserialization_error=None)
    assert msg.command == 0x9999
    assert msg.data == b"\xde\xad"


# --------------------------------------------------------------------------
# Malformed input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        int(Command.Command_Response),
        int(Command.Command_Error),
        int(Command.ModuleStatus_Zone_Status),
        int(Command.Connection_System_Capabilities),
    ],
)
def test_truncated_bodies_raise_rather_than_returning_garbage(command):
    with pytest.raises(ValueError):
        m.decode_message(command, b"")
