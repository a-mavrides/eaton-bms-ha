import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eaton_bms.api import BmsAuthError, BmsCertificateError, BmsError, Event, apply_event
from custom_components.eaton_bms.const import DOMAIN
from custom_components.eaton_bms.coordinator import BmsCoordinator
from custom_components.eaton_bms.alarm_control_panel import PartitionAlarm
from custom_components.eaton_bms.binary_sensor import ReadySensor, PanelProblem, FaultSensor, ZoneSensor
from custom_components.eaton_bms.sensor import NoticeCount
from custom_components.eaton_bms.sensor import LastAlarm, TriggeringZone, ExitCountdown, TimestampSensor
from custom_components.eaton_bms.alarm_details import alarm_details


def ui_data(panel):
    return {k: v for k, v in entry_data(panel).items() if k != "certificate_sha256"}


def entry_data(panel):
    return {"host": "127.0.0.1", "port": panel["port"], "bms_pin": "1234",
            "password": "test-password", "certificate_sha256": panel["fingerprint"]}


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_snapshot_discovers_expander_and_output(client):
    data = await client.snapshot()
    assert len(data["zones"]) == 10
    assert data["zones"]["55"]["name"] == "Upstairs PIR"
    assert data["zones"]["55"]["partitions"] == ["1"]
    assert data["outputs"]["0"]["state"] == "off"
    assert data["partitions"]["1"]["state"] == "unset"


async def test_auth_stops_after_one_request(client, panel):
    panel["deny"] = True
    with pytest.raises(BmsAuthError):
        await client.snapshot()
    assert len(panel["requests"]) == 1


async def test_fingerprint_checked_before_auth(client, panel):
    import aiohttp
    client.ssl = aiohttp.Fingerprint(bytes(32))
    with pytest.raises(BmsCertificateError):
        await client.system()
    assert panel["requests"] == []


async def test_split_live_frames_and_tamper_independence(client, panel):
    snapshot = await client.snapshot()
    events = client.events()
    try:
        assert (await anext(events)).kind == "connected"
        assert (await anext(events)).kind == "heartbeat"
        await panel["send"]("event: zone\r\ndata: 55\r\ndata: open\r\n\r\n")
        snapshot, changed = apply_event(snapshot, await anext(events))
        assert changed == ("zones", "55") and snapshot["zones"]["55"]["open"]
        for transition in ("tamper", "open_restore"):
            snapshot, _ = apply_event(snapshot, Event("zone", "55\n" + transition))
        assert not snapshot["zones"]["55"]["open"] and snapshot["zones"]["55"]["tamper"]
        snapshot, _ = apply_event(snapshot, Event("zone", "55\ntamper_restore"))
        assert snapshot["zones"]["55"]["state"] == "closed"
    finally:
        await events.aclose()


async def test_commands_use_user_pin_and_no_retry(client, panel):
    await client.set_partition("1", "fullset", "5678")
    assert panel["requests"][-1] == ("PUT", "/system/partitions-1/", "5678", "test-password")
    assert panel["partition"]["state"] == "fullset"
    await client.set_partition("1", "unset", "5678")
    assert panel["partition"]["state"] == "unset"
    before = len(panel["requests"])
    with pytest.raises(BmsError):
        await client.set_partition("1", "fullset", None)
    assert len(panel["requests"]) == before
    panel["command_status"] = 409
    with pytest.raises(BmsError, match="refused"):
        await client.set_partition("1", "fullset", "5678")
    assert len(panel["requests"]) == before + 1


