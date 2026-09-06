"""SessionCodec handshake state machine, driven with a synthetic panel."""

from __future__ import annotations

from dataclasses import replace

from dsc_itv2 import itv2_framing, messages, packet, tlink_framing
from dsc_itv2.commands import Command
from dsc_itv2.encryption import Type1EncryptionHandler, Type2EncryptionHandler
from dsc_itv2.session import (
    BytesToSend,
    Closed,
    Connected,
    ConnectionSettings,
    Failed,
    Notification,
    SessionCodec,
    State,
)
import pytest

INTEGRATION_ID = "123456789012"
TYPE2_KEY = "000102030405060708090a0b0c0d0e0f"
TYPE1_ACCESS_CODE = "12345678"

TYPE2_SETTINGS = ConnectionSettings(
    integration_id=INTEGRATION_ID, type2_access_code_hex=TYPE2_KEY
)
TYPE1_SETTINGS = ConnectionSettings(
    integration_id=INTEGRATION_ID, type1_access_code=TYPE1_ACCESS_CODE
)


def open_session(encryption_type: int, command_sequence: int = 0x01):
    return messages.OpenSession(
        command_sequence=command_sequence,
        device_type=0x02,
        device_id=b"\x00\x01",
        firmware_version=b"\x05\x61",  # BCD -> 5.61
        protocol_version=0x0301,
        tx_buffer_size=1024,
        rx_buffer_size=1024,
        unknown=b"\x00\x00",
        encryption_type=encryption_type,
    )


class FakePanel:
    """Minimal panel-side peer: frames messages the way a TL280 would."""

    def __init__(self, encryption_type: int = 2):
        self.encryption_type = encryption_type
        self.sender_sequence = 0
        if encryption_type == 2:
            self.crypto = Type2EncryptionHandler(TYPE2_KEY)
        else:
            self.crypto = Type1EncryptionHandler(INTEGRATION_ID, TYPE1_ACCESS_CODE)

    def frame(self, msg, receiver_sequence: int = 0) -> bytes:
        self.sender_sequence = (self.sender_sequence + 1) & 0xFF or 1
        command, body = messages.encode_message(msg)
        payload = itv2_framing.add_framing(
            packet.build_packet(self.sender_sequence, receiver_sequence, command, body)
        )
        if self.crypto.is_outbound_active:
            payload = self.crypto.encrypt(payload)
        return tlink_framing.pack(INTEGRATION_ID.encode("ascii"), payload)

    def read(self, wire: bytes):
        """Decode a frame the codec produced, returning the parsed packet."""
        tlink_pkt, rest = tlink_framing.extract_packet(wire)
        assert tlink_pkt is not None
        assert rest == b""
        payload = tlink_pkt.payload
        if self.crypto.is_inbound_active:
            payload = self.crypto.decrypt(payload)
        return packet.parse_packet(itv2_framing.remove_framing(payload).packet)


def sent(events) -> list[bytes]:
    return [e.data for e in events if isinstance(e, BytesToSend)]


def only_sent(events) -> bytes:
    frames = sent(events)
    assert len(frames) == 1, f"expected one outbound frame, got {len(frames)}"
    return frames[0]


def run_handshake(encryption_type: int = 2):
    """Drive a codec through all 12 handshake steps. Returns (codec, panel, events)."""
    settings = TYPE2_SETTINGS if encryption_type == 2 else TYPE1_SETTINGS
    codec = SessionCodec(settings)
    panel = FakePanel(encryption_type)
    collected = []

    # Steps 1-2: panel OpenSession -> our CommandResponse
    events = codec.feed(panel.frame(open_session(encryption_type)))
    collected += events
    panel.read(only_sent(events))

    # Steps 3-4: panel SimpleAck -> our OpenSession echo
    events = codec.feed(panel.frame(messages.SimpleAck()))
    collected += events
    echo = panel.read(only_sent(events))
    assert echo.command == int(Command.Connection_Open_Session)

    # Steps 5-6: panel CommandResponse -> our SimpleAck
    events = codec.feed(
        panel.frame(messages.CommandResponse(command_sequence=1, response_code=0))
    )
    collected += events
    panel.read(only_sent(events))

    # Steps 7-8: panel RequestAccess (its initializer) -> our encrypted CommandResponse
    panel_initializer = panel.crypto.configure_inbound()
    events = codec.feed(
        panel.frame(
            messages.RequestAccess(command_sequence=2, initializer=panel_initializer)
        )
    )
    collected += events
    panel.read(only_sent(events))

    # Steps 9-10: panel SimpleAck -> our RequestAccess (our initializer)
    events = codec.feed(panel.frame(messages.SimpleAck()))
    collected += events
    our_request_access = panel.read(only_sent(events))
    assert our_request_access.command == int(Command.Connection_Request_Access)
    our_initializer = messages.decode_message(
        our_request_access.command, our_request_access.body
    ).initializer
    panel.crypto.configure_outbound(our_initializer)

    # Steps 11-12: panel CommandResponse (encrypted) -> our final SimpleAck
    events = codec.feed(
        panel.frame(messages.CommandResponse(command_sequence=3, response_code=0))
    )
    collected += events
    panel.read(only_sent(events))

    return codec, panel, collected


