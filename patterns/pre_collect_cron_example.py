#!/usr/bin/env python3
"""
Pre-collect pattern: gather data with a plain script, let the LLM only write.

An agent that searches, scrapes and deduplicates on its own spends one model
call per tool step, and every step re-sends the growing context. A script does
the collection for 0 LLM tokens and prints a compact JSON; the agent gets that
as context and does a single reasoning pass.

Hermes Agent (script must live in ~/.hermes/scripts/):
    hermes cron create "every 6h" "Write a short digest of the new repos above." \
        --script pre_collect_cron_example.py

Claude Code / Codex / any CLI agent:
    python pre_collect_cron_example.py | claude -p "Write a short digest of these repos."
    python pre_collect_cron_example.py | codex exec "Write a short digest of these repos."

With nothing new the script prints nothing, so a `--no-agent` Hermes cron or a
shell `[ -n "$out" ]` guard skips the model call entirely.
"""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("PRE_COLLECT_STATE",
                            Path.home() / ".cache" / "pre_collect_state.json"))
KEEP = 2000  # remembered ids, oldest dropped first
URL = ("https://api.github.com/search/repositories"
       "?q=topic:mcp+stars:>100&sort=updated&per_page=10")


def load_seen() -> list:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("seen", [])
    except (OSError, ValueError):
        return []


def save_seen(seen: list) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"seen": seen[-KEEP:]}), encoding="utf-8")


def main() -> int:
    seen = load_seen()  # insertion order = age order
    known = set(seen)
    req = urllib.request.Request(URL, headers={"User-Agent": "pre-collector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as exc:
        print(f"collector error: {exc}", file=sys.stderr)
        return 1

    fresh = []
    for item in data.get("items", []):
        uid = hashlib.sha1(item["full_name"].encode()).hexdigest()[:12]
        if uid in known:
            continue
        known.add(uid)
        seen.append(uid)
        fresh.append({  # keep only what the model needs
            "repo": item["full_name"],
            "stars": item["stargazers_count"],
            "description": (item.get("description") or "")[:180],
            "url": item["html_url"],
        })
    save_seen(seen)
    if fresh:
        print(json.dumps(fresh, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
