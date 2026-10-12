"""Eaton/Scantronic local BMS integration."""
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BmsClient
from .const import CONF_FINGERPRINT, CONF_PIN
from .coordinator import BmsCoordinator

PLATFORMS = [Platform.ALARM_CONTROL_PANEL, Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass, entry):
    client = BmsClient(async_get_clientsession(hass), entry.data[CONF_HOST],
                       entry.data[CONF_PORT], entry.data[CONF_PIN],
                       entry.data[CONF_PASSWORD], entry.data[CONF_FINGERPRINT])
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.stop()
        return True
    return False


async def async_reload_entry(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)
