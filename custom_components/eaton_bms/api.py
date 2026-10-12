"""Independent local BMS client. No installer/web sessions are opened."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import re
import hashlib
import ssl
from typing import Any, AsyncIterator

import aiohttp


class BmsError(Exception):
    """A bounded, credential-free error."""


class BmsAuthError(BmsError):
    """PIN/password rejected."""


class BmsCertificateError(BmsError):
    """Panel certificate does not match the configured fingerprint."""


async def discover_fingerprint(host: str, port: int) -> str:
    """Retrieve a TLS certificate without sending HTTP or credentials."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    writer = None
    try:
        async with asyncio.timeout(15):
            _, writer = await asyncio.open_connection(host.strip("[]"), port, ssl=context)
            certificate = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
            return hashlib.sha256(certificate).hexdigest()
    except (OSError, TimeoutError):
        raise BmsError("Unable to retrieve the panel certificate") from None
    finally:
        if writer:
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass


def fault_applies(fault: dict, partition: str) -> bool:
    values = fault.get("affects-partition", [])
    if not isinstance(values, list):
        values = [values]
    return not values or partition in {str(value) for value in values}


def fault_description(fault: dict, zones: dict) -> str:
    zone = zones.get(str(fault.get("affects-zone")), {})
    if str(fault.get("type")) in ("5000", "5010") and zone:
        return str(zone.get("name", "Zone")) + (" open" if str(fault["type"]) == "5000" else " tamper")
    return str(fault.get("ui-string") or "Panel fault " + str(fault.get("type", fault.get("id", "unknown"))))[:200]


def blocking_reasons(snapshot: dict, partition: str) -> list[str]:
    reasons = []
    for zone in snapshot["zones"].values():
        if partition not in zone.get("partitions", []) or zone.get("omitted"):
            continue
        if zone.get("open"):
            reasons.append(str(zone.get("name", zone["id"])) + " open")
        if zone.get("tamper"):
            reasons.append(str(zone.get("name", zone["id"])) + " tamper")
    for fault in snapshot.get("faults", {}).values():
        # Live zone flags supply open-zone readiness. The periodically polled
        # type-5000 notices can otherwise keep a restored PIR blocked.
        if str(fault.get("type")) == "5000":
            continue
        if fault.get("prevents-set") and fault_applies(fault, partition):
            reasons.append(fault_description(fault, snapshot["zones"]))
    return list(dict.fromkeys(reasons))


def equipment_faults(snapshot: dict) -> dict:
    """Type 5000 means zone open; retain tamper and every other/unknown fault."""
    return {key: fault for key, fault in snapshot.get("faults", {}).items()
            if str(fault.get("type")) != "5000"}


@dataclass(frozen=True)
class Event:
    kind: str
    data: str = ""


def parse_frame(frame: str) -> Event | None:
    kind = ""
    data = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            kind = line[6:].lstrip(" ")
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    return Event(kind, "\n".join(data)) if kind else None


def indexed(items: Any, numeric: bool = True) -> dict[str, dict]:
    if not isinstance(items, list):
        raise BmsError("Unexpected BMS list response")
    result = {}
    for item in items:
        if not isinstance(item, dict) or not str(item.get("id", "")) or (numeric and not str(item.get("id", "")).isdigit()):
            raise BmsError("Unexpected BMS item identifier")
        result[str(int(item["id"])) if numeric else str(item["id"])] = dict(item)
    return result


def zone_flags(zone: dict) -> dict:
    known = zone.get("state") in ("closed", "open", "tamper", "tampered")
    return {**zone, "open": zone.get("state") == "open" if known else None,
            "tamper": zone.get("state") in ("tamper", "tampered") if known else None}


