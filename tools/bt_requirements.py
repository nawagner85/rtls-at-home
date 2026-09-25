"""Print the pip requirements of Home Assistant's Bluetooth integration and everything it depends on.

The test harness installs Home Assistant itself, but not the requirements of built-in integrations. Importing
`homeassistant.components.bluetooth` needs them (bleak, habluetooth, and via `usb`, aiousbwatcher, ...).
"""

from __future__ import annotations

import json
from pathlib import Path

import homeassistant

BASE = Path(homeassistant.__file__).parent / "components"


def requirements(root: str = "bluetooth") -> list[str]:
    seen: set[str] = set()
    reqs: set[str] = set()
    todo = [root]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        manifest = BASE / name / "manifest.json"
        if not manifest.is_file():
            continue
        data = json.loads(manifest.read_text())
        reqs.update(data.get("requirements", []))
        todo += data.get("dependencies", []) + data.get("after_dependencies", [])
    return sorted(reqs)


if __name__ == "__main__":
    print("\n".join(requirements()))
