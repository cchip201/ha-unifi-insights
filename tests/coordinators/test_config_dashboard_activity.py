# Copyright 2026 UniFi Insights contributors
"""Tests for the dashboard feed behind the internet activity sensors (cmcore, 1813)."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.unifi_insights.coordinators.config import (
    UnifiInsightsConfigCoordinator,
)
from custom_components.unifi_insights.coordinators.config_sections import (
    DASHBOARD_HISTORY_WINDOWS,
    aggregate_dashboard_activity_windows,
    async_call_dashboard_history,
    async_fetch_site_internet_activity,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant.common import MockConfigEntry

NOW_MS = 1_794_426_000_000
FIVE_MIN = 300_000
HOUR = 3_600_000
EIGHT_HOURS = 28_800_000


def _history(
    step_ms: int, count: int, rx_bps: float = 8_000.0, tx_bps: float = 800.0
) -> list[dict[str, Any]]:
    """Buckets ending at the one that starts at ``NOW_MS`` (oldest first)."""
    return [
        {
            "timestamp": NOW_MS - (count - 1 - i) * step_ms,
            "avg_rx_rate_bps": rx_bps,
            "avg_tx_rate_bps": tx_bps,
        }
        for i in range(count)
    ]


def test_dashboard_windows_turn_rates_into_bytes() -> None:
    """Each full bucket adds rate x seconds / 8; the open bucket counts what has run."""
    now_ms = NOW_MS + 60_000  # the newest bucket has run for a minute
    windows = aggregate_dashboard_activity_windows(
        {
            "1d": _history(FIVE_MIN, 288),
            "1w": _history(HOUR, 168),
            "1m": _history(EIGHT_HOURS, 90),
        },
        now_ms=now_ms,
    )

    rx_open = 8_000 * 60 / 8
    tx_open = 800 * 60 / 8
    # 1 h: the newest 12 five-minute buckets, 11 full and the open one
    assert windows["1h"]["rx_bytes"] == round(11 * 8_000 * 300 / 8 + rx_open)
    assert windows["1h"]["tx_bytes"] == round(11 * 800 * 300 / 8 + tx_open)
    # 1 d: all 288 buckets, 287 full and the open one
    assert windows["1d"]["rx_bytes"] == round(287 * 8_000 * 300 / 8 + rx_open)
    # 1 w: 168 hourly buckets, 167 full and the open one
    assert windows["1w"]["rx_bytes"] == round(167 * 8_000 * 3_600 / 8 + rx_open)
    # 1 m: 90 eight-hour buckets, 89 full and the open one
    assert windows["1m"]["rx_bytes"] == round(89 * 8_000 * 28_800 / 8 + rx_open)
    assert set(windows) == {"1h", "1d", "1w", "1m"}


def test_dashboard_windows_omit_what_has_no_usable_bucket() -> None:
    """A window without a numeric rate is left out, never published as zero."""
    junk: list[Any] = [
        "not a bucket",
        {"timestamp": True, "avg_rx_rate_bps": 5.0},
        {"timestamp": "yesterday", "avg_rx_rate_bps": 5.0},
        {"timestamp": NOW_MS - FIVE_MIN, "avg_rx_rate_bps": None},
    ]
    assert aggregate_dashboard_activity_windows({"1d": junk}, now_ms=NOW_MS) == {}
    assert aggregate_dashboard_activity_windows({}, now_ms=NOW_MS) == {}
    # one direction missing: only the other is reported
    one_way = [{"timestamp": NOW_MS - FIVE_MIN, "avg_tx_rate_bps": 8.0}]
    assert aggregate_dashboard_activity_windows({"1d": one_way}, now_ms=NOW_MS)[
        "1h"
    ] == {"tx_bytes": 300}
    # a rate that is not a number does not break the other direction
    mixed = [
        {
            "timestamp": NOW_MS - FIVE_MIN,
            "avg_rx_rate_bps": 16.0,
            "avg_tx_rate_bps": "x",
        }
    ]
    assert aggregate_dashboard_activity_windows({"1d": mixed}, now_ms=NOW_MS)["1h"] == {
        "rx_bytes": 600
    }


def test_dashboard_windows_use_the_default_step_for_one_bucket() -> None:
    """One bucket has no neighbour to measure, so the window's usual step applies."""
    one = [
        {"timestamp": NOW_MS - 600_000, "avg_rx_rate_bps": 8.0, "avg_tx_rate_bps": 8.0}
    ]
    windows = aggregate_dashboard_activity_windows({"1d": one}, now_ms=NOW_MS)
    assert windows["1h"] == {"rx_bytes": 300, "tx_bytes": 300}


def test_dashboard_windows_read_the_step_from_the_data() -> None:
    """Buckets that are not the usual size are summed at their own size."""
    ten_min = _history(2 * FIVE_MIN, 6, rx_bps=8.0, tx_bps=8.0)
    windows = aggregate_dashboard_activity_windows({"1d": ten_min}, now_ms=NOW_MS)
    # five 600 s buckets inside the hour (the sixth starts exactly at NOW_MS: 0 s)
    assert windows["1h"]["rx_bytes"] == 5 * 600


class _Client:
    """A Network client that only knows how to build a legacy path and GET."""

    def __init__(self, legacy_path: str, response: Any) -> None:
        self._legacy_path = legacy_path
        self._get = AsyncMock(return_value=response)

    def build_legacy_api_path(self, site_name: str, endpoint: str) -> str:
        assert site_name == "default"
        assert endpoint == "/x"
        return self._legacy_path


DASHBOARD = {
    "wan_activity": {
        "total_activity": {"history": [{"timestamp": 1, "avg_rx_rate_bps": 2.0}]}
    }
}


