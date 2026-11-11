# Copyright 2026 UniFi Insights contributors
"""Tests for the MAC-to-API-id device migration (cmcore patch, backlog 1795)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry
from unifi_official_api.network.models import Device

from custom_components.unifi_insights.const import DOMAIN
from custom_components.unifi_insights.device_id_migration import (
    _is_api_device_id,
    _rewrite_unique_id,
    async_migrate_mac_device_ids,
    mac_key,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

SITE = "88f7af54-98f8-306a-a1c7-c9349722b1f6"
MAC = "f0:9f:c2:17:4a:71"
API_ID = "a68579c2-e861-3ff3-ad1a-024270fd6d28"
CONSOLE_MAC = "e0:63:da:88:c0:bb"
CONSOLE_API_ID = "2e756474-1ce8-3eea-ac6c-e75f6848db8a"


def _entry(hass: HomeAssistant, entry_id: str = "mig_entry") -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, entry_id=entry_id, title="UniFi Local")
    entry.add_to_hass(hass)
    return entry


def _mac_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mac: str = MAC,
    *,
    identifier_mac: str | None = None,
) -> dr.DeviceEntry:
    """Register a switch the way the integration did while its id was the MAC."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SITE}_{identifier_mac or mac}")},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        manufacturer="Ubiquiti Inc.",
        model="US 8 60W",
        name="US860WLibreMon (10.0.1.31)",
    )


def _entity(  # noqa: PLR0917
    hass: HomeAssistant,
    entry: MockConfigEntry,
    device: dr.DeviceEntry,
    domain: str,
    unique_id: str,
    object_id: str,
    **kwargs: Any,
) -> er.RegistryEntry:
    return er.async_get(hass).async_get_or_create(
        domain,
        DOMAIN,
        unique_id,
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=object_id,
        **kwargs,
    )


def _site_api(
    device_id: str = API_ID, mac: str | None = MAC, site: str = SITE
) -> dict[str, dict[str, dict[str, Any]]]:
    return {site: {device_id: {"macAddress": mac}}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("f0:9f:c2:17:4a:71", "f09fc2174a71"),
        ("F0-9F-C2-17-4A-71", "f09fc2174a71"),
        ("f09f.c217.4a71", "f09fc2174a71"),
        ("  F0:9F:C2:17:4A:71  ", "f09fc2174a71"),
        (API_ID, None),
        ("5f1c2e8a9b3d4c6e7f8a9b0c", None),
        ("not a mac", None),
        ("", None),
        (None, None),
        (12, None),
    ],
)
def test_mac_key(value: object, expected: str | None) -> None:
    """A MAC reads the same in any case or separator; nothing else is one."""
    assert mac_key(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (API_ID, True),
        (MAC, False),
        ("5f1c2e8a9b3d4c6e7f8a9b0c", False),
        ("device1", False),
        (None, False),
        (7, False),
    ],
)
def test_is_api_device_id(value: object, expected: bool) -> None:  # noqa: FBT001
    """Only a UUID counts as the id of the integration API."""
    assert _is_api_device_id(value) is expected


@pytest.mark.parametrize(
    ("unique_id", "expected"),
    [
        (f"{SITE}_{MAC}_cpu_usage", f"{SITE}_{API_ID}_cpu_usage"),
        (f"{SITE}_{MAC.upper()}_cpu_usage", f"{SITE}_{API_ID}_cpu_usage"),
        (f"{MAC}_port_tx_bytes_1", f"{API_ID}_port_tx_bytes_1"),
        (
            f"{SITE}_{MAC}_port1_poe_power_cycle",
            f"{SITE}_{API_ID}_port1_poe_power_cycle",
        ),
        (f"{SITE}_rule-1_firewall_rule", None),
        (f"{SITE}_{MAC}", None),
        ("other", None),
    ],
)
def test_rewrite_unique_id(unique_id: str, expected: str | None) -> None:
    """Both unique id shapes the platforms build are rewritten, nothing else."""
    assert _rewrite_unique_id(unique_id, SITE, MAC, API_ID) == expected


