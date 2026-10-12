"""Portable setup with certificate discovery and explicit trust confirmation."""
from __future__ import annotations

import re
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import BmsAuthError, BmsCertificateError, BmsClient, BmsError, discover_fingerprint
from .const import (CONF_FINGERPRINT, CONF_HOME_MODE, CONF_NIGHT_MODE, CONF_PIN,
                    CONF_ZONE_TYPES, DEFAULT_PORT, DOMAIN, PART_MODES, ZONE_TYPES)


def schema(defaults=None):
    defaults = defaults or {}
    secret = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
    host = vol.Required(CONF_HOST, default=defaults[CONF_HOST]) if defaults.get(CONF_HOST) else vol.Required(CONF_HOST)
    return vol.Schema({
        host: str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)):
            vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required(CONF_PIN): secret,
        vol.Required(CONF_PASSWORD): secret,
    })


class BmsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        return await self._details("user", user_input)

    async def _details(self, step, user_input=None):
        errors = {}
        entry = self._get_reauth_entry() if step == "reauth_confirm" else self._get_reconfigure_entry() if step == "reconfigure" else None
        if user_input is not None:
            data = dict(user_input)
            host = data[CONF_HOST].strip().lower()
            if not host or any(c in host for c in "/@?# ") or "://" in host:
                errors["base"] = "invalid_host"
            elif not re.fullmatch(r"[0-9]+", data[CONF_PIN]) or not data[CONF_PASSWORD]:
                errors["base"] = "invalid_auth"
            elif entry and step == "reauth_confirm" and (host, data[CONF_PORT]) != (entry.data[CONF_HOST], entry.data[CONF_PORT]):
                errors["base"] = "same_panel_required"
            else:
                data[CONF_HOST] = host
                # Duplicate detection happens before any credentials are sent.
                await self.async_set_unique_id(f"{host}:{data[CONF_PORT]}")
                self._abort_if_unique_id_configured(updates=None) if not entry else None
                try:
                    fingerprint = await discover_fingerprint(host, data[CONF_PORT])
                except BmsError:
                    errors["base"] = "cannot_connect"
                else:
                    self._pending = {**data, CONF_FINGERPRINT: fingerprint}
                    self._mode = step
                    # Existing trusted certificate may be reused for reauthentication.
                    if entry and fingerprint == entry.data.get(CONF_FINGERPRINT):
                        return await self._finish()
                    return await self.async_step_certificate()
        return self.async_show_form(step_id=step, data_schema=schema(user_input or (entry.data if entry else None)), errors=errors)

    async def async_step_certificate(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input.get("confirm_certificate"):
                return await self._finish()
            errors["base"] = "certificate_not_confirmed"
        fingerprint = self._pending[CONF_FINGERPRINT]
        formatted = ":".join(fingerprint[i:i + 2].upper() for i in range(0, 64, 2))
        return self.async_show_form(step_id="certificate", description_placeholders={
            "host": self._pending[CONF_HOST], "fingerprint": formatted}, errors=errors,
            data_schema=vol.Schema({vol.Required("confirm_certificate", default=False): bool}))

    async def _finish(self):
        data = self._pending
        client = BmsClient(async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PORT],
                           data[CONF_PIN], data[CONF_PASSWORD], data[CONF_FINGERPRINT])
        try:
            system = await client.system()
        except BmsAuthError:
            error = "invalid_auth"
        except BmsCertificateError:
            error = "certificate_mismatch"
        except BmsError:
            error = "cannot_connect"
        else:
            if self._mode != "user":
                entry = self._get_reauth_entry() if self._mode == "reauth_confirm" else self._get_reconfigure_entry()
                unique = f"{data[CONF_HOST]}:{data[CONF_PORT]}"
                # Prevent reconfiguration to another already configured panel.
                if any(e.entry_id != entry.entry_id and e.unique_id == unique for e in self._async_current_entries()):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(entry, data=data, unique_id=unique)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=system.get("name", "Eaton alarm"), data=data)
        return self.async_show_form(step_id=self._mode, data_schema=schema(data), errors={"base": error})

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self._details("reauth_confirm", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._details("reconfigure", user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BmsOptionsFlow()


class BmsOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input[CONF_HOME_MODE] == user_input[CONF_NIGHT_MODE]:
                errors["base"] = "different_modes_required"
            else:
                self._options = {**self.config_entry.options, **user_input}
                return await self.async_step_zones()
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_HOME_MODE, default=self.config_entry.options.get(CONF_HOME_MODE, "partset-b")): vol.In(PART_MODES),
            vol.Required(CONF_NIGHT_MODE, default=self.config_entry.options.get(CONF_NIGHT_MODE, "partset-c")): vol.In(PART_MODES),
        }))

    async def async_step_zones(self, user_input=None):
        if user_input is not None:
            self._options[CONF_ZONE_TYPES] = dict(user_input)
            return self.async_create_entry(title="", data=self._options)
        coordinator = getattr(self.config_entry, "runtime_data", None)
        zones = coordinator.data["zones"] if coordinator and coordinator.data else {}
        types = self.config_entry.options.get(CONF_ZONE_TYPES, {})
        fields = {vol.Required(identifier, default=types.get(identifier, "motion")):
                  selector.SelectSelector(selector.SelectSelectorConfig(options=[
                      {"value": value, "label": value.capitalize()} for value in ZONE_TYPES]))
                  for identifier in zones}
        names = "\n".join(f"{identifier}: {zone.get('name', 'Zone')}" for identifier, zone in zones.items())
        return self.async_show_form(step_id="zones", data_schema=vol.Schema(fields),
                                    description_placeholders={"zones": names or "No zones currently discovered."})
