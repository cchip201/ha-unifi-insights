# Copyright 2026 UniFi Insights contributors
"""Tests for the Site Manager ISP metric sensors (latency, loss, ISP name, ASN)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime

from custom_components.unifi_insights.coordinators.site_manager import (
    _latest_isp_metrics,
)
from custom_components.unifi_insights.sensor import (
    SITE_ISP_METRIC_SENSOR_TYPES,
    UnifiSiteIspMetricSensor,
    _discover_site_isp_sensors,
    _isp_field,
    _isp_metric_sites,
    _isp_sample,
    _name_keys,
)
from tests.fixtures.facade_data import facade_data

# One row of GET /v1/isp-metrics/5m as the live API answered it on 2026-11-07
# (a host with a single site); identifiers are placeholders.
LIVE_ROW: dict[str, Any] = {
    "metricType": "5m",
    "hostId": "host-1",
    "siteId": "590502",
    "periods": [
        {
            "metricTime": "2026-11-07T23:00:00Z",
            "version": "1",
            "data": {
                "wan": {
                    "avgLatency": 3,
                    "download_kbps": 900000,
                    "downtime": 0,
                    "ispAsn": "20055",
                    "ispName": "Ziply Fiber",
                    "maxLatency": 4,
                    "packetLoss": 0,
                    "upload_kbps": 900000,
                    "uptime": 100,
                }
            },
        },
        {
            "metricTime": "2026-11-07T23:05:00Z",
            "version": "1",
            "data": {
                "wan": {
                    "avgLatency": 5,
                    "download_kbps": 900000,
                    "downtime": 0,
                    "ispAsn": "20055",
                    "ispName": "Ziply Fiber",
                    "maxLatency": 9,
                    "packetLoss": 2,
                    "upload_kbps": 900000,
                    "uptime": 100,
                }
            },
        },
    ],
}


def _snapshot(
    *,
    rows: list[dict[str, Any]] | None = None,
    manager_sites: dict[str, Any] | None = None,
    selected: str | None = "host-1",
    available: bool = True,
) -> dict[str, Any]:
    """Build the Site Manager part of a facade snapshot from API rows."""
    return {
        "hosts": {"host-1": {"id": "host-1"}},
        "sites": manager_sites if manager_sites is not None else {},
        "devices": {},
        "isp_metrics": _latest_isp_metrics([LIVE_ROW] if rows is None else rows),
        "sd_wan_configs": {},
        "collections": {
            "isp_metrics": {"available": available, "updated_at": None, "error": None}
        },
        "last_attempt": None,
        "cooldown_until": None,
        "selected_host_id": selected,
    }


def _coordinator(
    site_manager: dict[str, Any] | None,
    network_sites: dict[str, Any] | None = None,
    *,
    last_update_success: bool = True,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.last_update_success = last_update_success
    sections: dict[str, Any] = {
        "sites": network_sites
        if network_sites is not None
        else {"net-1": {"name": "Default"}}
    }
    if site_manager is not None:
        sections["site_manager"] = site_manager
    coordinator.data = facade_data(
        site_ids=tuple(sections["sites"]),
        **sections,
    )
    return coordinator


def _by_key(entities: list[Any]) -> dict[str, UnifiSiteIspMetricSensor]:
    return {entity.entity_description.key: entity for entity in entities}


# --- the response shape through the coordinator and out as five sensors ---


def test_live_response_becomes_five_sensors_with_the_newest_values() -> None:
    """The proven isp-metrics row yields latency, loss, ISP name and ASN."""
    coordinator = _coordinator(_snapshot())
    entities = _discover_site_isp_sensors(coordinator, set(), namespace="console")

    sensors = _by_key(entities)
    assert set(sensors) == {
        "isp_avg_latency",
        "isp_max_latency",
        "isp_packet_loss",
        "isp_name",
        "isp_asn",
    }
    # The 23:05 period is newer than the 23:00 one.
    assert sensors["isp_avg_latency"].native_value == 5
    assert sensors["isp_max_latency"].native_value == 9
    assert sensors["isp_packet_loss"].native_value == 2
    assert sensors["isp_name"].native_value == "Ziply Fiber"
    assert sensors["isp_asn"].native_value == "20055"
    assert sensors["isp_asn"].extra_state_attributes == {
        "metric_time": "2026-11-07T23:05:00+00:00"
    }
    assert all(sensor.available for sensor in entities)
    assert {sensor.unique_id for sensor in entities} == {
        f"console_590502_{description.key}"
        for description in SITE_ISP_METRIC_SENSOR_TYPES
    }
    # Grouped under the Network site's device, not under a new one.
    for sensor in entities:
        assert sensor.device_info is not None
        assert sensor.device_info["identifiers"] == {
            ("unifi_insights", "site_net-1"),
        }


def test_descriptions_carry_units_and_classes() -> None:
    """Latency is a duration in milliseconds and loss a percentage."""
    by_key = {
        description.key: description for description in SITE_ISP_METRIC_SENSOR_TYPES
    }
    for key in ("isp_avg_latency", "isp_max_latency"):
        assert by_key[key].device_class is SensorDeviceClass.DURATION
        assert by_key[key].native_unit_of_measurement == UnitOfTime.MILLISECONDS
    assert by_key["isp_packet_loss"].native_unit_of_measurement == PERCENTAGE
    assert by_key["isp_name"].text
    assert by_key["isp_asn"].text
    assert by_key["isp_name"].state_class is None
    # ISP name and AS number are facts about the line, not measurements.
    assert by_key["isp_name"].entity_category is EntityCategory.DIAGNOSTIC
    assert by_key["isp_asn"].entity_category is EntityCategory.DIAGNOSTIC
    # Only the maximum latency is off until someone wants it.
    assert {
        key
        for key, description in by_key.items()
        if not description.entity_registry_enabled_default
    } == {"isp_max_latency"}


def test_discovery_runs_again_without_new_entities() -> None:
    """A second pass over the same snapshot adds nothing."""
    coordinator = _coordinator(_snapshot())
    known: set[tuple[str, ...]] = set()
    assert len(_discover_site_isp_sensors(coordinator, known, namespace="c")) == 5
    assert _discover_site_isp_sensors(coordinator, known, namespace="c") == []


def test_a_field_the_period_lacks_gets_no_sensor() -> None:
    """Only the fields a period carries become sensors."""
    row = {
        **LIVE_ROW,
        "periods": [
            {
                "metricTime": "2026-11-07T23:00:00Z",
                "data": {"wan": {"avgLatency": 3, "packetLoss": 0}},
            }
        ],
    }
    coordinator = _coordinator(_snapshot(rows=[row]))
    entities = _discover_site_isp_sensors(coordinator, set(), namespace="c")
    assert {e.entity_description.key for e in entities} == {
        "isp_avg_latency",
        "isp_packet_loss",
    }
    # A real 0 is a value, not a missing field.
    assert _by_key(entities)["isp_packet_loss"].native_value == 0


def test_without_a_console_namespace_the_legacy_identity_is_used() -> None:
    """An entry with no console id still gets a stable identity."""
    coordinator = _coordinator(_snapshot())
    entities = _discover_site_isp_sensors(coordinator, set(), namespace=None)
    assert {e.unique_id for e in entities} == {
        f"590502_{description.key}" for description in SITE_ISP_METRIC_SENSOR_TYPES
    }


def test_an_entry_without_site_manager_has_no_isp_sensors() -> None:
    """A local entry has no Site Manager section at all."""
    coordinator = _coordinator(None)
    assert _discover_site_isp_sensors(coordinator, set(), namespace="c") == []


# --- availability ---


def test_sensors_go_unavailable_when_site_manager_fails() -> None:
    """A failed isp_metrics collection makes the sensors unavailable."""
    coordinator = _coordinator(_snapshot())
    sensor = _by_key(_discover_site_isp_sensors(coordinator, set(), namespace="c"))[
        "isp_avg_latency"
    ]
    assert sensor.available
    coordinator.data = _coordinator(_snapshot(available=False)).data
    assert not sensor.available


def test_sensors_go_unavailable_when_the_coordinator_fails() -> None:
    """The facade's own failure also makes them unavailable."""
    coordinator = _coordinator(_snapshot())
    sensor = _by_key(_discover_site_isp_sensors(coordinator, set(), namespace="c"))[
        "isp_name"
    ]
    coordinator.last_update_success = False
    assert not sensor.available


