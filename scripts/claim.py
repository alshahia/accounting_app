"""Optional hard locks for exclusive resources.

  claim.py acquire --name RESOURCE --owner WHO [--ttl MINUTES]
  claim.py release --name RESOURCE --owner WHO
  claim.py status [--json]
  claim.py doctor
  [--root DIR] applies to every subcommand

Locks live in `docs/.locks/`, which is gitignored: they are machine state, not pack
state, and they do not synchronise across clones. Creation is O_CREAT|O_EXCL, so two
agents cannot both win. This script never removes a lock it did not create, and never
removes a live one.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _packmodel as pm

LOCK_DIR = "docs/.locks"


class Refused(Exception):
    pass


def lock_path(root: Path, name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return root / LOCK_DIR / (safe + ".lock")


def describe(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    info = {"owner": lines[0] if lines else ""}
    for ln in lines[1:]:
        if "=" in ln:
            k, v = ln.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_stale(info: dict) -> bool:
    exp = pm.parse_ts(info.get("expires_at", ""))
    if exp is not None and exp <= datetime.now().astimezone():
        return True
    if info.get("host") == socket.gethostname():
        pid = int(re.sub(r"\D", "", info.get("pid", "") or "0") or 0)
        if pid and not process_alive(pid):
            return True
    return False


def acquire(root: Path, name: str, owner: str, ttl: int) -> dict:
    path = lock_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    exp = now + timedelta(minutes=ttl)
    body = "%s\npid=%d\nhost=%s\nclaimed_at=%s\nexpires_at=%s\n" % (
        owner, os.getpid(), socket.gethostname(),
        now.isoformat(timespec="seconds"), exp.isoformat(timespec="seconds"))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        holder = describe(path)
        state = "stale" if is_stale(holder) else "live"
        raise Refused("%s is held by %s (%s, expires %s)" % (name, holder.get("owner"), state, holder.get("expires_at")))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return {"name": name, "owner": owner, "status": "acquired",
            "expires_at": exp.isoformat(timespec="seconds"), "path": str(path)}


def release(root: Path, name: str, owner: str) -> dict:
    path = lock_path(root, name)
    if not path.is_file():
        return {"name": name, "status": "absent"}
    info = describe(path)
    if info.get("owner") != owner:
        raise Refused("%s is held by %s; only the owner releases a lock" % (name, info.get("owner")))
    path.unlink()
    return {"name": name, "status": "released"}


def status(root: Path) -> list[dict]:
    directory = root / LOCK_DIR
    if not directory.is_dir():
        return []
    out = []
    for p in sorted(directory.glob("*.lock")):
        info = describe(p)
        info["name"] = p.stem
        info["state"] = "stale" if is_stale(info) else "live"
        out.append(info)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Optional hard locks for exclusive resources.")
    ap.add_argument("command", choices=("acquire", "release", "status", "doctor"))
    ap.add_argument("--name", default="")
    ap.add_argument("--owner", default="")
    ap.add_argument("--ttl", type=int, default=120, help="minutes; default 120")
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "acquire":
            if not args.name or not args.owner:
                print("ERROR: acquire needs --name and --owner", file=sys.stderr)
                return 2
            result = acquire(root, args.name, args.owner, args.ttl)
        elif args.command == "release":
            if not args.name or not args.owner:
                print("ERROR: release needs --name and --owner", file=sys.stderr)
                return 2
            result = release(root, args.name, args.owner)
        elif args.command == "status":
            result = status(root)
        else:
            all_locks = status(root)
            result = {"locks": all_locks, "stale": [l["name"] for l in all_locks if l["state"] == "stale"]}
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if isinstance(result, list):
            for l in result:
                print("%-24s %-6s owner=%s expires=%s" % (l["name"], l["state"], l.get("owner"), l.get("expires_at")))
            if not result:
                print("no locks")
        elif isinstance(result, dict) and "locks" in result:
            print("stale locks: %s" % (", ".join(result["stale"]) or "none"))
        else:
            print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
