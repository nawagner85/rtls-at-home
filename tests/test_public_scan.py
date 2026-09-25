"""The personal-data scanner that guards the public repo."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_scan", Path(__file__).parents[1] / "tools" / "public_scan.py")
scan = importlib.util.module_from_spec(spec)
sys.modules["public_scan"] = scan  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(scan)


def test_flags_real_looking_identifiers():
    text = "mac 12:34:56:78:9A:BC ip 10.1.2.3 uuid 0123456789abcdef0123456789abcdef host nas.home.lan"
    kinds = {f.kind for f in scan.findings_in(text, private_words=["home.lan"])}
    assert kinds == {"mac", "ipv4", "uuid", "private"}


def test_allows_documentation_values():
    text = "00:00:5E:00:53:0A 192.0.2.10 198.51.100.1 203.0.113.9 127.0.0.1 0.0.0.0 00112233445566778899aabbccddeeff"
    assert scan.findings_in(text, private_words=[]) == []


def test_private_words_match_whole_words_only():
    assert [f.kind for f in scan.findings_in("Where is Fido?", private_words=["fido"])] == ["private"]
    assert scan.findings_in("fidonet and confidor", private_words=["fido"]) == []


def test_word_list_ignores_comments_and_blank_lines(tmp_path):
    f = tmp_path / "words.txt"
    f.write_text("# a comment\n\nalpha\n  beta  \n")
    assert scan.load_words(f) == ["alpha", "beta"]