@pytest.mark.parametrize(
    ("legacy_path", "expected_path"),
    [
        (
            "/proxy/network/api/s/default/x",
            "/proxy/network/v2/api/site/default/aggregated-dashboard",
        ),
        (
            "/v1/connector/consoles/abc/network/api/s/default/x",
            "/v1/connector/consoles/abc/network/v2/api/site/default/aggregated-dashboard",
        ),
    ],
)
async def test_call_dashboard_history_builds_the_v2_path(
    legacy_path: str, expected_path: str
) -> None:
    """Local and cloud-connector paths both map to the dashboard feed."""
    client = _Client(legacy_path, DASHBOARD)

    history = await async_call_dashboard_history(client, "default", 86_400_000)

    assert history == [{"timestamp": 1, "avg_rx_rate_bps": 2.0}]
    client._get.assert_awaited_once_with(
        expected_path,
        params={"historySeconds": 86_400},
        expected_unsupported=True,
        log_body=False,
    )


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"wan_activity": "x"},
        {"wan_activity": {}},
        {"wan_activity": {"total_activity": []}},
        {"wan_activity": {"total_activity": {"history": "x"}}},
    ],
)
async def test_call_dashboard_history_tolerates_other_shapes(response: Any) -> None:
    """Anything but a history list reads as no history."""
    client = _Client("/proxy/network/api/s/default/x", response)
    assert await async_call_dashboard_history(client, "default", 3_600_000) == []


@pytest.mark.parametrize(
    "client",
    [
        object(),
        MagicMock(spec=["build_legacy_api_path"]),
        _Client("/somewhere/else", DASHBOARD),
    ],
    ids=["no-methods", "no-get", "unexpected-path"],
)
async def test_call_dashboard_history_gives_up_quietly(client: Any) -> None:
    """A client that cannot name the route is not asked."""
    assert await async_call_dashboard_history(client, "default", 3_600_000) == []


async def _optional(
    _section: str, _site_id: str, fetch: Callable[[], Awaitable[list[Any]]]
) -> list[Any] | None:
    return await fetch()


async def test_fetch_falls_back_to_the_dashboard_when_reports_are_empty() -> None:
    """Empty classic reports (Network 11.0.99) hand over to the dashboard feed."""
    reports = AsyncMock(return_value=[])
    seen: list[int] = []

    async def dashboard(site_name: str, window_ms: int) -> list[Any]:
        assert site_name == "default"
        seen.append(window_ms)
        step = {86_400_000: FIVE_MIN, 604_800_000: HOUR, 2_592_000_000: EIGHT_HOURS}[
            window_ms
        ]
        return _history(step, window_ms // step, rx_bps=80.0, tx_bps=8.0)

    result = await async_fetch_site_internet_activity(
        _optional, reports, "site1", "default", NOW_MS, call_dashboard_history=dashboard
    )

    assert seen == list(DASHBOARD_HISTORY_WINDOWS.values())
    assert result is not None
    assert set(result) == {"1h", "1d", "1w", "1m"}
    # the newest bucket starts at "now" and has not run yet: 12 full buckets in the hour
    assert result["1h"] == {
        "rx_bytes": 12 * 80 * 300 // 8,
        "tx_bytes": 12 * 8 * 300 // 8,
    }


async def test_fetch_keeps_the_classic_reports_when_they_have_data() -> None:
    """The dashboard feed is not asked while the classic reports answer."""

    async def reports(
        _site: str, _interval: str, *, start_ms: int, end_ms: int
    ) -> list[dict[str, int]]:
        return [{"time": end_ms, "wan-rx_bytes": 4096, "wan-tx_bytes": 2048}]

    dashboard = AsyncMock(return_value=[])

    result = await async_fetch_site_internet_activity(
        _optional, reports, "site1", "default", NOW_MS, call_dashboard_history=dashboard
    )

    assert result is not None
    assert result["1h"] == {"rx_bytes": 4096, "tx_bytes": 2048}
    dashboard.assert_not_awaited()


async def test_fetch_without_a_dashboard_call_keeps_the_empty_answer() -> None:
    """No fallback is configured: empty reports stay empty."""
    result = await async_fetch_site_internet_activity(
        _optional, AsyncMock(return_value=[]), "site1", "default", NOW_MS
    )
    assert result == {}


async def test_fetch_keeps_the_empty_answer_when_the_dashboard_is_refused() -> None:
    """A refused dashboard call leaves the sensors as they were, not broken."""
    dashboard = AsyncMock(return_value=[])

    async def refuse_dashboard(
        _section: str, _site_id: str, fetch: Callable[[], Awaitable[list[Any]]]
    ) -> list[Any] | None:
        if isinstance(fetch, partial) and fetch.func is dashboard:
            return None
        return await fetch()

    result = await async_fetch_site_internet_activity(
        refuse_dashboard,
        AsyncMock(return_value=[]),
        "site1",
        "default",
        NOW_MS,
        call_dashboard_history=dashboard,
    )

    assert result == {}
    dashboard.assert_not_awaited()


async def test_coordinator_reads_the_dashboard_through_its_client(
    hass: HomeAssistant,
    mock_network_client: MagicMock,
    mock_protect_client: MagicMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The coordinator hands its Network client to the dashboard call."""
    coordinator = UnifiInsightsConfigCoordinator(
        hass=hass,
        network_client=mock_network_client,
        protect_client=mock_protect_client,
        entry=mock_config_entry,
    )
    mock_network_client.build_legacy_api_path = MagicMock(
        return_value="/proxy/network/api/s/default/x"
    )
    mock_network_client._get = AsyncMock(return_value=DASHBOARD)

    history = await coordinator._call_dashboard_history("default", 3_600_000)

    assert history == [{"timestamp": 1, "avg_rx_rate_bps": 2.0}]
