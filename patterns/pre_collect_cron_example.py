#!/usr/bin/env python3
"""
Reference Pattern: Pre-Collect Cron + CLI Offload (0 LLM Tokens for Data Gathering)

Instead of waking an LLM agent to run 10+ web_search/mcp tool calls in a loop
(burning 50k-200k tokens per tick), run a deterministic Python pre-collector first.

In Hermes Agent cron:
    hermes cron create --schedule "every 6h" --script pre_collect_cron_example.py \
        --prompt "Write a concise digest from the pre-collected JSON items above."

If combined with `monitor:` or `no_agent: true`, unchanged ticks cost 0 LLM tokens.
"""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

STATE_FILE = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "pre_collect_state.json"


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", []))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"seen": sorted(list(seen))[-500:]}, indent=2), encoding="utf-8")


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-pre-collector/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    seen = load_seen()
    # Example: fetch top trending repositories via GitHub Search API (0 LLM tokens)
    url = "https://api.github.com/search/repositories?q=topic:mcp+stars:>100&sort=updated&per_page=5"
    try:
        data = fetch_json(url)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 0

    fresh_items = []
    for item in data.get("items", []):
        uid = hashlib.sha1(item["full_name"].encode()).hexdigest()[:12]
        if uid in seen:
            continue
        seen.add(uid)
        # Strip 95% of noisy API fields before the LLM ever sees them
        fresh_items.append({
            "repo": item["full_name"],
            "stars": item["stargazers_count"],
            "description": (item.get("description") or "")[:180],
            "url": item["html_url"],
        })

    save_seen(seen)
    # Only compact, high-signal JSON enters the agent's context window
    print(json.dumps({"new_count": len(fresh_items), "items": fresh_items}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