async def test_migrates_device_and_entities_in_place(hass: HomeAssistant) -> None:
    """The device keeps its row; entities keep entity ids, names, areas and labels."""
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = _mac_device(hass, entry)
    dev_reg.async_update_device(device.id, area_id=None, labels={"network"})
    cpu = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")
    port = _entity(hass, entry, device, "sensor", f"{MAC}_port_tx_bytes_1", "port_tx")
    off = _entity(
        hass,
        entry,
        device,
        "button",
        f"{SITE}_{MAC}_device_restart",
        "restart",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    rule = _entity(
        hass, entry, device, "switch", f"{SITE}_rule-1_firewall_rule", "rule"
    )
    ent_reg.async_update_entity(cpu.entity_id, name="CPU", labels={"keep"})

    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    moved = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{SITE}_{API_ID}"), entry.entry_id
    )
    assert moved is not None
    assert moved.id == device.id
    assert moved.identifiers == {(DOMAIN, f"{SITE}_{API_ID}")}
    assert moved.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert moved.labels == {"network"}
    assert moved.name == "US860WLibreMon (10.0.1.31)"
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{SITE}_{MAC}"), entry.entry_id
        )
        is None
    )

    after_cpu = ent_reg.async_get(cpu.entity_id)
    assert after_cpu is not None
    assert after_cpu.unique_id == f"{SITE}_{API_ID}_cpu_usage"
    assert after_cpu.name == "CPU"
    assert after_cpu.labels == {"keep"}
    assert after_cpu.device_id == device.id
    after_port = ent_reg.async_get(port.entity_id)
    assert after_port is not None
    assert after_port.unique_id == f"{API_ID}_port_tx_bytes_1"
    after_off = ent_reg.async_get(off.entity_id)
    assert after_off is not None
    assert after_off.unique_id == f"{SITE}_{API_ID}_device_restart"
    assert after_off.disabled_by is er.RegistryEntryDisabler.USER
    after_rule = ent_reg.async_get(rule.entity_id)
    assert after_rule is not None
    assert after_rule.unique_id == f"{SITE}_rule-1_firewall_rule"
    assert after_rule.device_id == device.id

    # A second run finds nothing left to do.
    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 0


async def test_matches_mac_in_any_case_or_format(hass: HomeAssistant) -> None:
    """The registry's spelling of the MAC and the API's may differ."""
    entry = _entry(hass)
    device = _mac_device(hass, entry, identifier_mac=MAC.upper())
    entity = _entity(
        hass, entry, device, "sensor", f"{SITE}_{MAC.upper()}_cpu_usage", "cpu"
    )

    api = _site_api(mac="F0-9F-C2-17-4A-71")
    assert async_migrate_mac_device_ids(hass, entry, api) == 1

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{SITE}_{API_ID}"), entry.entry_id
    )
    updated = er.async_get(hass).async_get(entity.entity_id)
    assert updated is not None
    assert updated.unique_id == f"{SITE}_{API_ID}_cpu_usage"


async def test_two_devices_of_one_site(hass: HomeAssistant) -> None:
    """The console and the switch both move in one pass."""
    entry = _entry(hass)
    switch = _mac_device(hass, entry)
    console = _mac_device(hass, entry, CONSOLE_MAC)
    api = {
        SITE: {
            API_ID: {"macAddress": MAC},
            CONSOLE_API_ID: {"mac": CONSOLE_MAC.upper()},
        }
    }

    assert async_migrate_mac_device_ids(hass, entry, api) == 2

    dev_reg = dr.async_get(hass)
    assert dev_reg.async_get(switch.id).identifiers == {(DOMAIN, f"{SITE}_{API_ID}")}  # type: ignore[union-attr]
    assert dev_reg.async_get(console.id).identifiers == {  # type: ignore[union-attr]
        (DOMAIN, f"{SITE}_{CONSOLE_API_ID}")
    }


@pytest.mark.parametrize(
    "api",
    [
        pytest.param(_site_api(device_id=MAC), id="api-id-is-still-the-mac"),
        pytest.param(
            _site_api(device_id="5f1c2e8a9b3d4c6e7f8a9b0c"), id="classic-controller-id"
        ),
        pytest.param(_site_api(mac=None), id="no-mac"),
        pytest.param(_site_api(mac="not a mac"), id="unreadable-mac"),
        pytest.param(_site_api(mac="aa:bb:cc:dd:ee:ff"), id="other-hardware"),
        pytest.param(_site_api(site="another-site"), id="other-site"),
        pytest.param({SITE: "not a mapping"}, id="site-not-a-mapping"),
        pytest.param({SITE: {API_ID: "not a mapping"}}, id="device-not-a-mapping"),
        pytest.param({}, id="no-devices"),
        pytest.param("not a mapping", id="devices-not-a-mapping"),
    ],
)
async def test_leaves_device_alone(hass: HomeAssistant, api: Any) -> None:
    """Nothing moves unless the API lists that MAC under a UUID in that site."""
    entry = _entry(hass)
    device = _mac_device(hass, entry)
    entity = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")

    assert async_migrate_mac_device_ids(hass, entry, api) == 0

    assert dr.async_get(hass).async_get(device.id).identifiers == {  # type: ignore[union-attr]
        (DOMAIN, f"{SITE}_{MAC}")
    }
    kept = er.async_get(hass).async_get(entity.entity_id)
    assert kept is not None
    assert kept.unique_id == f"{SITE}_{MAC}_cpu_usage"


