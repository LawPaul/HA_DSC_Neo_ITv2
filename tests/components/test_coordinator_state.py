"""Coordinator state regressions reported from real panels."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.dsc_neo_itv2.coordinator import DscNeoCoordinator
from custom_components.dsc_neo_itv2.dsc_itv2.enums import ArmingMode
from custom_components.dsc_neo_itv2.dsc_itv2.messages import (
    NotificationArmDisarm,
    NotificationLabelText,
    SectionReadResponse,
)
from custom_components.dsc_neo_itv2.dsc_itv2.session import ConnectionSettings
from custom_components.dsc_neo_itv2.storage import CachedPanelConfig


def _coordinator(hass: HomeAssistant) -> DscNeoCoordinator:
    return DscNeoCoordinator(
        hass=hass,
        entry_id="test-entry",
        settings=ConnectionSettings(),
        host="0.0.0.0",
        port=3072,
    )


def test_section_200_is_an_lsb_first_partition_bitmap(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    coordinator.max_partitions = 4

    coordinator._handle_section_read_response(
        SectionReadResponse(
            section_address=(200, 1),
            section_data=b"\x0a",
        )
    )

    assert coordinator.partition_enabled == {
        1: False,
        2: True,
        3: False,
        4: True,
    }


def test_unknown_partition_is_not_hidden_by_incomplete_panel_config(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass)
    coordinator.has_panel_config = True

    assert coordinator.is_partition_enabled(1)

    coordinator.partition_enabled[1] = False

    assert not coordinator.is_partition_enabled(1)


def test_confirmed_disarm_clears_entry_and_exit_delays(hass: HomeAssistant):
    coordinator = _coordinator(hass)
    coordinator._fire_event = MagicMock()
    coordinator._send_partition_signal = MagicMock()
    partition = coordinator._touch_partition(1)
    partition.is_armed = True
    partition.arming_mode = int(ArmingMode.AwayArm)
    partition.is_exit_delay = True
    partition.exit_delay_seconds = 30
    partition.is_entry_delay = True
    partition.entry_delay_seconds = 45

    coordinator._handle_arm_disarm(
        NotificationArmDisarm(
            partition=1,
            arm_mode=int(ArmingMode.Disarm),
            method=0,
            user_id=1,
        )
    )

    assert not partition.is_armed
    assert partition.arming_mode == int(ArmingMode.Disarm)
    assert not partition.is_exit_delay
    assert partition.exit_delay_seconds == 0
    assert not partition.is_entry_delay
    assert partition.entry_delay_seconds == 0


def test_cached_config_materializes_enabled_entities_with_labels(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass)

    coordinator._apply_cached_config(
        CachedPanelConfig(
            max_zones=16,
            max_partitions=2,
            zone_definitions={"1": 3, "12": 5, "13": 0},
            partition_enabled={"1": True, "2": False},
            zone_labels={"1": "Front door", "12": "Garage door"},
            partition_labels={"1": "Home"},
        )
    )

    assert coordinator.has_panel_config
    assert set(coordinator.zones) == {1, 12}
    assert coordinator.zones[1].label == "Front door"
    assert coordinator.zones[12].label == "Garage door"
    assert 13 not in coordinator.zones
    assert set(coordinator.partitions) == {1}
    assert coordinator.partitions[1].label == "Home"


def test_late_label_response_is_scheduled_for_cache_persistence(
    hass: HomeAssistant,
):
    coordinator = _coordinator(hass)
    coordinator.has_panel_config = True
    coordinator._schedule_label_cache_save = MagicMock()
    coordinator._touch_zone(12)

    coordinator._update_labels(
        NotificationLabelText(
            collection=0xD1,
            start=12,
            end=12,
            labels=("Garage door",),
        )
    )

    assert coordinator.zone_labels[12] == "Garage door"
    assert coordinator.zones[12].label == "Garage door"
    coordinator._schedule_label_cache_save.assert_called_once_with()
