"""Real panel arm/disarm actions. User PIN is supplied for each command."""
from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity, AlarmControlPanelEntityFeature,
    AlarmControlPanelState, CodeFormat,
)
from homeassistant.exceptions import HomeAssistantError

from .api import BmsAuthError, BmsError, blocking_reasons
from .const import CONF_HOME_MODE, CONF_NIGHT_MODE
from .entity import BmsEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = set()
    def discover():
        fresh = set(coordinator.data["partitions"]) - known
        known.update(fresh)
        if fresh:
            async_add_entities(PartitionAlarm(coordinator, identifier) for identifier in fresh)
    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class PartitionAlarm(BmsEntity, AlarmControlPanelEntity):
    _attr_code_format = CodeFormat.NUMBER
    _attr_code_arm_required = True
    _attr_supported_features = (AlarmControlPanelEntityFeature.ARM_AWAY |
                                AlarmControlPanelEntityFeature.ARM_HOME |
                                AlarmControlPanelEntityFeature.ARM_NIGHT)

    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, f"partition_{identifier}")
        self.identifier = identifier
        self._attr_name = "Alarm" if len(coordinator.data["partitions"]) == 1 else "Partition " + identifier

    @property
    def home_mode(self):
        return self.coordinator.config_entry.options.get(CONF_HOME_MODE, "partset-b")

    @property
    def night_mode(self):
        return self.coordinator.config_entry.options.get(CONF_NIGHT_MODE, "partset-c")

    @property
    def available(self):
        return super().available and self.identifier in self.coordinator.data["partitions"]

    @property
    def alarm_state(self):
        status = self.coordinator.data["partitions"].get(self.identifier, {})
        if status.get("in-alarm"):
            return AlarmControlPanelState.TRIGGERED
        state = status.get("state")
        if state in ("unset", "acknowledged"):
            return AlarmControlPanelState.DISARMED
        if state == "fullset":
            return AlarmControlPanelState.ARMED_AWAY
        if state == self.home_mode:
            return AlarmControlPanelState.ARMED_HOME
        if state == self.night_mode:
            return AlarmControlPanelState.ARMED_NIGHT
        if state in ("partset", "partset-b", "partset-c", "partset-d"):
            return AlarmControlPanelState.ARMED_CUSTOM_BYPASS
        if state == "exit":
            return AlarmControlPanelState.ARMING
        if state == "entry":
            return AlarmControlPanelState.PENDING
        return None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data["partitions"].get(self.identifier, {})
        return {"partition_id": self.identifier, "panel_state": data.get("state"),
                "in_alarm": data.get("in-alarm", False), "time_to_exit": data.get("time-to-exit"),
                "home_mode": self.home_mode, "night_mode": self.night_mode,
                "blocking_reasons": blocking_reasons(self.coordinator.data, self.identifier),
                "last_command_error": self.coordinator.last_command_errors.get(self.identifier)}

    async def _command(self, state, code):
        if not self.available:
            raise HomeAssistantError("Alarm connection unavailable")
        try:
            await self.coordinator.client.set_partition(self.identifier, state, code)
        except BmsAuthError:
            self.coordinator.last_command_errors[self.identifier] = "User PIN or its remote permissions rejected"
            self.coordinator.async_update_listeners()
            raise HomeAssistantError("Panel rejected the user PIN or its remote permissions") from None
        except BmsError as error:
            await self.coordinator.async_refresh()
            reasons = blocking_reasons(self.coordinator.data, self.identifier)
            message = str(error) + (". " + "; ".join(reasons[:10]) if reasons else "")
            self.coordinator.last_command_errors[self.identifier] = message
            self.coordinator.async_update_listeners()
            raise HomeAssistantError(message) from None
        self.coordinator.last_command_errors.pop(self.identifier, None)
        # Never assume the command changed the state; use feedback from panel.
        await self.coordinator.async_refresh()

    async def async_alarm_disarm(self, code=None):
        await self._command("unset", code)

    async def async_alarm_arm_away(self, code=None):
        await self._command("fullset", code)

    async def async_alarm_arm_home(self, code=None):
        await self._command(self.home_mode, code)

    async def async_alarm_arm_night(self, code=None):
        await self._command(self.night_mode, code)
