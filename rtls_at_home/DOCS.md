# RTLS@Home

RTLS@Home works out which room each tracked Bluetooth device is in, and roughly where in it, from the ESPHome
Bluetooth proxies Home Assistant already uses. This App is the engine and its page: the live map, the map editor,
receiver placement, the outlet survey and placement advisor, calibration and onboarding. Open it from **RTLS@Home**
in the sidebar.

The RTLS@Home **integration** (on HACS) is the other half. It reads Home Assistant's Bluetooth sightings, sends them
to this App, and turns the answers into sensors: each device's room, floor and location, presence per area, and the
receivers' health.

## Connecting the integration

When the App starts it tells Home Assistant where it is. **Settings → Devices & services** then shows RTLS@Home as
discovered:

- With no RTLS@Home integration yet, confirm, and it connects to the App.
- With one already connected to another engine (a server elsewhere), confirm to **move it to the App**. Every device
  and sensor keeps its name, id and history.

The App makes its ingest token itself and hands it over; you never type it.

## The first start

The App builds its maps and geometry the first time it starts: about half a minute on a Raspberry Pi 5, while the
panel says the engine is starting and reloads by itself. Later starts take about ten seconds.

## Moving an existing installation

To bring the house, its history, calibration, devices and outlets from an engine that ran elsewhere, copy that
engine's data folder into this App's config folder as `import/`, ideally before the App's first start. Over Samba or SSH the config folder is `/addon_configs/<this App's slug>/`. On its
next start the App copies the folder in once, leaving out session logs, builds and caches, and never over a house it
already has. The log says what it did.

If the App already has a house of its own (you used it before moving), also put an empty file named `.force` in
`import/`. The App then sets its own data aside in a `replaced-<time>` folder (nothing is deleted), imports, and
removes `.force`, so it happens once.

## Data and backups

Everything lives in the App's data folder and goes into Home Assistant's backups with the App, except session logs
(the last 72 hours of readings) and the builds and caches the App can rebuild.

## Network

The integration reaches the App over Home Assistant's internal network, and you reach the page through the sidebar,
logged in to Home Assistant. The engine's port is not published. You can publish it in the App's **Network**
settings for debugging, but the page and its API then answer anyone on your network.

## Resources

The engine needs about 400 MB of memory and part of one core. Applying a new house starts a second engine for a
minute to check the house before switching to it.
