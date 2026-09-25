# Install

You need Home Assistant 2026.9 or newer with the Bluetooth integration running, and a reachable RTLS@Home engine.

## With HACS (recommended)

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/nawagner85/rtls-at-home` with the type **Integration**.
3. Find **RTLS@Home** in HACS and select **Download**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration**, search for **RTLS@Home**, and follow the steps in
   [configuration](configuration.md).

## By hand

1. Download the latest release from GitHub.
2. Copy `custom_components/rtls_at_home` into your Home Assistant configuration folder, so that you have
   `<config>/custom_components/rtls_at_home/manifest.json`.
3. Restart Home Assistant, then add the integration as in step 5 above.

## Update

HACS shows updates like any other integration. After updating, restart Home Assistant.

## Remove

**Settings → Devices & services → RTLS@Home → Delete**, then remove it in HACS (or delete the folder) and restart.