def test_sensor_is_unavailable_when_its_sample_disappears() -> None:
    """An empty hour of metrics leaves no value, so no state."""
    coordinator = _coordinator(_snapshot())
    sensor = _by_key(_discover_site_isp_sensors(coordinator, set(), namespace="c"))[
        "isp_max_latency"
    ]
    coordinator.data = _coordinator(_snapshot(rows=[])).data
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
    assert not sensor.available


@pytest.mark.parametrize(
    "collections",
    [None, "bad", {}, {"isp_metrics": "bad"}, {"isp_metrics": {"available": "yes"}}],
)
def test_sensor_needs_a_well_formed_collection_state(collections: object) -> None:
    """Anything but ``available: True`` for isp_metrics reads unavailable."""
    coordinator = _coordinator(_snapshot())
    sensor = _by_key(_discover_site_isp_sensors(coordinator, set(), namespace="c"))[
        "isp_name"
    ]
    broken = {**_snapshot(), "collections": collections}
    coordinator.data = _coordinator(broken).data
    assert not sensor.available


def test_available_with_no_site_manager_section_is_false() -> None:
    """A snapshot that lost the section reads unavailable, never raises."""
    coordinator = _coordinator(_snapshot())
    sensor = _by_key(_discover_site_isp_sensors(coordinator, set(), namespace="c"))[
        "isp_name"
    ]
    coordinator.data = _coordinator(None).data
    assert not sensor.available
    coordinator.data = None
    assert not sensor.available
    assert sensor.native_value is None


