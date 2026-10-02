"""cmcore (backlog 663): the device and Protect coordinators' per-entry poll interval.

A cloud entry is polled through UniFi's remote connector, which answered HTTP 408
(connector timeout) about 35 times an hour on the offsite console under the 30 s
poll. Cloud entries now default to 120 s, local entries keep 30 s, and the options
flow can set either.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_insights.const import (
    CONF_CONNECTION_TYPE,
    CONF_POLL_INTERVAL,
    CONNECTION_TYPE_LOCAL,
    CONNECTION_TYPE_REMOTE,
    DOMAIN,
    poll_interval_seconds,
)
from custom_components.unifi_insights.coordinators.base import entry_poll_interval
from custom_components.unifi_insights.coordinators.device import (
    UnifiDeviceCoordinator,
)
from custom_components.unifi_insights.coordinators.protect import (
    UnifiProtectCoordinator,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _entry(connection_type: str, options: dict | None = None) -> MockConfigEntry:
    data = {CONF_CONNECTION_TYPE: connection_type, CONF_API_KEY: "test_api_key"}
    if connection_type == CONNECTION_TYPE_LOCAL:
        data |= {CONF_HOST: "https://192.168.1.1", CONF_VERIFY_SSL: False}
    return MockConfigEntry(
        version=1,
        minor_version=0,
        domain=DOMAIN,
        title=f"UniFi Insights ({connection_type})",
        data=data,
        options=options or {},
        source="user",
        unique_id=f"poll_{connection_type}",
        entry_id=f"poll_{connection_type}_entry",
    )


async def _coordinators(
    hass: HomeAssistant, entry: MockConfigEntry
) -> tuple[UnifiDeviceCoordinator, UnifiProtectCoordinator]:
    entry.add_to_hass(hass)
    device = UnifiDeviceCoordinator(
        hass=hass,
        network_client=MagicMock(),
        protect_client=None,
        entry=entry,
        config_coordinator=MagicMock(),
    )
    protect = UnifiProtectCoordinator(
        hass=hass,
        network_client=MagicMock(),
        protect_client=MagicMock(),
        entry=entry,
    )
    return device, protect


def test_defaults_follow_the_connection_type() -> None:
    assert poll_interval_seconds(CONNECTION_TYPE_LOCAL, {}) == 30
    assert poll_interval_seconds(CONNECTION_TYPE_REMOTE, {}) == 120
    # An entry from before connection types existed is local.
    assert poll_interval_seconds(None, None) == 30


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60, 60),
        (45.0, 45),
        ("300", 300),
        (5, 30),
        (5000, 900),
        ("soon", 120),
        (True, 120),
        (None, 120),
    ],
)
def test_an_option_is_whole_seconds_within_bounds(value, expected) -> None:
    assert (
        poll_interval_seconds(CONNECTION_TYPE_REMOTE, {CONF_POLL_INTERVAL: value})
        == expected
    )


async def test_a_cloud_entry_polls_devices_and_protect_every_two_minutes(
    hass: HomeAssistant,
) -> None:
    device, protect = await _coordinators(hass, _entry(CONNECTION_TYPE_REMOTE))
    try:
        assert device.update_interval == timedelta(seconds=120)
        assert protect.update_interval == timedelta(seconds=120)
        # The independent sensor reconcile timer must not undo the slower poll.
        assert protect._sensor_reconcile_interval == timedelta(seconds=120)
    finally:
        await protect.async_shutdown()


async def test_a_local_entry_keeps_thirty_seconds(hass: HomeAssistant) -> None:
    entry = _entry(CONNECTION_TYPE_LOCAL)
    device, protect = await _coordinators(hass, entry)
    try:
        assert entry_poll_interval(entry) == timedelta(seconds=30)
        assert device.update_interval == timedelta(seconds=30)
        assert protect.update_interval == timedelta(seconds=30)
        assert protect._sensor_reconcile_interval == timedelta(seconds=30)
    finally:
        await protect.async_shutdown()


async def test_the_option_overrides_the_default(hass: HomeAssistant) -> None:
    device, protect = await _coordinators(
        hass, _entry(CONNECTION_TYPE_LOCAL, {CONF_POLL_INTERVAL: 300})
    )
    try:
        assert device.update_interval == timedelta(seconds=300)
        assert protect.update_interval == timedelta(seconds=300)
        assert protect._sensor_reconcile_interval == timedelta(seconds=300)
    finally:
        await protect.async_shutdown()


def _suggested(result, key: str):
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    pytest.fail(f"{key} is not in the options form")


async def test_the_options_form_suggests_the_interval_in_force(
    hass: HomeAssistant,
) -> None:
    entry = _entry(CONNECTION_TYPE_REMOTE)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == FlowResultType.FORM
    assert _suggested(result, CONF_POLL_INTERVAL) == 120


async def test_the_options_form_stores_whole_seconds(hass: HomeAssistant) -> None:
    entry = _entry(CONNECTION_TYPE_REMOTE)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "track_wifi_clients": False,
            "track_wired_clients": False,
            "client_control": True,
            CONF_POLL_INTERVAL: 180.0,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLL_INTERVAL] == 180
    assert isinstance(result["data"][CONF_POLL_INTERVAL], int)


async def test_a_cleared_interval_returns_to_the_default(hass: HomeAssistant) -> None:
    entry = _entry(CONNECTION_TYPE_REMOTE, {CONF_POLL_INTERVAL: 300})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _suggested(result, CONF_POLL_INTERVAL) == 300
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "track_wifi_clients": False,
            "track_wired_clients": False,
            "client_control": True,
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_POLL_INTERVAL not in result["data"]
    assert poll_interval_seconds(CONNECTION_TYPE_REMOTE, result["data"]) == 120
