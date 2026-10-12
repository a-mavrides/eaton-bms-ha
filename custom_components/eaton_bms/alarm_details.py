"""Alarm details from explicit panel fields only; never infer a cause from motion."""
from datetime import datetime

ALARM_NAMES = {1: "Faults", 2: "Technical", 3: "Fire", 4: "Burglary", 6: "Medical",
               7: "Panic", 8: "Social", 9: "Inactivity"}
ZONE_FIELDS = ("affects-zone", "zone-id", "zone_id", "zone")


def _zone_ids(payload):
    identifiers = set()
    for key in ZONE_FIELDS:
        value = payload.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("id")
            if item is not None and str(item).isdigit():
                identifiers.add(str(int(item)))
    return identifiers


def alarm_details(snapshot: dict, partition: str) -> dict:
    status = snapshot["partitions"].get(partition, {})
    zone_ids = _zone_ids(status)
    alarm_type = status.get("alarm-type")
    for notice in snapshot.get("alarms", {}).values():
        affects = notice.get("affects-partition")
        partitions = affects if isinstance(affects, list) else [affects]
        # Do not attribute an unscoped alarm to an arbitrary partition.
        if partition not in {str(value) for value in partitions}:
            continue
        zone_ids.update(_zone_ids(notice))
        if alarm_type is None:
            alarm_type = notice.get("type")
    code = int(alarm_type) if str(alarm_type).isdigit() else None
    return {"partition_id": partition, "alarm_type": code,
            "alarm_name": ALARM_NAMES.get(code, "Alarm"),
            "zones": [{"id": identifier, "name": snapshot["zones"].get(identifier, {}).get("name")}
                      for identifier in sorted(zone_ids, key=int)],
            "zone_reported": bool(zone_ids)}