def apply_event(snapshot: dict, event: Event) -> tuple[dict, tuple[str, str] | None]:
    """Return a new snapshot; preserve independent open/tamper/omit flags."""
    if event.kind == "zone":
        match = re.fullmatch(r"([0-9]+)\n(open|open_restore|tamper|tamper_restore|omit|omit_restore)\s*", event.data)
        if not match:
            return snapshot, None
        identifier, transition = str(int(match[1])), match[2]
        if identifier not in snapshot["zones"]:
            return snapshot, None
        zone = dict(snapshot["zones"][identifier])
        if transition.startswith("open"):
            zone["open"] = transition == "open"
        elif transition.startswith("tamper"):
            zone["tamper"] = transition == "tamper"
        else:
            zone["omitted"] = transition == "omit"
        zone["state"] = "tamper" if zone["tamper"] else "open" if zone["open"] else "closed"
        return {**snapshot, "zones": {**snapshot["zones"], identifier: zone}}, ("zones", identifier)
    if event.kind in ("partition", "output"):
        try:
            item = json.loads(event.data)
        except (ValueError, TypeError):
            return snapshot, None
        if not isinstance(item, dict) or not str(item.get("id", "")).isdigit():
            return snapshot, None
        group = "partitions" if event.kind == "partition" else "outputs"
        identifier = str(int(item["id"]))
        if identifier not in snapshot[group]:
            return snapshot, None
        previous = dict(snapshot[group][identifier])
        if group == "partitions" and (item.get("in-alarm") is False or
                item.get("in-alarm") is True and previous.get("in-alarm") is not True):
            # A new episode must not inherit the previous alarm's reported cause.
            for field in ("alarm-type", "affects-zone", "zone-id", "zone_id", "zone"):
                previous.pop(field, None)
        return {**snapshot, group: {**snapshot[group], identifier:
                {**previous, **item}}}, (group, identifier)
    return snapshot, None


class BmsClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int,
                 pin: str, password: str, fingerprint: str):
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        self.base_url = f"https://{host}:{port}"
        self.session = session
        self.pin, self.password = pin, password
        self.ssl = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
        self._lock = asyncio.Lock()
        self._system_cache = None

    def _authorization(self, user_pin: str | None = None) -> str:
        pin = self.pin if user_pin is None else user_pin
        token = base64.b64encode(f"{pin}:{self.password}".encode("utf-8")).decode("ascii")
        return "Basic " + token

    @staticmethod
    def _status(status: int) -> None:
        if status in (401, 403):
            raise BmsAuthError("BMS credentials rejected")
        if status == 409:
            raise BmsError("Panel refused the command: check open zones and panel faults")
        if status != 200:
            raise BmsError(f"BMS returned HTTP {status}")

    async def _request(self, method: str, path: str, payload: dict | None = None,
                       user_pin: str | None = None, optional: bool = False) -> Any:
        if not (method == "GET" and (path in ("/system/", "/system/partitions/", "/outputs/", "/faults/", "/alarms/")
                or re.fullmatch(r"/system/partitions-[1-9][0-9]*/zones/", path))
                or method == "PUT" and re.fullmatch(r"/system/partitions-[1-9][0-9]*/", path)):
            raise BmsError("Request outside supported BMS API")
        try:
            async with self._lock:
                async with self.session.request(
                    method, self.base_url + path, json=payload,
                    headers={"Authorization": self._authorization(user_pin)},
                    ssl=self.ssl, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if optional and response.status in (404, 405):
                        return None
                    if response.status != 409:
                        self._status(response.status)
                    raw = await response.content.read(2_000_001)
                    # StreamReader.read(n) may return fewer than n bytes. Collect to EOF.
                    while not response.content.at_eof() and len(raw) <= 2_000_000:
                        raw += await response.content.read(65536)
                    if len(raw) > 2_000_000:
                        raise BmsError("Oversized BMS response")
                    try:
                        result = json.loads(raw)
                    except (ValueError, UnicodeError):
                        if response.status == 409:
                            self._status(409)
                        raise BmsError("Expected a JSON BMS response") from None
                    if response.status == 409:
                        faults = result if isinstance(result, list) else []
                        messages = [fault_description(f, {}) for f in faults if isinstance(f, dict) and f.get("prevents-set")]
                        raise BmsError("Panel refused the command: " + ("; ".join(messages[:10]) or "check open zones and panel faults"))
                    if method == "PUT" and isinstance(result, dict):
                        failures = {"installer": "Installer is logged in", "appAccessDisabled": "Remote access is disabled",
                                    "noZonesToSet": "No zones to set", "inAlarm": "Panel is in alarm",
                                    "installerResetRequired": "Installer reset is required"}
                        if result.get("state") in failures:
                            raise BmsError(failures[result["state"]])
                    return result
        except aiohttp.ServerFingerprintMismatch:
            raise BmsCertificateError("Panel certificate fingerprint mismatch") from None
        except (aiohttp.ClientError, TimeoutError):
            raise BmsError("Unable to communicate with the local BMS API") from None

    async def system(self) -> dict:
        result = await self._request("GET", "/system/")
        if not isinstance(result, dict) or not isinstance(result.get("partitions"), list):
            raise BmsError("Unexpected system response")
        return result

    async def snapshot(self) -> dict:
        if self._system_cache is None:
            self._system_cache = await self.system()
        system = self._system_cache
        partitions = indexed(await self._request("GET", "/system/partitions/"))
        zones = {}
        for identifier in partitions:
            if int(identifier) <= 0 or int(identifier) > 100:
                raise BmsError("Unsupported partition identifier")
            items = indexed(await self._request("GET", f"/system/partitions-{identifier}/zones/"))
            for zone_id, item in items.items():
                groups = zones.get(zone_id, {}).get("partitions", [])
                zones[zone_id] = zone_flags({**item, "partitions": groups + [identifier]})
        outputs = indexed(await self._request("GET", "/outputs/"))
        faults = await self._request("GET", "/faults/", optional=True)
        alarms = await self._request("GET", "/alarms/", optional=True)
        return {"system": system, "partitions": partitions, "zones": zones, "outputs": outputs,
                "faults": indexed(faults, numeric=False) if faults is not None else {},
                "alarms": indexed(alarms, numeric=False) if alarms is not None else {},
                "faults_supported": faults is not None, "alarms_supported": alarms is not None}

    async def set_partition(self, identifier: str, state: str, user_pin: str) -> Any:
        if not isinstance(user_pin, str) or not user_pin.isascii() or not user_pin.isdigit():
            raise BmsError("Enter your normal alarm user PIN")
        if state not in ("unset", "fullset", "partset", "partset-b", "partset-c", "partset-d"):
            raise BmsError("Unsupported alarm mode")
        return await self._request("PUT", f"/system/partitions-{identifier}/",
                                   {"state": state}, user_pin)

    async def events(self) -> AsyncIterator[Event]:
        try:
            async with self.session.get(
                self.base_url + "/events/",
                ssl=self.ssl, allow_redirects=False,
                headers={"Accept": "text/event-stream", "Connection": "Keep-Alive",
                         "Authorization": self._authorization()},
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=180),
            ) as response:
                self._status(response.status)
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise BmsError("Unexpected BMS event stream format")
                yield Event("connected")
                buffer = b""
                async for chunk in response.content.iter_chunked(4096):
                    buffer += chunk
                    buffer = buffer.replace(b"\r\n", b"\n")
                    if len(buffer) > 65536:
                        raise BmsError("Oversized BMS event")
                    while b"\n\n" in buffer:
                        frame, buffer = buffer.split(b"\n\n", 1)
                        event = parse_frame(frame.decode("utf-8", errors="replace"))
                        if event:
                            yield event
        except aiohttp.ServerFingerprintMismatch:
            raise BmsCertificateError("Panel certificate fingerprint mismatch") from None
        except (aiohttp.ClientError, TimeoutError):
            raise BmsError("Local BMS event stream disconnected") from None
