# Copyright 2026 UniFi Insights contributors
"""
cmcore (backlog 1353): a local entry reads InnerSpace through UniFi's cloud.

InnerSpace 1.4.1 serves its Integration API only to a console login, so every
local API key gets 403. A local entry that stores a UI.com (Site Manager) key
builds its InnerSpace client against api.ui.com with it; Network, Protect and
Access stay local, and the cloud never delays, fails or reauthenticates the
entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unifi_insights import SETUP_PROBE_RETRIES
from custom_components.unifi_insights.api import (
    UniFiAuthenticationError,
    UniFiConnectionError,
    UniFiNotFoundError,
    UniFiResponseError,
)
from custom_components.unifi_insights.api.auth import ApiKeyAuth
from custom_components.unifi_insights.api.const import ConnectionType
from custom_components.unifi_insights.api.innerspace import (
    InnerSpaceFloorPlan,
    InnerSpaceProject,
    InnerSpaceProjectIdentity,
)
from custom_components.unifi_insights.config_flow import _clear_innerspace_cloud_field
from custom_components.unifi_insights.console_identity import cloud_host_id_for_mac
from custom_components.unifi_insights.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLEAR_INNERSPACE_CLOUD_API_KEY,
    CONF_CONNECTION_TYPE,
    CONF_CONSOLE_ID,
    CONF_INNERSPACE_CLOUD_API_KEY,
    CONF_INNERSPACE_CLOUD_CONSOLE_ID,
    CONNECTION_TYPE_LOCAL,
    DOMAIN,
)
from custom_components.unifi_insights.coordinators import (
    UnifiInsightsInnerSpaceCoordinator,
)
from custom_components.unifi_insights.diagnostics import TO_REDACT

if TYPE_CHECKING:
    from collections.abc import Generator

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

_HOST = "https://192.168.1.1"
_API_KEY = "network-api-key"
_CLOUD_KEY = "site-manager-key"
_CONSOLE_MAC = "e0:63:da:88:c0:bb"
# The live CMCoreUDMP host id: the MAC's 12 digits, then the rest of the id.
_CLOUD_ID = "E063DA88C0BB000000000475B63A0000000004A72469000000005E211E0A:1327936625"
_OTHER_ID = "D021F96BFF0F0000000000000000000000000000000000000000:1234567"


def _host(
    host_id: str,
    *,
    mac: str | None = None,
    hardware_mac: str | None = None,
    state: Any = "default",
) -> dict[str, Any]:
    """A Site Manager host record as GET /v1/hosts returns it."""
    reported: Any = {"name": "console"}
    if mac is not None:
        reported["mac"] = mac
    if hardware_mac is not None:
        reported["hardware"] = {"mac": hardware_mac}
    if state != "default":
        reported = state
    return {"id": host_id, "type": "console", "reportedState": reported}


_HOSTS = [
    _host(_OTHER_ID, mac="D021F96BFF0F", hardware_mac="D021F96BFF0F"),
    _host(_CLOUD_ID, mac="E063DA88C0BB", hardware_mac="E063DA88C0BB"),
]


class TestCloudHostIdForMac:
    """Matching the local console to its UI.com host id."""

    def test_reported_mac_matches(self) -> None:
        assert cloud_host_id_for_mac(_HOSTS, _CONSOLE_MAC) == _CLOUD_ID

    def test_hardware_mac_matches_when_no_top_level_mac(self) -> None:
        hosts = [_host(_OTHER_ID), _host(_CLOUD_ID, hardware_mac="e0-63-da-88-c0-bb")]
        assert cloud_host_id_for_mac(hosts, "E0:63:DA:88:C0:BB") == _CLOUD_ID

    def test_id_prefix_is_the_fallback_for_an_empty_reported_state(self) -> None:
        hosts = [_host(_OTHER_ID, state=None), _host(_CLOUD_ID, state=None)]
        assert cloud_host_id_for_mac(hosts, _CONSOLE_MAC) == _CLOUD_ID

    def test_a_reported_mac_wins_over_an_id_prefix(self) -> None:
        reporting = "AAAAAAAAAAAA0000:1"
        hosts = [_host(_CLOUD_ID, state={}), _host(reporting, mac="E063DA88C0BB")]
        assert cloud_host_id_for_mac(hosts, _CONSOLE_MAC) == reporting

    def test_two_hosts_reporting_one_mac_match_neither(self) -> None:
        hosts = [
            _host(_CLOUD_ID, mac="E063DA88C0BB"),
            _host("E063DA88C0BB0000:2", mac="E063DA88C0BB"),
        ]
        assert cloud_host_id_for_mac(hosts, _CONSOLE_MAC) is None

    def test_no_matching_host(self) -> None:
        assert cloud_host_id_for_mac(_HOSTS, "aa:bb:cc:dd:ee:ff") is None

    @pytest.mark.parametrize(
        "mac", [None, "", 5, "e0:63:da:88:c0", "e0:63:da:88:c0:zz", "e063da88c0bb00"]
    )
    def test_unusable_mac_matches_nothing(self, mac: Any) -> None:
        assert cloud_host_id_for_mac(_HOSTS, mac) is None

    def test_malformed_records_are_skipped(self) -> None:
        hosts: list[Any] = [
            "not a host",
            {"type": "console"},
            {"id": "", "reportedState": {"mac": "E063DA88C0BB"}},
            {"id": 7, "reportedState": {"mac": "E063DA88C0BB"}},
            _host(_OTHER_ID, state={"hardware": "not a mapping", "mac": 12}),
            _host(_CLOUD_ID, mac="E063DA88C0BB"),
        ]
        assert cloud_host_id_for_mac(hosts, _CONSOLE_MAC) == _CLOUD_ID


# --- setup -------------------------------------------------------------------------


def _local_entry(*, cloud: bool = True) -> MockConfigEntry:
    data: dict[str, Any] = {
        CONF_CONNECTION_TYPE: CONNECTION_TYPE_LOCAL,
        CONF_HOST: _HOST,
        CONF_API_KEY: _API_KEY,
        CONF_VERIFY_SSL: False,
        CONF_CONSOLE_ID: _CONSOLE_MAC,
    }
    if cloud:
        data[CONF_INNERSPACE_CLOUD_API_KEY] = _CLOUD_KEY
        data[CONF_INNERSPACE_CLOUD_CONSOLE_ID] = _CLOUD_ID
    return MockConfigEntry(
        domain=DOMAIN,
        title="UniFi Insights (CMCoreUDMP Local)",
        data=data,
        unique_id=_CONSOLE_MAC,
    )


def _with_project(client: MagicMock) -> MagicMock:
    client.get_project.return_value = InnerSpaceProject(
        project=InnerSpaceProjectIdentity(id="cmcore-project"),
    )
    client.list_floor_plans.return_value = [
        InnerSpaceFloorPlan(id="fp-1", name="CMCore Floor 1")
    ]
    return client


@pytest.fixture
def innerspace_cls(mock_innerspace_client: MagicMock) -> Generator[MagicMock]:
    """Record how setup builds its InnerSpace client."""
    cls = MagicMock(return_value=_with_project(mock_innerspace_client))
    with patch("custom_components.unifi_insights.UniFiInnerSpaceClient", cls):
        yield cls


@pytest.fixture
def network_cls(mock_network_client: MagicMock) -> Generator[MagicMock]:
    """Record how setup builds its Network client."""
    cls = MagicMock(return_value=mock_network_client)
    with patch("custom_components.unifi_insights.UniFiNetworkClient", cls):
        yield cls


def _reauth_flows(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    return list(entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH}))


@pytest.mark.usefixtures("mock_protect_client", "mock_local_auth")
class TestSetupRoute:
    """Which client reads InnerSpace, and what a cloud failure is allowed to do."""

    async def test_cloud_key_reads_innerspace_through_the_cloud(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        entry = _local_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        kwargs = innerspace_cls.call_args.kwargs
        assert kwargs["connection_type"] is ConnectionType.REMOTE
        assert kwargs["console_id"] == _CLOUD_ID
        assert kwargs["auth"] == ApiKeyAuth(api_key=_CLOUD_KEY)
        assert "base_url" not in kwargs
        # Network stays on the console with the console's own key.
        net = network_cls.call_args.kwargs
        assert net["connection_type"] is ConnectionType.LOCAL
        assert net["base_url"] == _HOST
        data = entry.runtime_data
        assert data.innerspace_coordinator is not None
        assert data.innerspace_coordinator.last_update_success
        assert "fp-1" in data.coordinator.data["innerspace"]["floor_plans"]

    async def test_without_a_cloud_key_innerspace_stays_on_the_console(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        entry = _local_entry(cloud=False)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        kwargs = innerspace_cls.call_args.kwargs
        assert kwargs["connection_type"] is ConnectionType.LOCAL
        assert kwargs["base_url"] == _HOST
        assert "console_id" not in kwargs

    async def test_a_key_without_a_console_id_stays_on_the_console(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        entry = _local_entry()
        data = dict(entry.data)
        del data[CONF_INNERSPACE_CLOUD_CONSOLE_ID]
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, data=data)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert (
            innerspace_cls.call_args.kwargs["connection_type"] is ConnectionType.LOCAL
        )

    async def test_cloud_down_at_setup_loads_and_innerspace_returns_by_itself(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        mock_innerspace_client: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        project = mock_innerspace_client.get_project.return_value
        mock_innerspace_client.get_project.side_effect = UniFiConnectionError(
            "api.ui.com unreachable"
        )
        entry = _local_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Loaded at the first attempt: the cloud does not hold the entry back.
        assert entry.state is ConfigEntryState.LOADED
        coordinator = entry.runtime_data.innerspace_coordinator
        assert coordinator is not None
        assert not coordinator.last_update_success
        assert entry.runtime_data.coordinator.device_available
        mock_innerspace_client.close.assert_not_awaited()

        # The cloud answers again: the next poll brings the floor plan back.
        mock_innerspace_client.get_project.side_effect = None
        mock_innerspace_client.get_project.return_value = project
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert "fp-1" in coordinator.data["floor_plans"]

    async def test_rejected_cloud_key_at_setup_loads_without_innerspace(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        mock_innerspace_client: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        mock_innerspace_client.get_project.side_effect = UniFiAuthenticationError(
            "key revoked"
        )
        entry = _local_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.innerspace_coordinator is None
        mock_innerspace_client.close.assert_awaited()
        assert _reauth_flows(hass, entry) == []

    async def test_empty_cloud_project_loads_without_innerspace(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        mock_innerspace_client: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        mock_innerspace_client.get_project.return_value = InnerSpaceProject()
        mock_innerspace_client.list_floor_plans.return_value = []
        entry = _local_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.innerspace_coordinator is None

    async def test_cloud_key_revoked_later_pauses_innerspace_without_reauth(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        mock_innerspace_client: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        entry = _local_entry()
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.innerspace_coordinator
        assert coordinator is not None

        mock_innerspace_client.get_project.side_effect = UniFiAuthenticationError(
            "key revoked"
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        assert isinstance(coordinator.last_exception, UpdateFailed)
        assert _reauth_flows(hass, entry) == []
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.coordinator.device_available

    async def test_console_down_is_not_loaded_innerspace_only(
        self,
        hass: HomeAssistant,
        innerspace_cls: MagicMock,
        network_cls: MagicMock,
        *,
        mock_network_client: MagicMock,
        mock_protect_client: MagicMock,
        enable_custom_integrations: None,
    ) -> None:
        mock_network_client.sites.get_all.side_effect = UniFiConnectionError("down")
        mock_protect_client.cameras.get_all.side_effect = UniFiConnectionError("down")
        entry = _local_entry()
        entry.add_to_hass(hass)
        # With the partial-answer retries already spent, an InnerSpace answer
        # counted as one of the console's would now load the entry without
        # Network and Protect.
        attempts = hass.data.setdefault(DOMAIN, {}).setdefault(
            "setup_probe_attempts", {}
        )
        attempts[entry.entry_id] = {"partial": SETUP_PROBE_RETRIES}
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The console's own applications decide setup; the cloud cannot stand in.
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert innerspace_cls.call_args.kwargs["connection_type"] is (
            ConnectionType.REMOTE
        )


class TestCoordinatorAuthError:
    """A rejected key: reauth for the entry's own key, a pause for a cloud key."""

    def _coordinator(
        self, hass: HomeAssistant, **kwargs: Any
    ) -> UnifiInsightsInnerSpaceCoordinator:
        entry = _local_entry()
        entry.add_to_hass(hass)
        return UnifiInsightsInnerSpaceCoordinator(
            hass=hass,
            network_client=MagicMock(),
            protect_client=None,
            innerspace_client=MagicMock(),
            entry=entry,
            **kwargs,
        )

    def test_own_key_rejected_starts_reauth(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass)
        with pytest.raises(ConfigEntryAuthFailed):
            coordinator._handle_auth_error(UniFiAuthenticationError("bad key"))

    def test_cloud_key_rejected_fails_the_update(self, hass: HomeAssistant) -> None:
        coordinator = self._coordinator(hass, reauth_on_auth_error=False)
        with pytest.raises(UpdateFailed, match="InnerSpace cloud API key"):
            coordinator._handle_auth_error(UniFiAuthenticationError("bad key"))
        assert coordinator._available is False


