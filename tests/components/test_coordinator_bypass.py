"""Zone bypass writes require an authenticated, serialized user-menu session."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.dsc_neo_itv2.const import CONF_USER_PIN
from custom_components.dsc_neo_itv2.coordinator import DscNeoCoordinator
from custom_components.dsc_neo_itv2.dsc_itv2.enums import ProgrammingMode
from custom_components.dsc_neo_itv2.dsc_itv2.messages import (
    ConfigurationEnter,
    ConfigurationExit,
    SingleZoneBypassWrite,
)
from custom_components.dsc_neo_itv2.dsc_itv2.session import ConnectionSettings


@pytest.fixture
def coordinator(hass, mock_config_entry):
    """Use the production coordinator with a fake, already-connected panel."""
    mock_config_entry.add_to_hass(hass)
    coordinator = DscNeoCoordinator(
        hass=hass,
        entry_id=mock_config_entry.entry_id,
        settings=ConnectionSettings(),
        host="127.0.0.1",
        port=3072,
    )
    connection = MagicMock()
    connection.is_open = True
    connection.send_message = AsyncMock()
    coordinator._active_conn = connection
    return coordinator, connection


@pytest.mark.parametrize("bypass", [True, False])
async def test_bypass_enters_authenticated_user_menu(coordinator, mock_config_entry, bypass):
    """A write uses the requested partition/zone and never installer mode."""
    coordinator, connection = coordinator
    await coordinator.async_bypass_zone(partition=2, zone=7, bypass=bypass)

    messages = [call.args[0] for call in connection.send_message.await_args_list]
    assert messages == [
        ConfigurationEnter(
            command_sequence=0,
            partition=2,
            programming_mode=int(ProgrammingMode.UserBypassProgramming),
            access_code=mock_config_entry.data[CONF_USER_PIN],
            read_write=1,
        ),
        SingleZoneBypassWrite(
            command_sequence=0, partition=2, zone_number=7, bypass_state=int(bypass)
        ),
        ConfigurationExit(command_sequence=0, partition=2),
    ]


async def test_missing_user_pin_uses_empty_code(hass, coordinator, mock_config_entry):
    """Allow panels whose user bypass menu does not require a PIN."""
    coordinator, connection = coordinator
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={key: value for key, value in mock_config_entry.data.items() if key != CONF_USER_PIN},
    )
    await coordinator.async_bypass_zone(partition=1, zone=7, bypass=True)
    assert connection.send_message.await_args_list[0].args[0].access_code == ""


async def test_concurrent_bypass_sessions_do_not_interleave(coordinator):
    """Separate writes must not enter or exit another call's user menu."""
    coordinator, connection = coordinator
    await asyncio.gather(
        coordinator.async_bypass_zone(partition=1, zone=7, bypass=True),
        coordinator.async_bypass_zone(partition=2, zone=8, bypass=False),
    )
    messages = [call.args[0] for call in connection.send_message.await_args_list]
    assert [type(message) for message in messages] == [
        ConfigurationEnter,
        SingleZoneBypassWrite,
        ConfigurationExit,
        ConfigurationEnter,
        SingleZoneBypassWrite,
        ConfigurationExit,
    ]
    assert [message.partition for message in messages] == [1, 1, 1, 2, 2, 2]


async def test_write_failure_exits_user_menu_and_propagates(coordinator):
    """A failed write must not leave the panel in the user menu."""
    coordinator, connection = coordinator
    connection.send_message.side_effect = [None, RuntimeError("write failed"), None]
    with pytest.raises(RuntimeError, match="write failed"):
        await coordinator.async_bypass_zone(partition=1, zone=7, bypass=True)
    assert isinstance(connection.send_message.await_args_list[-1].args[0], ConfigurationExit)
    assert not coordinator._bypass_lock.locked()


async def test_cancellation_exits_user_menu(coordinator):
    """Cancellation after entry also attempts user-menu cleanup."""
    coordinator, connection = coordinator
    entered = asyncio.Event()

    async def send_message(message):
        if isinstance(message, ConfigurationEnter):
            entered.set()

    connection.send_message.side_effect = send_message
    task = asyncio.create_task(coordinator.async_bypass_zone(partition=1, zone=7, bypass=True))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert isinstance(connection.send_message.await_args_list[-1].args[0], ConfigurationExit)
    assert not coordinator._bypass_lock.locked()


async def test_disconnect_during_write_does_not_attempt_exit(coordinator):
    """Do not send cleanup to an already-closed connection."""
    coordinator, connection = coordinator

    async def send_message(message):
        if isinstance(message, SingleZoneBypassWrite):
            connection.is_open = False
            raise ConnectionError("disconnected")

    connection.send_message.side_effect = send_message
    with pytest.raises(ConnectionError, match="disconnected"):
        await coordinator.async_bypass_zone(partition=1, zone=7, bypass=True)
    assert connection.send_message.await_count == 2
    assert not coordinator._bypass_lock.locked()


async def test_failed_menu_entry_does_not_attempt_exit(coordinator):
    """No user-menu session was entered if the first send fails."""
    coordinator, connection = coordinator
    connection.send_message.side_effect = ConnectionError("entry failed")
    with pytest.raises(ConnectionError, match="entry failed"):
        await coordinator.async_bypass_zone(partition=1, zone=7, bypass=True)
    connection.send_message.assert_awaited_once()
    assert not coordinator._bypass_lock.locked()
