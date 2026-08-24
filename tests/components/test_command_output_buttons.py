"""Command-output controls mirror programmed PGMs instead of fixed slots."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.dsc_neo_itv2.button import (
    DscNeoCommandOutputButton,
    async_setup_entry,
)
from custom_components.dsc_neo_itv2.const import DOMAIN
from custom_components.dsc_neo_itv2.coordinator import DscNeoCoordinator
from custom_components.dsc_neo_itv2.dsc_itv2.session import ConnectionSettings


def _coordinator(
    hass: HomeAssistant, entry_id: str = "test-entry",
) -> DscNeoCoordinator:
    coordinator = DscNeoCoordinator(
        hass=hass,
        entry_id=entry_id,
        settings=ConnectionSettings(),
        host="0.0.0.0",
        port=3072,
    )
    coordinator.partition_enabled = {1: True, 2: True}
    coordinator.has_panel_config = True
    coordinator._touch_partition(1)
    coordinator._touch_partition(2)
    return coordinator


def _entry(coordinator: DscNeoCoordinator) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = coordinator.entry_id
    entry.runtime_data = coordinator
    entry.data = {}
    return entry


async def test_only_programmed_command_outputs_are_added(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    coordinator.pgm_types = {
        1: 121,  # command output 1, partition 1
        2: 103,  # sensor reset / command output 2, partition 1
        3: 124,  # command output 4, partition 2
        4: 100,  # null PGM
    }
    coordinator.pgm_partition_assignment = {1: 1, 2: 1, 3: 2, 4: 1}
    added = []

    await async_setup_entry(hass, _entry(coordinator), added.extend)

    outputs = [entity for entity in added if isinstance(entity, DscNeoCommandOutputButton)]
    assert {entity._attr_unique_id for entity in outputs} == {
        "test-entry_partition_1_command_output_1",
        "test-entry_partition_1_command_output_2",
        "test-entry_partition_2_command_output_4",
    }


async def test_no_command_output_buttons_are_guessed_without_pgm_data(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass)
    added = []

    await async_setup_entry(hass, _entry(coordinator), added.extend)

    assert not any(
        isinstance(entity, DscNeoCommandOutputButton) for entity in added
    )


def test_unassigned_pgm_defaults_to_partition_one(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    coordinator.pgm_types = {7: 122}

    assert coordinator.command_output_pgms(1, 2) == [(7, 122)]
    assert coordinator.command_output_pgms(2, 2) == []


def test_stale_fixed_output_entities_are_pruned(
    hass: HomeAssistant, mock_config_entry,
):
    mock_config_entry.add_to_hass(hass)
    coordinator = _coordinator(hass, mock_config_entry.entry_id)
    coordinator.pgm_types = {1: 121}
    coordinator.pgm_partition_assignment = {1: 1}
    registry = er.async_get(hass)
    unique_ids = {
        output: f"{coordinator.entry_id}_partition_1_command_output_{output}"
        for output in range(1, 5)
    }
    for output, unique_id in unique_ids.items():
        registry.async_get_or_create(
            "button",
            DOMAIN,
            unique_id,
            suggested_object_id=f"command_output_{output}",
            config_entry=mock_config_entry,
        )

    assert coordinator.async_prune_stale_entities() == 3
    assert registry.async_get_entity_id("button", DOMAIN, unique_ids[1]) is not None
    for output in (2, 3, 4):
        assert registry.async_get_entity_id(
            "button", DOMAIN, unique_ids[output],
        ) is None