async def test_coordinator_preserves_event_during_snapshot(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    stale = await client.snapshot()

    async def delayed_snapshot():
        data, changed = apply_event(coordinator.data, Event("zone", "55\nopen"))
        coordinator._revision += 1
        coordinator._changes[changed] = coordinator._revision
        coordinator.async_set_updated_data(data)
        return stale

    with patch.object(client, "snapshot", delayed_snapshot):
        await coordinator.async_refresh()
    assert coordinator.data["zones"]["55"]["open"]


async def test_alarm_mapping_and_command_errors(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    coordinator.stream_connected = True
    alarm = PartitionAlarm(coordinator, "1")
    assert alarm.alarm_state == AlarmControlPanelState.DISARMED
    for state, expected in (("fullset", AlarmControlPanelState.ARMED_AWAY),
                            ("partset-b", AlarmControlPanelState.ARMED_HOME),
                            ("partset-c", AlarmControlPanelState.ARMED_NIGHT),
                            ("exit", AlarmControlPanelState.ARMING),
                            ("exit-fault", None)):
        coordinator.data["partitions"]["1"]["state"] = state
        assert alarm.alarm_state == expected
    coordinator.data["partitions"]["1"]["in-alarm"] = True
    assert alarm.alarm_state == AlarmControlPanelState.TRIGGERED
    with patch.object(client, "set_partition", AsyncMock(side_effect=BmsAuthError("rejected"))):
        with pytest.raises(HomeAssistantError, match="user PIN"):
            await alarm.async_alarm_disarm("5678")
    coordinator.stream_connected = False
    with pytest.raises(HomeAssistantError, match="unavailable"):
        await alarm.async_alarm_arm_away("5678")


async def test_config_flow_and_full_ha_setup(hass, panel):
    data = entry_data(panel)
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert flow["type"] == "form"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {k: v for k, v in data.items() if k != "certificate_sha256"})
    assert result["step_id"] == "certificate"
    assert panel["requests"] == []
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"confirm_certificate": True})
    assert result["type"] == "create_entry"
    entry = result["result"]
    await hass.async_block_till_done()
    await wait_for(lambda: getattr(entry, "runtime_data", None) and entry.runtime_data.stream_connected)
    coordinator = entry.runtime_data
    # Native Home Assistant entity setup, not only the standalone client.
    await hass.async_block_till_done()
    states = hass.states.async_all()
    motion = [s for s in states if s.attributes.get("device_class") == "motion"]
    assert len(motion) == 10
    master = next(s for s in motion if s.attributes.get("zone_id") == "55")
    assert master.state == "off"
    await panel["send"]("event: zone\ndata: 55\ndata: open\n\n")
    await wait_for(lambda: hass.states.get(master.entity_id).state == "on")
    await panel["send"]("event: zone\ndata: 55\ndata: open_restore\n\n")
    await wait_for(lambda: hass.states.get(master.entity_id).state == "off")
    assert coordinator.last_heartbeat is not None and coordinator.last_event is not None
    panel["zones"].append({"id": "99", "name": "Front Window", "state": "closed", "omittable": False, "omitted": False})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert any(s.attributes.get("zone_id") == "99" for s in hass.states.async_all())
    # New faults create optional individual problem entities automatically.
    panel["faults"] = [{"id": "mains", "type": "123", "ui-string": "Mains failure", "prevents-set": True}]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    from homeassistant.helpers import entity_registry as er
    assert any(e.unique_id == f"{entry.entry_id}_fault_mains" for e in er.async_get(hass).entities.values())
    panel["faults"] = []
    await coordinator.async_refresh()
    assert len([s for s in states if s.entity_id.startswith("alarm_control_panel.")]) == 1
    alarm = next(s for s in states if s.entity_id.startswith("alarm_control_panel."))
    # Native HA alarm feedback and persistent details from a reported zone.
    await panel["send"]('event: partition\ndata: {"id":"1","state":"fullset","in-alarm":true,"alarm-type":4,"affects-zone":55}\n\n')
    await wait_for(lambda: hass.states.get(alarm.entity_id).state == "triggered")
    assert coordinator.last_alarms["1"]["zones"][0]["id"] == "55"
    await panel["send"]('event: partition\ndata: {"id":"1","state":"unset","in-alarm":false}\n\n')
    await wait_for(lambda: hass.states.get(alarm.entity_id).state == "disarmed")
    assert coordinator.last_alarms["1"]["active"] is False
    # Countdown ticks locally: no extra requests and no optimistic arming.
    panel["partition"].update(state="exit", **{"time-to-exit": 3})
    await coordinator.async_refresh()
    requests_before_tick = len(panel["requests"])
    await asyncio.sleep(1.1)
    assert ExitCountdown(coordinator, "1").native_value <= 2
    assert len(panel["requests"]) == requests_before_tick
    assert hass.states.get(alarm.entity_id).state == "arming"
    panel["partition"].pop("time-to-exit")
    panel["partition"]["state"] = "unset"
    await coordinator.async_refresh()
    for action, state in (("alarm_arm_away", "armed_away"), ("alarm_disarm", "disarmed"),
                          ("alarm_arm_home", "armed_home"), ("alarm_disarm", "disarmed"),
                          ("alarm_arm_night", "armed_night"), ("alarm_disarm", "disarmed")):
        await hass.services.async_call("alarm_control_panel", action,
                                       {"entity_id": alarm.entity_id, "code": "5678"}, blocking=True)
        assert hass.states.get(alarm.entity_id).state == state
    before = len(panel["requests"])
    with pytest.raises(HomeAssistantError, match="user PIN"):
        await hass.services.async_call("alarm_control_panel", "alarm_disarm",
                                       {"entity_id": alarm.entity_id, "code": "9999"}, blocking=True)
    assert len(panel["requests"]) == before + 1
    assert hass.states.get(alarm.entity_id).state == "disarmed"
    # Disconnect is visible; reconnection takes a fresh snapshot.
    next(z for z in panel["zones"] if z["id"] == "55")["state"] = "open"
    await panel["send"](None)
    await wait_for(lambda: not coordinator.stream_connected)
    assert hass.states.get(master.entity_id).state == "unavailable"
    await wait_for(lambda: coordinator.stream_connected and panel["connected"] >= 2)
    await wait_for(lambda: hass.states.get(master.entity_id).state == "on")
    tasks = list(coordinator._tasks)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert all(task.done() for task in tasks)


