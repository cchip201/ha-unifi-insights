# Copyright 2026 UniFi Insights contributors
"""
Move network devices registered under their MAC to the id the API reports now.

cmcore patch (backlog 1795). A network device is registered as
``(DOMAIN, "<site>_<device id>")`` and its entities carry the same id in their
unique ids. Until the 2026-11-11 UniFi OS update (UDM-Pro 5.1.34 to 6.0.17) the
Network application listed the console and one switch of the CMCoreUDMP site
without a usable ``id``: ``Device.populate_id_fallback`` keyed them on the MAC
(``device_id_is_mac``), while the clients endpoint already named them by their
UUID. Since the update the devices list carries the UUID, the platforms build
``<site>_<uuid>`` and Home Assistant refuses the device ("identifiers or
connections ... are already registered for device ... of the same config
entry"), so every entity of those devices went unavailable.

Deleting the device would drop the entity ids that dashboards use. This module
re-keys the registry rows in place instead: the device keeps its registry id,
area, labels and name; its entities keep their entity ids, names and areas and
only their unique ids change. It matches on the MAC the API reports, whatever
case or separators the registry holds, so it covers any device that was
registered under a MAC, not only these two.

A client entity whose uplink is such a device registers an identifiers-only
device named after the config entry (the "stub") under the UUID form. When that
stub already holds the target identifier, its entities move to the real device
and the stub is removed so the identifier is free.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .console_identity import build_site_unique_id, resolve_console_namespace
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

_LOGGER = logging.getLogger(__name__)

_MAC_SEPARATORS = re.compile(r"[:\-.]")
_MAC_HEX = re.compile(r"[0-9a-f]{12}")


def mac_key(value: object) -> str | None:
    """
    Return a MAC address as 12 lower-case hex digits, or None for anything else.

    The case and the separators (``:``, ``-``, ``.``) do not matter. A UUID (32
    hex digits) and a classic controller id (24) are not MACs.
    """
    if not isinstance(value, str):
        return None
    key = _MAC_SEPARATORS.sub("", value.strip().lower())
    return key if _MAC_HEX.fullmatch(key) else None


def _is_api_device_id(value: object) -> bool:
    """Return True for an integration API id (a UUID): not a MAC, not a classic id."""
    if not isinstance(value, str) or mac_key(value) is not None:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _rewrite_unique_id(
    unique_id: str, site_id: str, old_token: str, new_device_id: str
) -> str | None:
    """
    Return ``unique_id`` with the old device id replaced, or None if it has none.

    The platforms build two shapes from the device id: ``<site>_<id>_<suffix>``
    (most entities) and ``<id>_<suffix>`` (port sensors). The suffix is kept
    verbatim, so the result is what the platform builds for the new id.

    A third shape starts with the same MAC but is not keyed on the device:
    ``<console>_<site>_<suffix>`` names a site entity (vouchers, client totals)
    by the console's MAC, which the platform keeps building whatever id the API
    gives the gateway device. It is left alone (v2026.11.0-cmcore.2 rewrote it
    and orphaned 22 entities; ``_revert_console_namespaced`` undoes that).
    """
    lowered = unique_id.lower()
    keyed_on_site = f"{site_id}_{old_token}_"
    if lowered.startswith(keyed_on_site.lower()):
        return f"{site_id}_{new_device_id}_{unique_id[len(keyed_on_site) :]}"
    keyed_on_device = f"{old_token}_"
    if lowered.startswith(keyed_on_device.lower()):
        rest = unique_id[len(keyed_on_device) :]
        if rest.startswith(f"{site_id}_"):
            return None
        return f"{new_device_id}_{rest}"
    return None


def _is_client_stub(device: DeviceEntry, entry_id: str) -> bool:
    """
    Return True for a device only a client entity's uplink identifiers created.

    Such a device has the entry's title as its name and nothing else: no
    connection, manufacturer or model, and no name chosen by the user.
    """
    return (
        device.config_entry_id == entry_id
        and not device.connections
        and not device.manufacturer
        and not device.model
        and device.name_by_user is None
    )


def _merge_stub_into(
    hass: HomeAssistant, stub: DeviceEntry, target: DeviceEntry
) -> bool:
    """
    Move every entity of ``stub`` to ``target`` and remove ``stub``.

    The entities move first: removing a device detaches the entities it holds.
    Return False, leaving the stub in place, when an entity cannot move.
    """
    entity_registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_device(
        entity_registry, stub.id, include_disabled_entities=True
    ):
        try:
            entity_registry.async_update_entity(
                reg_entry.entity_id, device_id=target.id
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "Device id migration: cannot move %s to device %s: %s",
                reg_entry.entity_id,
                target.id,
                err,
            )
            return False
    dr.async_get(hass).async_remove_device(stub.id)
    return True


@callback
def async_migrate_mac_device_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
    devices_by_site: object,
) -> int:
    """
    Re-key the entry's MAC-registered network devices to the ids the API reports.

    ``devices_by_site`` is the device coordinator's ``{site: {device id: device}}``.
    A registry device of ``entry`` whose identifier is ``<site>_<MAC>`` is moved
    to ``<site>_<device id>`` when the API lists that MAC under a UUID id, and
    the rows of its entities whose unique id holds the MAC follow. Return the
    number of devices moved.
    """
    if not isinstance(devices_by_site, Mapping):
        return 0
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    migrated = 0

    for site_id, site_devices in devices_by_site.items():
        if not isinstance(site_devices, Mapping):
            continue
        prefix = f"{site_id}_"
        for device_id, device in site_devices.items():
            if not isinstance(device, Mapping) or not _is_api_device_id(device_id):
                continue
            key = mac_key(device.get("macAddress") or device.get("mac"))
            if key is None:
                continue

            new_identifier = f"{prefix}{device_id}"
            found = _find_mac_device(device_registry, entry.entry_id, prefix, key)
            if found is None:
                moved_device = device_registry.async_get_device_by_identifier(
                    (DOMAIN, new_identifier), entry.entry_id
                )
                if moved_device is not None:
                    _revert_console_namespaced(
                        entity_registry,
                        entry,
                        moved_device.id,
                        site_id=site_id,
                        device_id=device_id,
                        key=key,
                    )
                continue
            old, old_identifier = found

            holder = device_registry.async_get_device_by_identifier(
                (DOMAIN, new_identifier), entry.entry_id
            )
            if holder is not None and holder.id != old.id:
                if not _is_client_stub(holder, entry.entry_id):
                    _LOGGER.warning(
                        "Device id migration: %s is already registered for "
                        "device %s; leaving %s as it is",
                        new_identifier,
                        holder.id,
                        old_identifier,
                    )
                    continue
                if not _merge_stub_into(hass, holder, old):
                    continue

            try:
                device_registry.async_update_device(
                    old.id,
                    new_identifiers=(old.identifiers - {(DOMAIN, old_identifier)})
                    | {(DOMAIN, new_identifier)},
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning(
                    "Device id migration: cannot move device %s from %s to %s: %s",
                    old.id,
                    old_identifier,
                    new_identifier,
                    err,
                )
                continue

            moved = _rewrite_entities(
                entity_registry,
                entry.entry_id,
                old.id,
                site_id=site_id,
                old_token=old_identifier[len(prefix) :],
                new_device_id=device_id,
            )
            migrated += 1
            _LOGGER.info(
                "Device id migration: moved device %s from %s to %s (%d entities)",
                old.id,
                old_identifier,
                new_identifier,
                moved,
            )
    return migrated


def _revert_console_namespaced(
    entity_registry: er.EntityRegistry,
    entry: ConfigEntry,
    registry_device_id: str,
    *,
    site_id: str,
    device_id: str,
    key: str,
) -> int:
    """
    Undo the cmcore.2 rewrite of site entities named by the console's MAC.

    v2026.11.0-cmcore.2 turned ``<console MAC>_<site>_<suffix>`` into
    ``<device id>_<site>_<suffix>``, which no platform builds, so those entities
    went unavailable and the platforms added copies with a ``_2`` entity id.
    For the console's own device, put each unique id back to the namespaced
    form and drop the copy that holds it, so the original keeps its entity id.
    Return how many unique ids were put back.
    """
    namespace = resolve_console_namespace(entry)
    if namespace is None or mac_key(namespace) != key:
        return 0
    wrong = f"{device_id}_{site_id}_"
    reverted = 0
    for reg_entry in er.async_entries_for_device(
        entity_registry, registry_device_id, include_disabled_entities=True
    ):
        if (
            reg_entry.platform != DOMAIN
            or reg_entry.config_entry_id != entry.entry_id
            or not reg_entry.unique_id.startswith(wrong)
        ):
            continue
        target = build_site_unique_id(
            namespace, site_id, reg_entry.unique_id[len(wrong) :]
        )
        copy_id = entity_registry.async_get_entity_id(reg_entry.domain, DOMAIN, target)
        if copy_id is not None:
            copy = entity_registry.async_get(copy_id)
            if copy is None or copy.config_entry_id != entry.entry_id:
                _LOGGER.warning(
                    "Device id migration: %s holds %s; leaving %s as it is",
                    copy_id,
                    target,
                    reg_entry.entity_id,
                )
                continue
            entity_registry.async_remove(copy_id)
        try:
            entity_registry.async_update_entity(
                reg_entry.entity_id, new_unique_id=target
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "Device id migration: cannot put back %s (%s): %s",
                reg_entry.entity_id,
                reg_entry.unique_id,
                err,
            )
            continue
        reverted += 1
    if reverted:
        _LOGGER.info(
            "Device id migration: put %d console-namespaced unique ids back on %s",
            reverted,
            registry_device_id,
        )
    return reverted


def _find_mac_device(
    device_registry: dr.DeviceRegistry, entry_id: str, prefix: str, key: str
) -> tuple[DeviceEntry, str] | None:
    """Return the entry's device registered as ``<site>_<MAC>`` for ``key``."""
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        for domain, identifier in device.identifiers:
            if (
                domain == DOMAIN
                and identifier.startswith(prefix)
                and mac_key(identifier[len(prefix) :]) == key
            ):
                return device, identifier
    return None


def _rewrite_entities(
    entity_registry: er.EntityRegistry,
    entry_id: str,
    registry_device_id: str,
    *,
    site_id: str,
    old_token: str,
    new_device_id: str,
) -> int:
    """Rewrite the unique ids of a device's entities; return how many changed."""
    changed = 0
    for reg_entry in er.async_entries_for_device(
        entity_registry, registry_device_id, include_disabled_entities=True
    ):
        if reg_entry.platform != DOMAIN or reg_entry.config_entry_id != entry_id:
            continue
        new_unique_id = _rewrite_unique_id(
            reg_entry.unique_id, site_id, old_token, new_device_id
        )
        if new_unique_id is None:
            continue
        try:
            entity_registry.async_update_entity(
                reg_entry.entity_id, new_unique_id=new_unique_id
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "Device id migration: cannot re-key %s (%s): %s",
                reg_entry.entity_id,
                reg_entry.unique_id,
                err,
            )
            continue
        changed += 1
    return changed
