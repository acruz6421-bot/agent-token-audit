#!/usr/bin/env python3
"""
Hermes Agent state.db storage audit. Read-only, stdlib only.

    python hermes/audit_db_storage.py

Reports the real disk footprint (db + wal + shm), WAL size against the
64 MiB cap recent Hermes versions apply, VACUUM yield and FTS index weight.
"""
import os
import sqlite3
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = H / "state.db"
WAL_CAP_MB = 64  # hermes_state._WAL_SIZE_LIMIT_BYTES


def mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024 if path.exists() else 0.0


def bar(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main() -> int:
    if not DB.exists():
        print(f"state.db not found at {DB}")
        return 1

    bar("1. DISK FOOTPRINT (db + wal + shm)")
    files = {n: mb(H / n) for n in ("state.db", "state.db-wal", "state.db-shm")}
    for name, size in files.items():
        print(f"   {name:14s} {size:9.1f} MB")
    print(f"   {'TOTAL':14s} {sum(files.values()):9.1f} MB")
    wal = files["state.db-wal"]
    if wal > WAL_CAP_MB * 1.5:
        print(f"   WAL is above the {WAL_CAP_MB} MB cap. Either this Hermes version predates "
              "the cap or a long-lived reader is pinning it. See wal_checkpoint_external.py.")
    else:
        print(f"   WAL within the {WAL_CAP_MB} MB cap: nothing to do.")

    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    cur = con.cursor()

    bar("2. VACUUM YIELD")
    free = cur.execute("SELECT freelist_count * page_size / 1048576.0 "
                       "FROM pragma_freelist_count(), pragma_page_size()").fetchone()[0]
    print(f"   freelist: {free:.1f} MB -> "
          + ("VACUUM is worth it" if free > 20 else "VACUUM would recover little"))

    bar("3. FTS INDEX WEIGHT (about half the DB is normal)")
    total = 0.0
    for t in ("messages_fts_data", "messages_fts_trigram_data", "messages_fts_cjk_data"):
        try:
            rows, size = cur.execute(
                f"SELECT COUNT(*), SUM(LENGTH(block)) / 1048576.0 FROM {t}").fetchone()
        except sqlite3.OperationalError:
            continue
        total += size or 0
        print(f"   {t:28s} {size or 0:8.1f} MB")
    print(f"   {'total':28s} {total:8.1f} MB")
    row = cur.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts_trigram'").fetchone()
    if row and row[0] and "content='messages_fts_trigram_src'" not in " ".join(row[0].split()):
        print("   Legacy trigram layout: `hermes sessions optimize-storage` "
              "(gateway stopped) shrinks it.")

    bar("4. SESSION AGE (started_at is a float epoch)")
    n = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(f"   {n:,} sessions")
    for days in (7, 30, 90):
        c = cur.execute("SELECT COUNT(*) FROM sessions WHERE started_at < "
                        "CAST(strftime('%s','now',?) AS REAL)", (f"-{days} days",)).fetchone()[0]
        print(f"   older than {days:3d}d: {c:6,}")
    print("   Old sessions cost disk only, never tokens. Archiving them saves 0 tokens.")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
