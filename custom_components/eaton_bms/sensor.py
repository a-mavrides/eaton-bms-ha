"""Panel summaries and timestamp diagnostics."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from datetime import timedelta
import math
from .api import equipment_faults, fault_description
from .entity import BmsEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([TimestampSensor(coordinator, key, name) for key, name in
                        (("last_heartbeat", "Last heartbeat"), ("last_event", "Last event"),
                         ("last_sync", "Last status sync"))] +
                       [NoticeCount(coordinator, "faults"), NoticeCount(coordinator, "alarms")])
    known = set()
    def discover():
        fresh = set(coordinator.data["partitions"]) - known
        known.update(fresh)
        if fresh:
            async_add_entities([entity for identifier in fresh for entity in
                (ExitCountdown(coordinator, identifier), LastAlarm(coordinator, identifier),
                 TriggeringZone(coordinator, identifier))])
    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class TimestampSensor(BmsEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key)
        self.key, self._attr_name = key, name
    @property
    def available(self):
        return True
    @property
    def native_value(self):
        return getattr(self.coordinator, self.key)


class ExitCountdown(BmsEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"partition_{identifier}_exit_countdown")
        self.identifier = identifier
        self._attr_name = "Exit countdown" if len(coordinator.data["partitions"]) == 1 else f"Partition {identifier} exit countdown"
        self._cancel_tick = None

    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["partitions"]

    @property
    def native_value(self):
        state = self.coordinator.data["partitions"].get(self.identifier, {}).get("state")
        if state == "exit":
            deadline = self.coordinator.exit_deadlines.get(self.identifier)
            return max(0, math.ceil((deadline - dt_util.utcnow()).total_seconds())) if deadline else None
        if state in ("unset", "acknowledged", "fullset", "partset", "partset-b", "partset-c", "partset-d", "entry", "exit-fault"):
            return 0
        return None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._update_timer()

    async def async_will_remove_from_hass(self):
        self._stop_timer()
        await super().async_will_remove_from_hass()

    def _stop_timer(self):
        if self._cancel_tick:
            self._cancel_tick()
            self._cancel_tick = None

    def _update_timer(self):
        if self.available and (self.native_value or 0) > 0:
            if not self._cancel_tick:
                self._cancel_tick = async_track_time_interval(self.hass, self._tick, timedelta(seconds=1))
        else:
            self._stop_timer()

    @callback
    def _tick(self, now):
        self.async_write_ha_state()
        self._update_timer()

    @callback
    def _handle_coordinator_update(self):
        super()._handle_coordinator_update()
        self._update_timer()

    @property
    def extra_state_attributes(self):
        return {"partition_id": self.identifier, "source": "panel_countdown_with_local_elapsed_time",
                "panel_state": self.coordinator.data["partitions"].get(self.identifier, {}).get("state")}


class LastAlarm(BmsEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:alarm-light-outline"
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"partition_{identifier}_last_alarm")
        self.identifier = identifier
        self._attr_name = "Last alarm" if len(coordinator.data["partitions"]) == 1 else f"Partition {identifier} last alarm"
    @property
    def available(self):
        return True
    @property
    def native_value(self):
        value = self.coordinator.last_alarms.get(self.identifier, {}).get("observed_at")
        return dt_util.parse_datetime(value) if value else None
    @property
    def extra_state_attributes(self):
        record = self.coordinator.last_alarms.get(self.identifier, {})
        return {**record, "time_source": "first_observed_by_home_assistant"}


class TriggeringZone(BmsEntity, SensorEntity):
    _attr_icon = "mdi:motion-sensor"
    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"partition_{identifier}_triggering_zone")
        self.identifier = identifier
        self._attr_name = "Triggering zone" if len(coordinator.data["partitions"]) == 1 else f"Partition {identifier} triggering zone"
    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["partitions"]
    @property
    def native_value(self):
        state = self.coordinator.data["partitions"].get(self.identifier, {})
        if state.get("in-alarm") is False:
            return "No active alarm"
        if state.get("in-alarm") is not True:
            return None
        record = self.coordinator.last_alarms.get(self.identifier, {})
        zones = record.get("zones", [])
        return ", ".join(str(zone.get("name") or "Zone " + zone["id"]) for zone in zones)[:255] if zones else "Not reported"
    @property
    def extra_state_attributes(self):
        active = self.coordinator.data["partitions"].get(self.identifier, {}).get("in-alarm") is True
        return {"partition_id": self.identifier,
                "zones": self.coordinator.last_alarms.get(self.identifier, {}).get("zones", []) if active else []}


class NoticeCount(BmsEntity, SensorEntity):
    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, coordinator, group):
        super().__init__(coordinator, group + "_count")
        self.group = group
        self._attr_name = "Fault count" if group == "faults" else "Active alarm count"
    @property
    def native_value(self):
        if not self.coordinator.data.get(self.group + "_supported"):
            return None
        return len(equipment_faults(self.coordinator.data) if self.group == "faults" else self.coordinator.data[self.group])
    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if self.group == "faults":
            return {"faults": [{"id": f.get("id"), "type": f.get("type"),
                "description": fault_description(f, data["zones"]), "prevents_set": f.get("prevents-set"),
                "affects_partition": f.get("affects-partition"), "is_current": f.get("is-current"),
                "is_latched": f.get("is-latched")} for f in equipment_faults(data).values()]}
        labels = {1: "Faults", 2: "Technical", 3: "Fire", 4: "Burglary", 6: "Medical", 7: "Panic", 8: "Social", 9: "Inactivity"}
        return {"alarms": [{"id": a.get("id"), "type": a.get("type"),
            "description": labels.get(int(a["type"]), "Alarm") if str(a.get("type", "")).isdigit() else "Alarm",
            "affects_partition": a.get("affects-partition")} for a in data["alarms"].values()]}