# --- reconfigure -----------------------------------------------------------------


def _cm(client: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.fixture
def network_client() -> MagicMock:
    """One Network client for the local validation and the cloud host listing."""
    client = MagicMock()
    client.sites = MagicMock()
    client.sites.get_all = AsyncMock(return_value=[MagicMock(id="default")])
    client.devices = MagicMock()
    client.devices.get_all = AsyncMock(return_value=[])
    client.get_hosts = AsyncMock(return_value=list(_HOSTS))
    client.close = AsyncMock()
    return client


@pytest.fixture
def flow_network_cls(network_client: MagicMock) -> Generator[MagicMock]:
    with (
        patch(
            "custom_components.unifi_insights.config_flow.UniFiNetworkClient",
            return_value=_cm(network_client),
        ) as cls,
        patch("custom_components.unifi_insights.config_flow.LocalAuth"),
    ):
        yield cls


@pytest.fixture
def cloud_innerspace() -> MagicMock:
    client = MagicMock()
    client.get_project = AsyncMock(
        return_value=InnerSpaceProject(project=InnerSpaceProjectIdentity(id="p"))
    )
    for name in ("list_floor_plans", "list_access_points", "list_switches"):
        setattr(client, name, AsyncMock(return_value=[]))
    client.list_inventory = AsyncMock(return_value=[])
    return client


@pytest.fixture
def flow_innerspace_cls(cloud_innerspace: MagicMock) -> Generator[MagicMock]:
    with patch(
        "custom_components.unifi_insights.config_flow.UniFiInnerSpaceClient",
        return_value=_cm(cloud_innerspace),
    ) as cls:
        yield cls


@pytest.fixture
def no_entry_setup() -> Generator[None]:
    """Keep reconfigure's reload from setting the entry up for real."""
    with patch("custom_components.unifi_insights.async_setup_entry", return_value=True):
        yield


async def _reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, **extra: Any
) -> ConfigFlowResult:
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_HOST: _HOST, CONF_API_KEY: _API_KEY, **extra},
    )


