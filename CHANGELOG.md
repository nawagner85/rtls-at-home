# Changelog

All notable changes to RTLS@Home. Versions follow [Semantic Versioning](https://semver.org/).

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
