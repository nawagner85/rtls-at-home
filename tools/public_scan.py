"""Fail if the public tree contains personal data: real-looking MAC addresses, IPv4 addresses, iBeacon-style
UUIDs, or any word from a private list kept OUTSIDE this repository.

    python tools/public_scan.py [ROOT] [--private-words FILE]

Documentation values are allowed: MACs 00:00:5E:00:53:xx (RFC 7042), IPs 192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24 (RFC 5737), 127.0.0.1, 0.0.0.0, and the example UUID 00112233445566778899aabbccddeeff.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MAC = re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.I)
ALLOWED_IP = re.compile(r"^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|127\.0\.0\.1$|0\.0\.0\.0$)")
ALLOWED_UUIDS = {"00112233445566778899aabbccddeeff"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini", ".sh", ""}


@dataclass(frozen=True)
class Finding:
    kind: str
    value: str


def findings_in(text: str, private_words: list[str]) -> list[Finding]:
    out: list[Finding] = []
    for m in MAC.finditer(text):
        if not m.group(0).lower().replace("-", ":").startswith("00:00:5e:00:53:"):
            out.append(Finding("mac", m.group(0)))
    for m in IPV4.finditer(text):
        if not ALLOWED_IP.match(m.group(0)):
            out.append(Finding("ipv4", m.group(0)))
    for m in UUID.finditer(text):
        if m.group(0).lower().replace("-", "") not in ALLOWED_UUIDS:
            out.append(Finding("uuid", m.group(0)))
    for w in private_words:
        if w and re.search(r"(?<![0-9a-z])" + re.escape(w) + r"(?![0-9a-z])", text, re.I):
            out.append(Finding("private", w))
    return out


def load_words(path: Path) -> list[str]:
    """Private words, one per line; blank lines and lines starting with # are ignored."""
    lines = (line.strip() for line in Path(path).read_text().splitlines())
    return [w for w in lines if w and not w.startswith("#")]


def scan(root: Path, private_words: list[str]) -> list[tuple[Path, Finding]]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(p in SKIP_DIRS for p in path.parts) or path.suffix not in TEXT_SUFFIXES:
            continue
        if path.name in ("public_scan.py", "test_public_scan.py"):
            continue                    # the scanner and its test hold deliberate fake identifiers
        hits += [(path, f) for f in findings_in(path.read_text(errors="ignore"), private_words)]
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--private-words", help="file with one private word per line (kept outside the repo)")
    a = ap.parse_args()
    words = load_words(Path(a.private_words)) if a.private_words else []
    hits = scan(Path(a.root), words)
    for path, f in hits:
        print(f"{path}: {f.kind}: {f.value if f.kind != 'private' else '<private word>'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