@pytest.mark.usefixtures("enable_custom_integrations", "no_entry_setup")
class TestReconfigure:
    """The optional UniFi cloud key for InnerSpace on a local entry."""

    async def test_form_offers_the_key_and_clear_only_when_one_is_stored(
        self, hass: HomeAssistant, flow_network_cls: MagicMock
    ) -> None:
        for entry, clear_offered in (
            (_local_entry(cloud=False), False),
            (_local_entry(), True),
        ):
            entry.add_to_hass(hass)
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={
                    "source": config_entries.SOURCE_RECONFIGURE,
                    "entry_id": entry.entry_id,
                },
            )
            fields = {str(key) for key in result["data_schema"].schema}
            assert CONF_INNERSPACE_CLOUD_API_KEY in fields
            assert (CONF_CLEAR_INNERSPACE_CLOUD_API_KEY in fields) is clear_offered
            hass.config_entries.flow.async_abort(result["flow_id"])
        assert _clear_innerspace_cloud_field(_local_entry(cloud=False)) == {}

    async def test_typed_key_is_matched_to_this_console_and_stored(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
    ) -> None:
        entry = _local_entry(cloud=False)
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: f"  {_CLOUD_KEY} "}
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_INNERSPACE_CLOUD_API_KEY] == _CLOUD_KEY
        assert entry.data[CONF_INNERSPACE_CLOUD_CONSOLE_ID] == _CLOUD_ID
        assert entry.data[CONF_API_KEY] == _API_KEY
        # The cloud listing used the typed key; InnerSpace was read through it.
        remote = [
            call.kwargs
            for call in flow_network_cls.call_args_list
            if call.kwargs.get("connection_type") is ConnectionType.REMOTE
        ]
        assert remote == [
            {
                "auth": ApiKeyAuth(api_key=_CLOUD_KEY),
                "connection_type": ConnectionType.REMOTE,
                "timeout": 30,
            }
        ]
        kwargs = flow_innerspace_cls.call_args.kwargs
        assert kwargs["connection_type"] is ConnectionType.REMOTE
        assert kwargs["console_id"] == _CLOUD_ID
        assert kwargs["auth"] == ApiKeyAuth(api_key=_CLOUD_KEY)

    async def test_console_mac_found_by_validation_is_used(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
    ) -> None:
        # The stored id is a host fallback; the console reports its MAC now.
        gateway = MagicMock(
            spec=["type", "model", "name", "mac", "is_gateway"],
            type="UDMPRO",
            model="UDM Pro",
            name="CMCoreUDMP",
            mac=_CONSOLE_MAC,
            is_gateway=True,
        )
        network_client.devices.get_all.return_value = [gateway]
        entry = _local_entry(cloud=False)
        data = {**entry.data, CONF_CONSOLE_ID: "https://192.168.1.1"}
        entry = MockConfigEntry(
            domain=DOMAIN, data=data, unique_id="https://192.168.1.1"
        )
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_INNERSPACE_CLOUD_CONSOLE_ID] == _CLOUD_ID

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (UniFiAuthenticationError("rejected"), "innerspace_cloud_invalid_auth"),
            (UniFiConnectionError("down"), "innerspace_cloud_cannot_connect"),
            (
                UniFiResponseError("odd", status_code=400),
                "innerspace_cloud_unsupported",
            ),
        ],
    )
    async def test_host_listing_failure_is_a_field_error(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
        *,
        error: Exception,
        expected: str,
    ) -> None:
        network_client.get_hosts.side_effect = error
        entry = _local_entry(cloud=False)
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_INNERSPACE_CLOUD_API_KEY: expected}
        assert CONF_INNERSPACE_CLOUD_API_KEY not in entry.data
        flow_innerspace_cls.assert_not_called()

    async def test_console_not_among_the_keys_consoles(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
    ) -> None:
        network_client.get_hosts.return_value = [_HOSTS[0]]
        entry = _local_entry(cloud=False)
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["errors"] == {
            CONF_INNERSPACE_CLOUD_API_KEY: "innerspace_cloud_console_not_found"
        }
        flow_innerspace_cls.assert_not_called()

    async def test_console_without_a_known_mac_cannot_be_matched(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
    ) -> None:
        data = {
            **_local_entry(cloud=False).data,
            CONF_CONSOLE_ID: "https://192.168.1.1",
        }
        entry = MockConfigEntry(
            domain=DOMAIN, data=data, unique_id="https://192.168.1.1"
        )
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["errors"] == {
            CONF_INNERSPACE_CLOUD_API_KEY: "innerspace_cloud_console_not_found"
        }
        network_client.get_hosts.assert_not_awaited()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (UniFiAuthenticationError("403"), "innerspace_cloud_invalid_auth"),
            (UniFiConnectionError("timeout"), "innerspace_cloud_cannot_connect"),
            (
                UniFiNotFoundError("no app", status_code=404),
                "innerspace_cloud_unsupported",
            ),
        ],
    )
    async def test_innerspace_through_the_cloud_must_answer(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        cloud_innerspace: MagicMock,
        *,
        error: Exception,
        expected: str,
    ) -> None:
        cloud_innerspace.get_project.side_effect = error
        entry = _local_entry(cloud=False)
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["errors"] == {CONF_INNERSPACE_CLOUD_API_KEY: expected}
        assert CONF_INNERSPACE_CLOUD_API_KEY not in entry.data

    async def test_empty_project_through_the_cloud_is_accepted(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        cloud_innerspace: MagicMock,
    ) -> None:
        cloud_innerspace.get_project.return_value = InnerSpaceProject()
        entry = _local_entry(cloud=False)
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY}
        )

        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_INNERSPACE_CLOUD_CONSOLE_ID] == _CLOUD_ID

    async def test_empty_field_keeps_the_stored_key_without_asking_the_cloud(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
    ) -> None:
        entry = _local_entry()
        result = await _reconfigure(
            hass, entry, **{CONF_INNERSPACE_CLOUD_API_KEY: "  "}
        )

        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_INNERSPACE_CLOUD_API_KEY] == _CLOUD_KEY
        assert entry.data[CONF_INNERSPACE_CLOUD_CONSOLE_ID] == _CLOUD_ID
        network_client.get_hosts.assert_not_awaited()
        flow_innerspace_cls.assert_not_called()

    async def test_clear_flag_removes_the_key_and_console(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
    ) -> None:
        entry = _local_entry()
        result = await _reconfigure(
            hass, entry, **{CONF_CLEAR_INNERSPACE_CLOUD_API_KEY: True}
        )

        assert result["reason"] == "reconfigure_successful"
        assert CONF_INNERSPACE_CLOUD_API_KEY not in entry.data
        assert CONF_INNERSPACE_CLOUD_CONSOLE_ID not in entry.data
        assert entry.data[CONF_CONSOLE_ID] == _CONSOLE_MAC

    async def test_typed_key_wins_over_the_clear_flag(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
    ) -> None:
        entry = _local_entry()
        result = await _reconfigure(
            hass,
            entry,
            **{
                CONF_INNERSPACE_CLOUD_API_KEY: "replacement-key",
                CONF_CLEAR_INNERSPACE_CLOUD_API_KEY: True,
            },
        )

        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_INNERSPACE_CLOUD_API_KEY] == "replacement-key"

    async def test_access_error_skips_the_cloud(
        self,
        hass: HomeAssistant,
        flow_network_cls: MagicMock,
        flow_innerspace_cls: MagicMock,
        network_client: MagicMock,
    ) -> None:
        access = MagicMock()
        access.doors.get_all = AsyncMock(side_effect=UniFiAuthenticationError("no"))
        with patch(
            "custom_components.unifi_insights.config_flow.UniFiAccessClient",
            return_value=_cm(access),
        ):
            result = await _reconfigure(
                hass,
                _local_entry(cloud=False),
                **{
                    CONF_ACCESS_TOKEN: "bad-token",
                    CONF_INNERSPACE_CLOUD_API_KEY: _CLOUD_KEY,
                },
            )

        assert result["errors"] == {CONF_ACCESS_TOKEN: "access_invalid_auth"}
        network_client.get_hosts.assert_not_awaited()


def test_diagnostics_redact_the_cloud_key_and_console() -> None:
    entry = _local_entry()
    redacted = async_redact_data(entry.as_dict(), TO_REDACT)
    text = repr(redacted)
    assert _CLOUD_KEY not in text
    assert _CLOUD_ID not in text
