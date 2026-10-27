#!/usr/bin/env python3
"""
Shrink an oversized Hermes state.db-wal and verify integrity afterwards.

Recent Hermes versions set `PRAGMA journal_size_limit` to 64 MiB on every
connection, so the WAL normally stays at or below 64 MB and you do not need
this script. Use it only when audit_db_storage.py shows a WAL far above that
(older Hermes, or a stuck long-lived reader).

The gateway holds a read snapshot that keeps checkpoints from finishing, so
stop it first, from a shell that is not running inside the gateway:

    hermes gateway stop
    python hermes/wal_checkpoint_external.py
    hermes gateway start

Never kill the gateway with `kill -9` / `taskkill /F` while it writes.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB, WAL, SHM = H / "state.db", H / "state.db-wal", H / "state.db-shm"
ATTEMPTS = 20


def mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def main() -> int:
    if not DB.exists():
        print(f"{DB} not found")
        return 1
    before = sum(mb(p) for p in (DB, WAL, SHM))
    print(f"before: db {mb(DB):.1f} MB, wal {mb(WAL):.1f} MB, total {before:.1f} MB")

    con = sqlite3.connect(str(DB), timeout=60, isolation_level=None)
    con.execute("PRAGMA busy_timeout = 60000")
    done = False
    for attempt in range(1, ATTEMPTS + 1):
        busy, log, ckpt = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        print(f"  [{attempt:2d}] busy={busy} log={log} ckpt={ckpt} wal={mb(WAL):.1f} MB")
        # busy=0 alone does not mean success: the file must actually shrink.
        if busy == 0 and log == ckpt and mb(WAL) < 1:
            done = True
            break
        time.sleep(3)
    con.close()
    if not done:
        print("WAL not truncated. Something still holds the database open "
              "(gateway, desktop app, cron). Stop it and retry.")
        return 2

    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    check = con.execute("PRAGMA quick_check").fetchone()[0]
    print(f"quick_check: {check}")
    n_msg = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    try:
        n_fts = con.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        print(f"messages {n_msg:,} | messages_fts {n_fts:,}")
        if n_fts != n_msg:
            print("FTS index out of sync: run `hermes sessions optimize`.")
    except sqlite3.OperationalError:
        print(f"messages {n_msg:,}")
    con.close()
    after = sum(mb(p) for p in (DB, WAL, SHM))
    print(f"after: total {after:.1f} MB (freed {before - after:.1f} MB)")
    return 0 if check == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
