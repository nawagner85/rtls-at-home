# Ingest protocol, version 1

The bridge and the engine talk over HTTP with JSON bodies. Every request carries the shared ingest token:

```http
Authorization: Bearer <token>
```

## `GET /api/ingest/hello`

Checks the protocol version and the token. The config flow calls it before creating an entry.

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"v": 1, "ok": true, "mode": "ingest"}` | Token accepted. `mode` is informational (`ingest`, or `shadow` while an engine runs the old and new feeds side by side). |
| 401 | `{"error": "bad token"}` | Wrong or missing token. |
| 503 | `{"error": "..."}` | The engine is not accepting bridge data. |

## `POST /api/ingest`

One batch per poll (about twice a second).

### Request

```json
{
  "v": 1,
  "sent_wall": 1790000000.123,
  "sent_mono": 812345.678,
  "adverts": [["00:00:5e:00:53:0a", "00:00:5e:00:53:f1", -71, 812345.41]],
  "scanners": [["00:00:5e:00:53:f1", "living-room-proxy", 0.4]],
  "census": [{"keys": ["00:00:5e:00:53:0a"], "name": "tag", "ibeacon": false, "rssi": -60, "scanners": 5, "age": 1.2}]
}
```

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Protocol version, `1`. |
| `sent_wall` | float | Home Assistant's wall clock (Unix seconds) when the batch was built. |
| `sent_mono` | float | Home Assistant's monotonic clock at the same moment. |
| `adverts` | list | New sightings since the last batch, only for wanted keys: `[key, scanner, rssi, stamp]`. |
| `adverts[][0]` key | string | Lower-case address, or iBeacon `<uuid>_<major>_<minor>`. |
| `adverts[][1]` scanner | string | The proxy's source address, lower case. |
| `adverts[][2]` rssi | int | dBm. |
| `adverts[][3]` stamp | float | When the proxy's sighting arrived, in Home Assistant's monotonic clock. |
| `scanners` | list | Every active scanner: `[source address, Home Assistant's name for it, seconds since its last advertisement]`. |
| `census` | list | Optional, about every 10 s: devices heard in the last 60 s, loudest first, at most 500. Each entry has `keys`, `name` (may be null), `ibeacon`, `rssi` (best), `scanners` (how many heard it), `age` (seconds). |

**Clocks.** The engine converts a stamp to its own clock as
`wall = sent_wall - (sent_mono - stamp)`, so the two machines' clocks never need to agree. A converted stamp later
than the engine's own "now" is clamped to now.

### Response

```json
{
  "wanted": ["00:00:5e:00:53:0a", "00112233445566778899aabbccddeeff_100_40004"],
  "tracked": [
    {"key": "00:00:5e:00:53:0a", "name": "Keys", "room": "Living Room", "floor": "Main", "p_room": 0.8,
     "x": 5.2, "y": 2.1, "z": 3.8, "r68": 1.5, "verdict": "CALL", "age": 1.0,
     "near": "ottoman", "description": "near the ottoman in the Living Room"}
  ]
}
```

| Field | Meaning |
|---|---|
| `wanted` | Keys to send sightings for from now on. |
| `tracked[].key`, `name` | The device and its display name. |
| `room`, `floor` | Best room and floor (null while the engine has no estimate). |
| `p_room` | Probability of that room, 0–1. |
| `x`, `y`, `z` | Position in metres in the engine's floor-plan frame. |
| `r68` | Radius in metres that holds the device with 68 % probability. |
| `verdict` | `CALL` when confident, `LOW-CONF` otherwise. |
| `age` | Seconds since the device was last heard. |
| `near` | Closest named furniture within 1 m on the same floor, or null. |
| `description` | A sentence for voice assistants, or null. |

### Errors

| Status | Meaning |
|---|---|
| 400 | Malformed batch (wrong version, missing field, bad type). |
| 401 | Wrong or missing token. |
| 413 | Body over 1 MB. |
| 503 | The engine is not accepting bridge data. |