# --- pairing a Site Manager site with a Network site ---


def test_name_keys_reads_internal_reference_name_and_meta_name() -> None:
    assert _name_keys("not a record") == set()
    assert _name_keys({}) == set()
    assert _name_keys(
        {"internalReference": "Default", "name": "Home", "meta": {"name": "HQ"}}
    ) == {"default", "home", "hq"}
    assert _name_keys({"meta": "bad", "name": ""}) == set()


def test_pairing_needs_a_site_manager_section_and_a_selected_host() -> None:
    coordinator = _coordinator(_snapshot())
    assert _isp_metric_sites(coordinator.data) == {"590502": "net-1"}
    assert _isp_metric_sites(_coordinator(None).data) == {}
    assert _isp_metric_sites(_coordinator(_snapshot(selected=None)).data) == {}
    assert _isp_metric_sites(_coordinator(_snapshot(selected="other")).data) == {}
    no_metrics = {**_snapshot(), "isp_metrics": "bad"}
    assert _isp_metric_sites(_coordinator(no_metrics).data) == {}
    assert _isp_metric_sites(_coordinator(_snapshot(), network_sites={}).data) == {}


def test_pairing_by_name_when_a_console_has_several_sites() -> None:
    """With two metered sites each pairs with the Network site of its name."""
    rows = [
        LIVE_ROW,
        {**LIVE_ROW, "siteId": "69dc99"},
        {**LIVE_ROW, "siteId": "no-name-match"},
        {**LIVE_ROW, "siteId": "no-record"},
    ]
    manager_sites = {
        "590502": {"meta": {"name": "default"}},
        "69dc99": {"meta": {"name": "Branch"}},
        "no-name-match": {"meta": {"name": "elsewhere"}},
    }
    network = {
        "net-1": {"name": "Default Site", "internalReference": "default"},
        "net-2": {"name": "Branch"},
    }
    coordinator = _coordinator(
        _snapshot(rows=rows, manager_sites=manager_sites), network
    )
    assert _isp_metric_sites(coordinator.data) == {
        "590502": "net-1",
        "69dc99": "net-2",
    }
    # Site Manager's list of sites missing or malformed: nothing can pair.
    coordinator = _coordinator(_snapshot(rows=rows, manager_sites={}), network)
    assert _isp_metric_sites(coordinator.data) == {}
    broken = {**_snapshot(rows=rows), "sites": "bad"}
    assert _isp_metric_sites(_coordinator(broken, network).data) == {}


def test_one_metered_site_and_many_network_sites_pairs_by_name_only() -> None:
    network = {"net-1": {"name": "Home"}, "net-2": {"name": "Default"}}
    manager_sites = {"590502": {"meta": {"name": "default"}}}
    coordinator = _coordinator(_snapshot(manager_sites=manager_sites), network)
    assert _isp_metric_sites(coordinator.data) == {"590502": "net-2"}
    unmatched = _coordinator(_snapshot(manager_sites={}), network)
    assert _isp_metric_sites(unmatched.data) == {}


# --- readers ---


def test_isp_sample_reader_tolerates_malformed_snapshots() -> None:
    good = _coordinator(_snapshot()).data
    assert _isp_sample(good, "590502") is not None
    assert _isp_sample(good, "missing") is None
    assert _isp_sample(None, "590502") is None
    assert _isp_sample({}, "590502") is None
    assert _isp_sample({"site_manager": {"isp_metrics": "bad"}}, "590502") is None
    assert (
        _isp_sample(
            {
                "site_manager": {
                    "isp_metrics": {"host-1": "bad"},
                    "selected_host_id": "host-1",
                }
            },
            "590502",
        )
        is None
    )
    assert (
        _isp_sample(
            {
                "site_manager": {
                    "isp_metrics": {"host-1": {"590502": "bad"}},
                    "selected_host_id": "host-1",
                }
            },
            "590502",
        )
        is None
    )


def test_isp_field_checks_the_type_of_each_value() -> None:
    sample = {"wan": {"avgLatency": 3, "ispName": "Ziply Fiber", "flag": True}}
    assert _isp_field(sample, "avgLatency", text=False) == 3
    assert _isp_field(sample, "ispName", text=True) == "Ziply Fiber"
    # a number read as text, text read as a number, a boolean, a missing key
    assert _isp_field(sample, "avgLatency", text=True) is None
    assert _isp_field(sample, "ispName", text=False) is None
    assert _isp_field(sample, "flag", text=False) is None
    assert _isp_field(sample, "missing", text=False) is None
    assert _isp_field({"wan": "bad"}, "avgLatency", text=False) is None
    assert _isp_field(None, "avgLatency", text=False) is None
    assert _isp_field({"wan": {"ispName": ""}}, "ispName", text=True) is None