@pytest.mark.parametrize("change,error", [
    ({"host": "https://bad/"}, "invalid_host"),
    ({"bms_pin": ""}, "invalid_auth"),
    ({"password": "wrong"}, "invalid_auth"),
])
async def test_config_flow_errors(hass, panel, change, error):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {**ui_data(panel), **change})
    if result.get("step_id") == "certificate":
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"confirm_certificate": True})
    assert result["errors"] == {"base": error}


async def test_duplicate_and_options_flow(hass, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel),
                            unique_id=f"127.0.0.1:{panel['port']}")
    entry.add_to_hass(hass)
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], ui_data(panel))
    assert result["type"] == "abort" and result["reason"] == "already_configured"
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(flow["flow_id"],
                                                              {"home_mode": "partset-b", "night_mode": "partset-b"})
    assert result["errors"] == {"base": "different_modes_required"}
    result = await hass.config_entries.options.async_configure(flow["flow_id"],
                                                              {"home_mode": "partset", "night_mode": "partset-d"})
    assert result["step_id"] == "zones"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert result["type"] == "create_entry"
    assert entry.options["home_mode"] == "partset"


async def test_reauth_updates_credentials(hass, panel):
    data = entry_data(panel)
    entry = MockConfigEntry(domain=DOMAIN, data={**data, "password": "old"},
                            unique_id=f"127.0.0.1:{panel['port']}")
    entry.add_to_hass(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=dict(entry.data))
    assert flow["step_id"] == "reauth_confirm"
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], ui_data(panel))
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "test-password"


async def test_certificate_confirmation_does_not_send_credentials(hass, panel):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    # Portable form has no panel-specific address or fingerprint field.
    keys = {str(key.schema) for key in flow["data_schema"].schema}
    assert "certificate_sha256" not in keys
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], ui_data(panel))
    assert result["step_id"] == "certificate"
    assert panel["requests"] == []
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"confirm_certificate": False})
    assert result["errors"] == {"base": "certificate_not_confirmed"}
    assert panel["requests"] == []


async def test_certificate_change_requires_new_confirmation(hass, panel):
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data(panel), "certificate_sha256": "0" * 64},
                            unique_id=f"127.0.0.1:{panel['port']}")
    entry.add_to_hass(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], ui_data(panel))
    assert result["step_id"] == "certificate" and panel["requests"] == []
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"confirm_certificate": True})
    assert result["type"] == "abort"
    assert entry.data["certificate_sha256"] == panel["fingerprint"]


