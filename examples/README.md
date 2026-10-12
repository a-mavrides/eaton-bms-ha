# Optional room occupancy helper

The integration keeps raw PIR events unchanged. `room_occupancy.yaml` shows how
Home Assistant can hold an occupancy helper on for 10 minutes after the PIR clears.
A new activation restarts the hold period. Change the delay per room.

Replace the placeholder PIR entity in both `state` and `availability`. Merge the
example into an existing `template:` section rather than creating a duplicate
section. Reload Template entities after checking configuration.

Alternatively use **Settings → Devices & services → Helpers → Create helper →
Template → Binary sensor**. Select Occupancy as the device class, enter the same
State/Availability templates, and set Delay off under Additional options.

If the source becomes unavailable, this example becomes unavailable too. It does
not treat loss of connection as an empty room. It does not retain its hold timer
through Home Assistant restarts; do not use it as the sole basis for sensitive
absence decisions. For stationary people, combine PIR data with mmWave or other
presence evidence. Bedroom occupancy needs more than movement detection.

This file is not installed automatically and does not control lights, AC or alarms.
Use a unique helper name/unique_id for each room. Official template documentation:
https://www.home-assistant.io/integrations/template/
