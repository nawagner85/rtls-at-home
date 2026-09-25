# Configuration

## Setup

Add the integration under **Settings → Devices & services → Add integration → RTLS@Home**.

| Field | What to enter |
|---|---|
| Engine URL | The engine's address on your network, e.g. `http://192.0.2.10:8765`. |
| Ingest token | The shared secret the engine was started with (its `RTLS_INGEST_TOKEN`). |

The integration checks both before it finishes. You can connect more than one engine; each URL can be added once.

## Options

**Settings → Devices & services → RTLS@Home → Configure.** Changing an option reloads the integration.

| Option | Default | Range | Effect |
|---|---|---|---|
| Bluetooth read interval | 0.49 s | 0.2–5 s | How often the bridge reads Home Assistant's Bluetooth scanners and sends a batch. A proxy refreshes a sighting about once a second, so anything under 0.5 s catches every refresh. Longer intervals lose samples. |
| Nearby-device list interval | 10 s | 2–300 s | How often the list of nearby devices is sent to the engine (used to choose tags to track). |

## Sensors

For each device the engine tracks: `sensor.<device>_room`, `sensor.<device>_floor` and `sensor.<device>_location`.
Attributes are listed in the [README](../README.md#entities). Which devices are tracked is decided on the engine.

## Voice assistants

To answer "Where is …?", expose the `_location` sensors to Assist under
**Settings → Voice assistants → Expose**. The sentence is written for being read aloud.
