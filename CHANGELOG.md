# Changelog

All notable changes to RTLS@Home. Versions follow [Semantic Versioning](https://semver.org/).

## 0.5.0 - 2026-10-02

### Added

- **The engine, as a Home Assistant App.** This repository is also an App repository: add it in the App store and
  install **RTLS@Home**. A new house starts on the App's map - draw the floors and rooms, place the receivers Home
  Assistant hears - and tracks on a default model until five calibrated spots fit it.
- **Finds the RTLS@Home App.** When the engine runs as a Home Assistant App, it announces itself and Home Assistant
  shows RTLS@Home as discovered. With no RTLS@Home integration yet, confirming connects to the App, with no address or
  token to type. With one connected to another engine, confirming *moves* it to the App: the same integration entry,
  so every device and sensor keeps its name, id and history. When the App's token changes, the integration takes it
  without asking.
- **A new house is set up first.** The engine's status is `setup` until its house has rooms and a placed receiver;
  until then it tracks nothing, and its page opens on the map.

## 0.4.0 - 2026-10-02 (released with 0.5.0)

### Added

- **Presence per area.** Every Home Assistant area that a room of the engine's map is linked to gets a small device
  ("Kitchen presence") with an occupancy sensor, suggested into that area so it shows on the area's page. It is on
  while a tracked device is present in any room linked to the area, with the devices' names and their count as
  attributes. Logical rooms of the map get none.
- **The engine as a device**, with its status (`tracking`, or `applying` while a new house is applied), how many
  receivers it hears of all it knows, and how many tracked devices are present. Its *Visit* link, and every tracked
  device's, opens the engine's page.
- **Receivers.** Each receiver the engine knows gets a device with *Heard*, *Signal correction* (dB) and *Placed in*
  (room and floor), connected via the receiver's own Home Assistant device (its ESPHome proxy) when Home Assistant
  has one with the same Wi-Fi MAC or Bluetooth address.
- **Download diagnostics** (the ingest token redacted), and an icon and a name for every entity.

### Changed

- The room sensor's `ha_area` attribute is the area the map links the room to; a same-name area only when the map
  leaves the room unlinked.
- The bridge asks the engine for its meta with every census (protocol: `meta`). With an engine that doesn't send one,
  nothing new appears.

## 0.3.0 - 2026-10-02 (released with 0.5.0)

### Added

- Home Assistant's floors and areas travel with the census, so the engine's map editor can name the floors and
  rooms of the house map after them (or keep a room as a logical room of the map).
- Census rows carry what identifies a device besides its name: manufacturer ids with their first bytes, service
  ids with their first data bytes, and the advertised transmit power. The engine uses them to show each onboarding
  candidate's likely maker and kind (for example "Apple · AirPods or Beats" or "Sonos"). Empty fields are left out.

## 0.2.0 - 2026-09-25

### Added

- Away: a tracked device that goes quiet stays listed. Its room and floor read `Away`, and its location says when
  and where it was last detected.
- A `device_tracker` entity per tracked device (`home` / `not_home`).
- Devices removed in the engine are deleted from Home Assistant; renames in the engine follow through, but never
  over a name set in Home Assistant.
- Onboarding support: while the engine's onboarding panel is open, a detailed census every 5 s (each proxy's
  median signal, when the device was first heard, and its address type), so the engine can place untracked
  devices on its map.

## 0.1.0 - 2026-09-25

First release: the Home Assistant bridge.

### Added

- Reads Home Assistant's Bluetooth scanners every 0.49 s (configurable) and sends each proxy's new sightings to an
  RTLS@Home engine (ingest protocol v1), for the devices the engine asks for.
- Device keys compatible with Bermuda: lower-case MAC addresses and iBeacon `uuid_major_minor`.
- Scanner health (seconds since each proxy's last advertisement) with every batch.
- A census of nearby devices every 10 s (configurable), for choosing tags to track.
- Room, floor and location sensors for each tracked device, with position, confidence, closest named furniture and
  the matching Home Assistant area as attributes; throttled state writes.
- Config flow (engine URL, ingest token, protocol check) and options (read and census intervals).
- Back-off while the engine is unreachable; sensors go unavailable after 30 s without a reply.
