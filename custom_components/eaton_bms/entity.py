"""Shared panel device metadata and availability."""
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class BmsEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, key):
        super().__init__(coordinator)
        entry = coordinator.config_entry
        system = coordinator.data["system"]
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=system.get("name", "Eaton alarm"),
            manufacturer="Eaton / Scantronic", model=system.get("type", "i-on"),
        )

    @property
    def available(self):
        return super().available and self.coordinator.stream_connected