async def test_ignores_devices_of_another_entry(hass: HomeAssistant) -> None:
    """A device another console's entry owns is not this entry's to re-key."""
    entry = _entry(hass)
    other = _entry(hass, "other_entry")
    device = _mac_device(hass, other)

    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 0

    assert dr.async_get(hass).async_get(device.id).identifiers == {  # type: ignore[union-attr]
        (DOMAIN, f"{SITE}_{MAC}")
    }


async def test_merges_the_client_stub_that_holds_the_new_identifier(
    hass: HomeAssistant,
) -> None:
    """A client's uplink stub gives its entities to the real device and goes."""
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = _mac_device(hass, entry)
    stub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SITE}_{API_ID}")},
        name="UniFi Local",
    )
    client_button = _entity(
        hass, entry, stub, "button", f"{SITE}_client-1_reconnect", "client_reconnect"
    )
    client_off = _entity(
        hass,
        entry,
        stub,
        "switch",
        f"{SITE}_client-1_block_switch",
        "client_block",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    cpu = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")

    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    assert dev_reg.async_get(stub.id) is None
    moved = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{SITE}_{API_ID}"), entry.entry_id
    )
    assert moved is not None
    assert moved.id == device.id
    for original in (client_button, client_off):
        after = ent_reg.async_get(original.entity_id)
        assert after is not None
        assert after.device_id == device.id
        assert after.unique_id == original.unique_id
    after_cpu = ent_reg.async_get(cpu.entity_id)
    assert after_cpu is not None
    assert after_cpu.unique_id == f"{SITE}_{API_ID}_cpu_usage"


async def test_keeps_a_holder_that_is_a_real_device(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A device with a model of its own is never merged or removed."""
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    device = _mac_device(hass, entry)
    holder = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SITE}_{API_ID}")},
        manufacturer="Ubiquiti Inc.",
        model="US 8 60W",
    )

    with caplog.at_level(logging.WARNING):
        assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 0

    assert dev_reg.async_get(holder.id) is not None
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, f"{SITE}_{MAC}")}  # type: ignore[union-attr]
    assert "is already registered for device" in caplog.text


async def test_keeps_a_stub_of_another_entry(hass: HomeAssistant) -> None:
    """A stub another entry owns is not this entry's to remove."""
    entry = _entry(hass)
    other = _entry(hass, "other_entry")
    dev_reg = dr.async_get(hass)
    device = _mac_device(hass, entry)
    holder = dev_reg.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(DOMAIN, f"{SITE}_{API_ID}")}
    )

    # Identifiers are unique per entry, so this entry's own lookup finds no holder.
    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    assert dev_reg.async_get(holder.id) is not None
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, f"{SITE}_{API_ID}")}  # type: ignore[union-attr]


async def test_stub_that_cannot_be_emptied_stays(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """When an entity will not move, neither the stub nor the device changes."""
    entry = _entry(hass)
    dev_reg = dr.async_get(hass)
    device = _mac_device(hass, entry)
    stub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{SITE}_{API_ID}")}
    )
    client = _entity(hass, entry, stub, "button", f"{SITE}_c_reconnect", "c_reconnect")

    with (
        caplog.at_level(logging.WARNING),
        patch.object(
            er.EntityRegistry, "async_update_entity", side_effect=ValueError("busy")
        ),
    ):
        assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 0

    assert dev_reg.async_get(stub.id) is not None
    assert dev_reg.async_get(device.id).identifiers == {(DOMAIN, f"{SITE}_{MAC}")}  # type: ignore[union-attr]
    assert er.async_get(hass).async_get(client.entity_id).device_id == stub.id  # type: ignore[union-attr]
    assert "cannot move" in caplog.text


