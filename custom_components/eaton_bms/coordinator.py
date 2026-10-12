"""Live event handling with independent periodic reconciliation."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
from datetime import timedelta
import json
from homeassistant.util import dt as dt_util
from homeassistant.helpers.storage import Store

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import BmsAuthError, BmsClient, BmsError, apply_event
from .const import DOMAIN
from .alarm_details import alarm_details

_LOGGER = logging.getLogger(__name__)


class BmsCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client: BmsClient):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.client = client
        self.stream_connected = False
        self._revision = 0
        self._changes: dict[tuple[str, str], int] = {}
        self._tasks: list[asyncio.Task] = []
        self.last_heartbeat = None
        self.last_event = None
        self.last_sync = None
        self.last_command_errors = {}
        self.last_alarms = {}
        self.exit_deadlines = {}
        self._active_alarms = set()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.last_alarms")

    async def _async_setup(self):
        saved = await self._store.async_load()
        if isinstance(saved, dict) and isinstance(saved.get("last_alarms"), dict):
            self.last_alarms = saved["last_alarms"]
            self._active_alarms = {key for key, value in self.last_alarms.items()
                                   if isinstance(value, dict) and value.get("active") is True}

    def _update_exit_deadline(self, identifier, status):
        if status.get("state") != "exit":
            self.exit_deadlines.pop(identifier, None)
            return
        value = status.get("time-to-exit")
        if str(value).isdigit():
            seconds = min(int(value), 86400)
            self.exit_deadlines[identifier] = dt_util.utcnow() + timedelta(seconds=seconds)

    async def _track_alarms(self, data):
        changed = False
        now = dt_util.utcnow().isoformat()
        for identifier, status in data["partitions"].items():
            if status.get("in-alarm") is True:
                details = alarm_details(data, identifier)
                if identifier not in self._active_alarms:
                    self.last_alarms[identifier] = {**details, "observed_at": now, "active": True}
                    self._active_alarms.add(identifier)
                    changed = True
                else:
                    previous = self.last_alarms[identifier]
                    # Polling may add details after the initial partition event.
                    enrich = {key: value for key, value in details.items()
                              if key not in ("zones", "zone_reported", "alarm_type", "alarm_name")}
                    if details["zone_reported"]:
                        enrich.update(zones=details["zones"], zone_reported=True)
                    if details["alarm_type"] is not None:
                        enrich.update(alarm_type=details["alarm_type"], alarm_name=details["alarm_name"])
                    updated = {**previous, **enrich}
                    if updated != previous:
                        self.last_alarms[identifier] = updated
                        changed = True
            elif status.get("in-alarm") is False and identifier in self._active_alarms:
                self._active_alarms.discard(identifier)
                self.last_alarms[identifier] = {**self.last_alarms[identifier], "active": False, "cleared_at": now}
                changed = True
        if changed:
            try:
                await self._store.async_save({"last_alarms": self.last_alarms})
            except OSError:
                _LOGGER.warning("Unable to persist last-alarm details; live monitoring continues")

    async def _async_update_data(self):
        revision = self._revision
        try:
            fresh = await self.client.snapshot()
        except BmsAuthError as error:
            raise ConfigEntryAuthFailed("BMS PIN/password rejected") from error
        except BmsError as error:
            raise UpdateFailed(str(error)) from error
        # A PIR event may arrive while HTTP snapshot requests are in flight.
        # Preserve all objects changed since this refresh started.
        if self.data:
            for (group, identifier), changed_at in self._changes.items():
                if changed_at > revision and identifier in self.data[group]:
                    fresh[group][identifier] = self.data[group][identifier]
        self.last_sync = dt_util.utcnow()
        for identifier, status in fresh["partitions"].items():
            if self._changes.get(("partitions", identifier), 0) <= revision:
                self._update_exit_deadline(identifier, status)
        await self._track_alarms(fresh)
        # Persistence is asynchronous too. An event arriving during that disk
        # write must not be replaced when this older snapshot is returned.
        if self.data:
            for (group, identifier), changed_at in self._changes.items():
                if changed_at > revision and identifier in self.data[group]:
                    fresh[group][identifier] = self.data[group][identifier]
        return fresh

    def _connected(self, value: bool) -> None:
        self.stream_connected = value
        self.async_update_listeners()

    async def _stream_loop(self):
        delay = 2
        while True:
            try:
                async for event in self.client.events():
                    self.last_event = dt_util.utcnow()
                    if event.kind == "connected":
                        # Reconcile every time the stream reconnects. Buffered
                        # events are processed after this snapshot finishes.
                        await self.async_refresh()
                        if self.last_update_success:
                            self._connected(True)
                        delay = 2
                        continue
                    if event.kind == "heartbeat":
                        self.last_heartbeat = dt_util.utcnow()
                        self.async_update_listeners()
                        continue
                    if self.data:
                        data, changed = apply_event(self.data, event)
                        if changed is None and event.kind in ("zone", "partition", "output", "fault", "alarm", "configuration"):
                            # New entities and fault/alarm changes are reconciled
                            # through the same debouncer as other refresh requests.
                            await self.async_request_refresh()
                            data, changed = apply_event(self.data, event)
                        if changed:
                            self._revision += 1
                            self._changes[changed] = self._revision
                            if changed[0] == "partitions":
                                # Keep an existing countdown when a partial event
                                # omits time-to-exit; do not reset it from cached data.
                                payload = json.loads(event.data)
                                payload["state"] = data["partitions"][changed[1]].get("state")
                                self._update_exit_deadline(changed[1], payload)
                            # Publish live alarm feedback before any disk I/O.
                            self.async_set_updated_data(data)
                            if changed[0] == "partitions":
                                await self._track_alarms(data)
                                self.async_update_listeners()
            except asyncio.CancelledError:
                raise
            except BmsAuthError:
                self._connected(False)
                self.config_entry.async_start_reauth(self.hass)
                return
            except BmsError:
                _LOGGER.debug("Local BMS stream disconnected; will reconnect")
            finally:
                self._connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _refresh_loop(self):
        # Independent timer: frequent PIR events cannot postpone status checks.
        while True:
            await asyncio.sleep(60)
            await self.async_refresh()

    def start(self):
        self._tasks = [self.hass.async_create_background_task(
            self._stream_loop(), "Eaton BMS event stream"),
            self.hass.async_create_background_task(
                self._refresh_loop(), "Eaton BMS status reconciliation")]

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._connected(False)