async def test_readiness_faults_alarms_and_clearance(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    ready = ReadySensor(coordinator, "1")
    problem = PanelProblem(coordinator)
    assert ready.is_on and not problem.is_on
    coordinator.data, _ = apply_event(coordinator.data, Event("zone", "55\nopen"))
    assert not ready.is_on
    assert "Upstairs PIR open" in ready.extra_state_attributes["blocking_reasons"]
    panel["faults"] = [{"id": "mains", "type": "123", "ui-string": "Mains failure",
                        "prevents-set": True, "affects-partition": [1], "is-current": True}]
    panel["alarms"] = [{"id": "1", "type": 4, "affects-partition": 1}]
    await coordinator.async_refresh()
    fault = FaultSensor(coordinator, "mains")
    assert not ready.is_on and problem.is_on and fault.is_on
    assert ready.extra_state_attributes["blocking_reasons"] == ["Mains failure"]
    assert NoticeCount(coordinator, "alarms").native_value == 1
    assert NoticeCount(coordinator, "alarms").extra_state_attributes["alarms"][0]["description"] == "Burglary"
    panel["faults"] = []
    panel["alarms"] = []
    await coordinator.async_refresh()
    assert ready.is_on and not fault.is_on and not problem.is_on


async def test_optional_faults_unsupported_is_unknown(hass, client, panel):
    panel["diagnostic_status"] = 404
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    assert len(coordinator.data["zones"]) == 10
    assert ReadySensor(coordinator, "1").is_on is None
    assert NoticeCount(coordinator, "faults").native_value is None


async def test_command_refusal_has_panel_reason_without_retry(client, panel):
    panel["command_status"] = 409
    panel["command_faults"] = [{"id": "battery", "ui-string": "Low battery", "prevents-set": True}]
    with pytest.raises(BmsError, match="Low battery"):
        await client.set_partition("1", "fullset", "5678")
    assert len(panel["requests"]) == 1


async def test_zone_options_keep_entity_identity(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    original = ZoneSensor(coordinator, "55", False)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(flow["flow_id"],
                             {"home_mode": "partset-b", "night_mode": "partset-c"})
    assert result["step_id"] == "zones"
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {"55": "door"})
    assert result["type"] == "create_entry"
    updated = ZoneSensor(coordinator, "55", False)
    assert updated.unique_id == original.unique_id
    assert updated.device_class == "door"


async def test_unknown_zone_state_is_not_ready(hass, client, panel):
    panel["zones"][0]["state"] = "unknown-state"
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    assert ReadySensor(coordinator, "1").is_on is None
    assert ZoneSensor(coordinator, "0", False).is_on is None


async def test_open_pir_not_equipment_fault_and_restore_not_blocked(hass, client, panel):
    panel['faults'] = [{'id': '5767', 'type': '5000', 'affects-zone': '55',
                        'prevents-set': True, 'affects-partition': ['1'],
                        'is-current': False, 'is-latched': False}]
    next(z for z in panel['zones'] if z['id'] == '55')['state'] = 'open'
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    assert not PanelProblem(coordinator).is_on
    assert NoticeCount(coordinator, 'faults').native_value == 0
    assert NoticeCount(coordinator, 'faults').extra_state_attributes['faults'] == []
    assert not ReadySensor(coordinator, '1').is_on
    coordinator.data, _ = apply_event(coordinator.data, Event('zone', '55\nopen_restore'))
    assert ReadySensor(coordinator, '1').is_on
    # The polled notice is still present: live restore must take precedence.
    assert '5767' in coordinator.data['faults']


async def test_open_zone_filter_keeps_tamper_and_unknown_faults(hass, client, panel):
    panel['faults'] = [
        {'id': '1', 'type': 5000, 'affects-zone': '55', 'prevents-set': True},
        {'id': '2', 'type': '5010', 'affects-zone': '55', 'prevents-set': True},
        {'id': '3', 'type': 'unrecognised', 'ui-string': 'Other fault', 'prevents-set': True},
    ]
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    assert PanelProblem(coordinator).is_on
    assert NoticeCount(coordinator, 'faults').native_value == 2
    descriptions = PanelProblem(coordinator).extra_state_attributes['faults']
    assert 'Upstairs PIR tamper' in descriptions and 'Other fault' in descriptions
    assert not ReadySensor(coordinator, '1').is_on


async def test_alarm_zone_is_explicit_never_guessed(client):
    data = await client.snapshot()
    data['partitions']['1'].update({'in-alarm': True, 'alarm-type': 4})
    data['zones']['55']['open'] = True
    assert alarm_details(data, '1')['zones'] == []
    data['alarms'] = {'1': {'id': '1', 'type': 4, 'affects-partition': '1', 'affects-zone': 0}}
    details = alarm_details(data, '1')
    assert details['zones'][0]['id'] == '0' and details['zone_reported']
    data['alarms']['1']['affects-partition'] = '2'
    assert alarm_details(data, '1')['zones'] == []


async def test_last_alarm_persists_clearance_restart_and_next_episode(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    data, _ = apply_event(coordinator.data, Event('partition', '{"id":"1","in-alarm":true,"alarm-type":4,"affects-zone":55}'))
    await coordinator._track_alarms(data)
    timestamp = coordinator.last_alarms['1']['observed_at']
    await coordinator._track_alarms(data)
    assert coordinator.last_alarms['1']['observed_at'] == timestamp
    data, _ = apply_event(data, Event('partition', '{"id":"1","in-alarm":false}'))
    await coordinator._track_alarms(data)
    coordinator.async_set_updated_data(data)
    assert TriggeringZone(coordinator, '1').native_value == 'No active alarm'
    assert TriggeringZone(coordinator, '1').extra_state_attributes['zones'] == []
    assert LastAlarm(coordinator, '1').native_value is not None
    restored = BmsCoordinator(hass, entry, client)
    await restored._async_setup()
    assert restored.last_alarms['1']['observed_at'] == timestamp
    assert restored.last_alarms['1']['zones'][0]['id'] == '55'
    assert not restored.last_alarms['1']['active']
    data, _ = apply_event(data, Event('partition', '{"id":"1","in-alarm":true,"alarm-type":7}'))
    await restored._track_alarms(data)
    assert restored.last_alarms['1']['zones'] == []
    assert restored.last_alarms['1']['alarm_name'] == 'Panic'


async def test_missing_alarm_zone_and_quiet_diagnostics(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    data, _ = apply_event(coordinator.data, Event('partition', '{"id":"1","in-alarm":true}'))
    await coordinator._track_alarms(data)
    coordinator.async_set_updated_data(data)
    assert TriggeringZone(coordinator, '1').native_value == 'Not reported'
    assert not TimestampSensor(coordinator, 'last_event', 'Last event').entity_registry_enabled_default


async def test_countdown_zero_does_not_assume_armed(hass, client, panel):
    from datetime import timedelta
    from homeassistant.util import dt as dt_util
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    coordinator.data['partitions']['1']['state'] = 'exit'
    countdown = ExitCountdown(coordinator, '1')
    assert countdown.native_value is None  # panel has not supplied a countdown
    coordinator.exit_deadlines['1'] = dt_util.utcnow() - timedelta(seconds=1)
    assert countdown.native_value == 0
    assert PartitionAlarm(coordinator, '1').alarm_state == AlarmControlPanelState.ARMING
    deadline = coordinator.exit_deadlines['1']
    coordinator._update_exit_deadline('1', {'state': 'exit'})
    assert coordinator.exit_deadlines['1'] == deadline
    coordinator._update_exit_deadline('1', {'state': 'unset'})
    assert '1' not in coordinator.exit_deadlines


async def test_optional_occupancy_example_hold_and_unavailability(hass):
    from pathlib import Path
    import yaml
    from homeassistant.setup import async_setup_component
    config = yaml.safe_load((Path(__file__).parents[1] / 'examples/room_occupancy.yaml').read_text())
    hass.states.async_set('binary_sensor.your_room_pir', 'on')
    assert await async_setup_component(hass, 'template', config)
    await hass.async_block_till_done()
    helper = next(s for s in hass.states.async_all() if s.entity_id != 'binary_sensor.your_room_pir'
                  and s.attributes.get('device_class') == 'occupancy')
    assert helper.state == 'on'
    hass.states.async_set('binary_sensor.your_room_pir', 'off')
    await hass.async_block_till_done()
    assert hass.states.get(helper.entity_id).state == 'on'

    hass.states.async_set('binary_sensor.your_room_pir', 'unavailable')
    await hass.async_block_till_done()
    assert hass.states.get(helper.entity_id).state == 'unavailable'
    hass.states.async_set('binary_sensor.your_room_pir', 'on')
    await hass.async_block_till_done()
    assert hass.states.get(helper.entity_id).state == 'on'


async def test_alarm_event_during_storage_write_wins_over_snapshot(hass, client, panel):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(panel))
    entry.add_to_hass(hass)
    coordinator = BmsCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    panel['partition']['in-alarm'] = True
    fired = False
    async def save(payload):
        nonlocal fired
        if not fired:
            fired = True
            data, changed = apply_event(coordinator.data, Event('partition', '{"id":"1","in-alarm":false}'))
            coordinator._revision += 1
            coordinator._changes[changed] = coordinator._revision
            coordinator.async_set_updated_data(data)
            await coordinator._track_alarms(data)
    with patch.object(coordinator._store, 'async_save', save):
        await coordinator.async_refresh()
    assert coordinator.data['partitions']['1']['in-alarm'] is False