async def test_device_update_failure_is_logged_not_raised(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused identifier change skips that device and the setup goes on."""
    entry = _entry(hass)
    device = _mac_device(hass, entry)
    entity = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")

    with (
        caplog.at_level(logging.WARNING),
        patch.object(
            dr.DeviceRegistry, "async_update_device", side_effect=ValueError("clash")
        ),
    ):
        assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 0

    assert "cannot move device" in caplog.text
    kept = er.async_get(hass).async_get(entity.entity_id)
    assert kept is not None
    assert kept.unique_id == f"{SITE}_{MAC}_cpu_usage"


async def test_entity_unique_id_clash_skips_that_entity(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An entity whose new unique id is taken stays; the others still move."""
    entry = _entry(hass)
    ent_reg = er.async_get(hass)
    device = _mac_device(hass, entry)
    clash = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")
    free = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_memory_usage", "mem")
    taken = ent_reg.async_get_or_create(
        "sensor", DOMAIN, f"{SITE}_{API_ID}_cpu_usage", config_entry=entry
    )

    with caplog.at_level(logging.WARNING):
        assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    assert "cannot re-key" in caplog.text
    kept = ent_reg.async_get(clash.entity_id)
    assert kept is not None
    assert kept.unique_id == f"{SITE}_{MAC}_cpu_usage"
    assert ent_reg.async_get(taken.entity_id).unique_id == taken.unique_id  # type: ignore[union-attr]
    moved = ent_reg.async_get(free.entity_id)
    assert moved is not None
    assert moved.unique_id == f"{SITE}_{API_ID}_memory_usage"


async def test_deleted_entity_with_the_new_unique_id_does_not_block(
    hass: HomeAssistant,
) -> None:
    """Registry tombstones from the era of UUID ids (2026-09) are not live rows."""
    entry = _entry(hass)
    ent_reg = er.async_get(hass)
    device = _mac_device(hass, entry)
    old = _entity(
        hass, entry, device, "sensor", f"{SITE}_{API_ID}_cpu_usage", "cpu_old"
    )
    ent_reg.async_remove(old.entity_id)
    live = _entity(hass, entry, device, "sensor", f"{SITE}_{MAC}_cpu_usage", "cpu")

    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    moved = ent_reg.async_get(live.entity_id)
    assert moved is not None
    assert moved.unique_id == f"{SITE}_{API_ID}_cpu_usage"


async def test_other_platform_entities_are_not_rewritten(hass: HomeAssistant) -> None:
    """Only this integration's entities of the device are re-keyed."""
    entry = _entry(hass)
    device = _mac_device(hass, entry)
    foreign = er.async_get(hass).async_get_or_create(
        "sensor",
        "another_integration",
        f"{SITE}_{MAC}_cpu_usage",
        config_entry=entry,
        device_id=device.id,
    )

    assert async_migrate_mac_device_ids(hass, entry, _site_api()) == 1

    kept = er.async_get(hass).async_get(foreign.entity_id)
    assert kept is not None
    assert kept.unique_id == f"{SITE}_{MAC}_cpu_usage"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_entry_moves_the_registered_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_network_client: MagicMock,
    mock_protect_client: MagicMock,
    mock_local_auth: MagicMock,
) -> None:
    """Entry setup re-keys a MAC-registered device before the platforms add it."""
    mock_config_entry.add_to_hass(hass)
    mock_network_client.sites.get_all.return_value = [{"id": SITE, "name": "Default"}]
    mock_network_client.devices.get_all.return_value = [
        Device(
            id=API_ID,
            name="US860WLibreMon",
            model="US 8 60W",
            macAddress=MAC.upper(),
            state="ONLINE",
            ipAddress="10.0.1.31",
        )
    ]
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = _mac_device(hass, mock_config_entry)
    entity = _entity(
        hass,
        mock_config_entry,
        device,
        "sensor",
        f"{SITE}_{MAC}_cpu_usage",
        "us860wlibremon_cpu_usage",
    )

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    moved = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{SITE}_{API_ID}"), mock_config_entry.entry_id
    )
    assert moved is not None
    assert moved.id == device.id
    after = ent_reg.async_get(entity.entity_id)
    assert after is not None
    assert after.entity_id == entity.entity_id
    assert after.unique_id == f"{SITE}_{API_ID}_cpu_usage"
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{SITE}_{MAC}"), mock_config_entry.entry_id
        )
        is None
    )
    state = hass.states.get(entity.entity_id)
    assert state is not None
    assert state.state != "unavailable"
