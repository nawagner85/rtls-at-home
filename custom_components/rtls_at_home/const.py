"""Constants for RTLS@Home."""

from __future__ import annotations

DOMAIN = "rtls_at_home"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_POLL_INTERVAL = "poll_interval"
CONF_CENSUS_INTERVAL = "census_interval"

DEFAULT_POLL_INTERVAL = 0.49  # s: faster than twice the ~1 s rate a scanner refreshes a sighting (Nyquist)
DEFAULT_CENSUS_INTERVAL = 10.0  # s between nearby-device censuses
CENSUS_WINDOW = 60.0  # s: a device heard within this counts as nearby
CENSUS_MAX = 500  # devices per census, loudest first
DEDUPE_KEEP = 300.0  # s: forget (scanner, address) dedupe entries older than this
STALE_AFTER = 30.0  # s without an engine reply before entities go unavailable
BACKOFF_MIN = 0.5
BACKOFF_MAX = 30.0
WRITE_EVERY = 10.0  # s: most often a sensor writes state unless its room or floor changed
PROTOCOL_VERSION = 1

SIGNAL_UPDATE = "rtls_at_home_update_{}"  # .format(entry_id)
