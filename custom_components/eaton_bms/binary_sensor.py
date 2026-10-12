"""Live zones, readiness, panel problems, individual faults and connection status."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from .api import blocking_reasons, equipment_faults, fault_description
from .const import CONF_ZONE_TYPES
from .entity import BmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = {group: set() for group in ("zones", "outputs", "partitions", "faults")}
    async_add_entities([ConnectionSensor(coordinator), PanelProblem(coordinator)])
    def discover():
        entities = []
        for group in known:
            items = equipment_faults(coordinator.data) if group == "faults" else coordinator.data[group]
            fresh = set(items) - known[group]
            known[group].update(fresh)
            for identifier in fresh:
                if group == "zones":
                    entities.extend([ZoneSensor(coordinator, identifier, False), ZoneSensor(coordinator, identifier, True)])
                elif group == "outputs":
                    entities.append(OutputSensor(coordinator, identifier))
                elif group == "partitions":
                    entities.append(ReadySensor(coordinator, identifier))
                else:
                    entities.append(FaultSensor(coordinator, identifier))
        if entities:
            async_add_entities(entities)
    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class ZoneSensor(BmsEntity, BinarySensorEntity):
    def __init__(self, coordinator, identifier, tamper):
        super().__init__(coordinator, f"zone_{identifier}_{'tamper' if tamper else 'motion'}")
        self.identifier, self.tamper = identifier, tamper
        self._attr_name = coordinator.data["zones"][identifier].get("name", "Zone " + identifier)
        if tamper:
            self._attr_name += " tamper"
            self._attr_device_class = BinarySensorDeviceClass.TAMPER
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        else:
            kind = coordinator.config_entry.options.get(CONF_ZONE_TYPES, {}).get(identifier, "motion")
            self._attr_device_class = {"motion": BinarySensorDeviceClass.MOTION,
                "door": BinarySensorDeviceClass.DOOR, "window": BinarySensorDeviceClass.WINDOW,
                "opening": BinarySensorDeviceClass.OPENING}.get(kind, BinarySensorDeviceClass.MOTION)

    @property
    def name(self):
        zone = self.coordinator.data["zones"].get(self.identifier, {})
        return zone.get("name", self._attr_name.removesuffix(" tamper")) + (" tamper" if self.tamper else "")

    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["zones"]

    @property
    def is_on(self):
        return self.coordinator.data["zones"].get(self.identifier, {}).get("tamper" if self.tamper else "open")

    @property
    def extra_state_attributes(self):
        zone = self.coordinator.data["zones"].get(self.identifier, {})
        return {"zone_id": self.identifier, "partitions": zone.get("partitions", []),
                "omitted": zone.get("omitted", False)}


class OutputSensor(BmsEntity, BinarySensorEntity):
    _attr_icon = "mdi:electric-switch"
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"output_{identifier}")
        self.identifier = identifier
        self._attr_name = coordinator.data["outputs"][identifier].get("name", "Output " + identifier) + " output"
    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["outputs"]
    @property
    def is_on(self):
        return self.coordinator.data["outputs"].get(self.identifier, {}).get("state") == "on"


class ConnectionSensor(BmsEntity, BinarySensorEntity):
    _attr_name = "Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, coordinator):
        super().__init__(coordinator, "connection")
    @property
    def available(self):
        return True
    @property
    def is_on(self):
        return self.coordinator.stream_connected and self.coordinator.last_update_success


class ReadySensor(BmsEntity, BinarySensorEntity):
    _attr_icon = "mdi:shield-check"
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"partition_{identifier}_ready")
        self.identifier = identifier
        self._attr_name = "Ready for full set" if len(coordinator.data["partitions"]) == 1 else f"Partition {identifier} ready for full set"
    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["partitions"]
    @property
    def is_on(self):
        if not self.coordinator.data.get("faults_supported"):
            return None
        if any((zone.get("open") is None or zone.get("tamper") is None)
               for zone in self.coordinator.data["zones"].values()
               if self.identifier in zone.get("partitions", []) and not zone.get("omitted")):
            return None
        partition = self.coordinator.data["partitions"][self.identifier]
        return (partition.get("state") in ("unset", "acknowledged") and not partition.get("in-alarm")
                and not blocking_reasons(self.coordinator.data, self.identifier))
    @property
    def extra_state_attributes(self):
        return {"blocking_reasons": blocking_reasons(self.coordinator.data, self.identifier),
                "scope": "full_set", "source": "derived_from_zone_status_and_panel_faults"}


class PanelProblem(BmsEntity, BinarySensorEntity):
    _attr_name = "Panel faults"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, coordinator):
        super().__init__(coordinator, "panel_faults")
    @property
    def is_on(self):
        if not self.coordinator.data.get("faults_supported"):
            return None
        return bool(equipment_faults(self.coordinator.data))
    @property
    def extra_state_attributes(self):
        return {"faults": [fault_description(f, self.coordinator.data["zones"])
                           for f in equipment_faults(self.coordinator.data).values()]}


class FaultSensor(BmsEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"fault_{identifier}")
        self.identifier = identifier
        self._attr_name = fault_description(coordinator.data["faults"][identifier], coordinator.data["zones"])
    @property
    def is_on(self):
        if not self.coordinator.data.get("faults_supported"):
            return None
        return self.identifier in equipment_faults(self.coordinator.data)
    @property
    def extra_state_attributes(self):
        fault = self.coordinator.data["faults"].get(self.identifier, {})
        return {"fault_id": self.identifier, "type": fault.get("type"),
                "prevents_set": fault.get("prevents-set", False), "is_current": fault.get("is-current"),
                "is_latched": fault.get("is-latched")}
