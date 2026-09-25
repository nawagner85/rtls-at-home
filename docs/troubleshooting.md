# Troubleshooting

## Setup errors

| Message | Cause | Fix |
|---|---|---|
| Can't reach the engine at that address (`cannot_connect`) | Wrong URL, the engine is down, or a firewall is in the way. | Open `<engine URL>/api/ingest/hello` from a machine on the same network: you should get a 401 JSON error, which means the engine is up. |
| The engine rejected the token (`invalid_auth`) | The token doesn't match the engine's `RTLS_INGEST_TOKEN`. | Copy the token again; watch for spaces or line breaks. |
| The engine speaks a different protocol version (`unsupported_version`) | The bridge and the engine are from different generations. | Update whichever is older. |
| This engine is already connected (`already_configured`) | That URL already has an entry. | Use the existing entry, or delete it first. |

## Sensors are unavailable

- **All of them:** the engine hasn't replied for 30 seconds. Check that it's running. The log shows
  `RTLS@Home engine unavailable (...)` at most once a minute, then `RTLS@Home engine reachable again`.
- **One device:** the engine is no longer tracking it. Track it again on the engine, or delete the device from its
  page in Home Assistant.

## Sensors never appear

The engine decides which devices are tracked. A device gets sensors after the engine first reports it. Check that
the engine lists the device as tracked and that at least one proxy hears it.

## Positions look wrong

The bridge only transports readings; positions come from the engine and its calibration. Check that every proxy is
reporting (the engine marks silent proxies dead), and that proxies haven't moved since calibration.

## Logs

Add to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.rtls_at_home: debug
```
