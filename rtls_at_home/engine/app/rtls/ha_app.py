"""The engine as a Home Assistant App (spec docs/specs/2026-10-01-ha-app-design.md): what happens before the server
starts - the ingest token, a one-time import of an existing installation's data folder - and the announcement that
lets the RTLS@Home integration find the App (Supervisor discovery).

    python ha_app.py prepare      the token and the import, before the server starts (run.sh)
    python ha_app.py announce     tell the Supervisor where the engine is and its token
"""
import json
import os
import secrets
import shutil
import socket
import sys
import time
import urllib.request

DATA = os.environ.get("RTLS_SESSIONS", "/data")
TOKEN_FILE = os.environ.get("RTLS_INGEST_TOKEN_FILE", os.path.join(DATA, "ingest_token"))
IMPORT_DIR = os.environ.get("RTLS_IMPORT_DIR", "/config/import")    # the App's config folder: /addon_configs/<slug>
PORT = int(os.environ.get("RTLS_PORT", "8765"))
SKIP_DIRS = {"build", "cache"}                                       # rebuilt from the house on the first start
MARK = ".imported"
FORCE = ".force"                       # in the import folder: import over the App's own house (the cutover)
KEEP = {"ingest_token"}                # what a forced import leaves in place
STAGE = ".import-stage"


def log(*a):
    print(time.strftime("%H:%M:%S"), "app:", *a, flush=True)


def ensure_token(path):
    """The ingest token: made on the first start (64 hex digits, readable by the App only), then kept."""
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def _skip(rel_dir, name):
    return (rel_dir == "" and name in SKIP_DIRS) or name.endswith((".jsonl", ".jsonl.gz"))


def import_once(src, data):
    """Copies an existing installation's data folder from `src` into `data` once: "imported", "done before" (it ran
    already), "have house" (never over a house the App has), or "none" (nothing to import). Session logs, builds and
    caches stay behind: the first start rebuilds what it needs. A `.force` file in `src` imports anyway - over a house
    the App made itself - after setting the App's data aside in `replaced-<time>/` (the ingest token stays); the
    file is removed, so it happens once."""
    force = os.path.exists(os.path.join(src, FORCE))
    if not force and os.path.exists(os.path.join(data, MARK)):
        return "done before"
    if not os.path.isdir(src) or not [n for n in os.listdir(src) if n != FORCE]:
        return "none"
    if not force and os.path.exists(os.path.join(data, "house.json")):
        return "have house"
    os.makedirs(data, exist_ok=True)
    stage = os.path.join(data, STAGE)                  # copied here first: a failed copy changes nothing else
    shutil.rmtree(stage, ignore_errors=True)
    copied = 0
    try:
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            rel = "" if rel == "." else rel
            dirs[:] = [d for d in dirs if not _skip(rel, d)]
            os.makedirs(os.path.join(stage, rel), exist_ok=True)
            for name in files:
                if not _skip(rel, name) and not (rel == "" and name == FORCE):
                    shutil.copy2(os.path.join(root, name), os.path.join(stage, rel, name))
                    copied += 1
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if force:
        aside = os.path.join(data, "replaced-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        os.makedirs(aside)
        for name in os.listdir(data):
            if name not in KEEP and name != STAGE and not name.startswith("replaced-"):
                shutil.move(os.path.join(data, name), os.path.join(aside, name))
    for name in os.listdir(stage):
        shutil.move(os.path.join(stage, name), os.path.join(data, name))
    os.rmdir(stage)
    with open(os.path.join(data, MARK), "w", encoding="utf-8") as f:
        json.dump(dict(at=time.time(), files=copied, source=src, forced=force), f)
    if force:
        os.remove(os.path.join(src, FORCE))
    return "imported"


def announce(token, host, port, url="http://supervisor", supervisor_token=None, opener=urllib.request.urlopen):
    """Tells the Supervisor where the engine is (service rtls_at_home), so the integration offers to connect to it.
    False, logged, when it can't: outside an App (no Supervisor token) or the Supervisor refuses."""
    if not supervisor_token:
        log("not running as a Home Assistant App: no discovery")
        return False
    body = json.dumps({"service": "rtls_at_home", "config": {"host": host, "port": port, "token": token}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/discovery", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {supervisor_token}",
                                          "Content-Type": "application/json"})
    try:
        with opener(req, timeout=10) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception as e:                      # the engine runs without discovery; the integration can still be set up
        log("discovery failed:", type(e).__name__, e)
        return False
    log("announced to Home Assistant as", f"{host}:{port}" if ok else "(refused)")
    return ok


def house_note(data, repo_house):
    """What the first start says about the house: None when the data folder has one; the image's house when the image
    carries one (a developer's build); else setup mode (a household's App - plan 2026-10-01-a3)."""
    if os.path.exists(os.path.join(data, "house.json")):
        return None
    if os.path.exists(repo_house):
        return "no house in the data folder yet: the engine starts on the house in the image"
    return "no house yet: the engine starts in setup mode - draw it in the map"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "prepare":
        ensure_token(TOKEN_FILE)
        log("import:", import_once(IMPORT_DIR, DATA))
        note = house_note(DATA, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "house", "house.json"))
        if note:
            log(note)
        return 0
    if cmd == "announce":
        announce(ensure_token(TOKEN_FILE), socket.gethostname(), PORT,
                 supervisor_token=os.environ.get("SUPERVISOR_TOKEN"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
