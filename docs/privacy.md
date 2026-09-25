# Privacy

RTLS@Home keeps everything on your network. It has no cloud service, sends no telemetry and makes no connections to
the internet.

## What the bridge sends, and to whom

Only to the engine URL you configured:

- **Sightings of wanted devices:** device key, which proxy heard it, signal strength and when. Only for devices the
  engine lists as wanted (the ones you track, reference beacons, and calibration tags).
- **Scanner health:** each proxy's address, Home Assistant's name for it, and how long since it last heard anything.
- **Nearby devices:** about every 10 seconds, the devices heard in the last minute (address and/or iBeacon identity,
  advertised name, signal strength, how many proxies heard it). This lets you choose new devices to track. It can
  include neighbours' devices that your proxies happen to hear.
- **Onboarding detail:** only while the engine's onboarding panel is open (and for at most 30 s after it closes),
  the nearby-device list comes every 5 s and adds each proxy's recent signal readings, when the device was first
  heard, and its address type, so the engine can place devices on its map. This also covers devices that aren't
  yours. The engine keeps them in memory only and never writes them to disk.

## What Home Assistant stores

The room, floor and location sensors, like any other sensor, in Home Assistant's database. Position attributes
(`x`, `y`, `z`, `confidence`, `radius_m`) are excluded from the recorder, and states are written at most every 10
seconds unless the room or floor changes.

## Who can see locations

Anyone who can see these sensors in Home Assistant can see where tracked devices are. Treat them like any
presence data: limit dashboard access, and think before exposing them to voice assistants that other people use.