# --------------------------------------------------------------------------
# Initial state
# --------------------------------------------------------------------------


def test_codec_starts_awaiting_open_session():
    codec = SessionCodec(TYPE2_SETTINGS)
    assert codec.state is State.AWAITING_OPEN_SESSION
    assert codec.session_id == ""
    assert not codec.is_ready


def test_feeding_no_bytes_is_a_no_op():
    codec = SessionCodec(TYPE2_SETTINGS)
    assert codec.feed(b"") == []
    assert codec.state is State.AWAITING_OPEN_SESSION


def test_partial_frames_are_buffered_until_complete():
    codec = SessionCodec(TYPE2_SETTINGS)
    wire = FakePanel().frame(open_session(2))
    assert codec.feed(wire[:5]) == []
    assert codec.state is State.AWAITING_OPEN_SESSION
    assert sent(codec.feed(wire[5:]))


def test_sending_before_ready_is_rejected():
    codec = SessionCodec(TYPE2_SETTINGS)
    with pytest.raises(RuntimeError, match="AWAITING_OPEN_SESSION"):
        codec.send_message(messages.ConnectionPoll())


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------


def test_open_session_advances_the_state_machine():
    codec = SessionCodec(TYPE2_SETTINGS)
    events = codec.feed(FakePanel().frame(open_session(2)))
    assert codec.state is State.AWAITING_OPEN_SESSION_ACK_FROM_PANEL
    assert len(sent(events)) == 1


def test_the_session_id_is_taken_from_the_first_tlink_header():
    codec = SessionCodec(TYPE2_SETTINGS)
    codec.feed(FakePanel().frame(open_session(2)))
    assert codec.session_id == INTEGRATION_ID


def test_first_reply_echoes_the_command_sequence():
    codec = SessionCodec(TYPE2_SETTINGS)
    panel = FakePanel()
    events = codec.feed(panel.frame(open_session(2, command_sequence=0x2A)))
    reply = panel.read(only_sent(events))
    assert reply.command == int(Command.Command_Response)
    decoded = messages.decode_message(reply.command, reply.body)
    assert decoded.command_sequence == 0x2A
    assert decoded.response_code == 0


@pytest.mark.parametrize("trailing_data", [b"", bytes.fromhex("02 03 02")])
def test_open_session_echo_preserves_firmware_extensions(trailing_data):
    codec = SessionCodec(TYPE2_SETTINGS)
    panel = FakePanel()
    offer = replace(
        open_session(2, command_sequence=0x9B),
        firmware_version=b"\x05\x63",
        trailing_data=trailing_data,
    )
    codec.feed(panel.frame(offer))
    events = codec.feed(panel.frame(messages.SimpleAck()))
    echo = panel.read(only_sent(events))
    assert echo.command == int(Command.Connection_Open_Session)
    assert echo.body == bytes([0x9C]) + messages.encode_open_session(offer)[1:]
    assert codec.state is State.AWAITING_OPEN_SESSION_ECHO_RESPONSE


@pytest.mark.parametrize("encryption_type", [1, 2])
def test_full_handshake_reaches_ready(encryption_type):
    codec, _panel, events = run_handshake(encryption_type)
    assert codec.state is State.READY
    assert codec.is_ready
    connected = [e for e in events if isinstance(e, Connected)]
    assert len(connected) == 1
    assert connected[0].session_id == INTEGRATION_ID
    assert connected[0].encryption_type == encryption_type


def test_connected_reports_bcd_decoded_firmware():
    _codec, _panel, events = run_handshake()
    connected = next(e for e in events if isinstance(e, Connected))
    assert (connected.panel_firmware_major, connected.panel_firmware_minor) == (5, 61)


def test_no_failure_events_during_a_clean_handshake():
    _codec, _panel, events = run_handshake()
    assert [e for e in events if isinstance(e, Failed)] == []


