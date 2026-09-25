# Development

## Layout

```text
custom_components/rtls_at_home/
    __init__.py       setup/unload, device deletion
    config_flow.py    config and options flows
    client.py         HTTP client for the engine (protocol v1)
    reader.py         Bluetooth scanners -> sightings, iBeacon keys, census (pure Python)
    runner.py         the poll/post loop, back-off, the engine's answers
    sensor.py         room, floor and location sensors
    const.py          constants
    strings.json      UI text (translations/en.json is a copy)
tests/                pytest with pytest-homeassistant-custom-component
tools/
    bt_requirements.py  requirements of HA's Bluetooth integration, for the test environment
    public_scan.py      personal-data check (CI and pre-commit)
docs/                 this documentation
```

## Tests

Home Assistant 2026.9 needs Python 3.14 and does not run on Windows. The simplest way to get a matching environment
is a container:

```sh
docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/w -w /w python:3.14-slim sh -c '
  pip install -q -r requirements_test.txt &&
  python tools/bt_requirements.py > /tmp/bt.txt && pip install -q -r /tmp/bt.txt &&
  python -m pytest -q -p no:cacheprovider'
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component`, which pins the matching Home Assistant.
`tools/bt_requirements.py` adds the requirements of Home Assistant's Bluetooth integration (the harness does not
install built-in integrations' requirements).

Without Docker: a Python 3.14 virtual environment on Linux or macOS, then the same three commands.

## Lint and the personal-data check

```sh
pip install ruff pre-commit
ruff check .
python tools/public_scan.py .
pre-commit install        # runs both on every commit
```

`public_scan.py` fails on real-looking MAC addresses, IPv4 addresses and UUIDs. Use documentation values in examples
and tests: MACs `00:00:5E:00:53:xx`, IPs `192.0.2.x` / `198.51.100.x` / `203.0.113.x`, the UUID
`00112233445566778899aabbccddeeff`.

## Protocol changes

The bridge and the engine share [protocol.md](protocol.md). A breaking change bumps `v`. The config flow refuses an
engine that answers with a different version.