def test_a_rejected_open_session_echo_fails_the_session():
    codec = SessionCodec(TYPE2_SETTINGS)
    panel = FakePanel()
    codec.feed(panel.frame(open_session(2)))
    codec.feed(panel.frame(messages.SimpleAck()))
    events = codec.feed(
        panel.frame(messages.CommandResponse(command_sequence=1, response_code=9))
    )
    assert codec.state is State.FAILED
    assert any(isinstance(e, Failed) for e in events)


def test_an_out_of_order_message_fails_the_session():
    codec = SessionCodec(TYPE2_SETTINGS)
    events = codec.feed(FakePanel().frame(messages.ConnectionPoll()))
    assert codec.state is State.FAILED
    failed = next(e for e in events if isinstance(e, Failed))
    assert "unexpected" in failed.reason


def test_a_missing_type2_key_fails_the_session():
    codec = SessionCodec(ConnectionSettings(integration_id=INTEGRATION_ID))
    panel = FakePanel()
    codec.feed(panel.frame(open_session(2)))
    codec.feed(panel.frame(messages.SimpleAck()))
    events = codec.feed(
        panel.frame(messages.CommandResponse(command_sequence=1, response_code=0))
    )
    assert codec.state is State.FAILED
    assert "type2_access_code_hex" in next(
        e for e in events if isinstance(e, Failed)
    ).reason


def test_an_unsupported_encryption_type_fails_the_session():
    codec = SessionCodec(TYPE2_SETTINGS)
    panel = FakePanel()
    codec.feed(panel.frame(open_session(7)))
    codec.feed(panel.frame(messages.SimpleAck()))
    events = codec.feed(
        panel.frame(messages.CommandResponse(command_sequence=1, response_code=0))
    )
    assert codec.state is State.FAILED
    assert "unsupported encryption type" in next(
        e for e in events if isinstance(e, Failed)
    ).reason


def test_garbage_bytes_fail_the_session():
    codec = SessionCodec(TYPE2_SETTINGS)
    events = codec.feed(tlink_framing.pack(b"header", b"\x00\x01\x02\x03"))
    assert codec.state is State.FAILED
    assert any(isinstance(e, Failed) for e in events)


# --------------------------------------------------------------------------
# Post-handshake traffic
# --------------------------------------------------------------------------


def test_notifications_are_published_and_acked():
    codec, panel, _ = run_handshake()
    events = codec.feed(panel.frame(messages.NotificationText(message="System Trouble")))

    notification = next(e for e in events if isinstance(e, Notification))
    assert notification.command == int(Command.Notification_Text)
    assert notification.decoded.message == "System Trouble"

    ack = panel.read(only_sent(events))
    assert ack.command == int(Command.Simple_Ack)


def test_inbound_simple_acks_are_swallowed():
    codec, panel, _ = run_handshake()
    assert codec.feed(panel.frame(messages.SimpleAck())) == []
    assert codec.state is State.READY


def test_send_message_produces_a_frame_the_panel_can_decrypt():
    codec, panel, _ = run_handshake()
    pkt = panel.read(codec.send_message(messages.ConnectionPoll()))
    assert pkt.command == int(Command.Connection_Poll)


def test_the_local_sequence_advances_between_sends():
    codec, panel, _ = run_handshake()
    first = panel.read(codec.send_message(messages.ConnectionPoll()))
    second = panel.read(codec.send_message(messages.ConnectionPoll()))
    assert first.sender_sequence != second.sender_sequence


def test_outbound_traffic_is_encrypted_after_the_handshake():
    codec, _panel, _ = run_handshake()
    wire = codec.send_message(messages.ConnectionPoll())
    tlink_pkt, _ = tlink_framing.extract_packet(wire)
    assert len(tlink_pkt.payload) % 16 == 0
    with pytest.raises(ValueError):
        itv2_framing.remove_framing(tlink_pkt.payload)


def test_a_panel_end_session_closes_the_codec():
    codec, panel, _ = run_handshake()
    events = codec.feed(panel.frame(messages.ConnectionEndSession()))
    assert codec.state is State.CLOSED
    assert any(isinstance(e, Closed) for e in events)


def test_close_emits_an_end_session_frame_once():
    codec, panel, _ = run_handshake()
    pkt = panel.read(codec.close())
    assert pkt.command == int(Command.Connection_End_Session)
    assert codec.state is State.CLOSED
    assert codec.close() == b""


def test_close_is_a_no_op_after_a_failure():
    codec = SessionCodec(TYPE2_SETTINGS)
    codec.feed(FakePanel().frame(messages.ConnectionPoll()))
    assert codec.state is State.FAILED
    assert codec.close() == b""
