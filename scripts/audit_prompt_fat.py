#!/usr/bin/env python3
"""
Measured Token Fat & Bleed Auditor for Hermes Agent — 0 LLM tokens, read-only (~5s).

Answers with empirical data from `state.db`:
  1. Real rendered system prompt size broken down by section (from `system_prompts`).
  2. Phantom config fat probes (entries that appear 0x in the rendered prompt).
  3. TRUE skill usage from structured `messages.tool_calls` (avoids the `<available_skills>` regex trap).
  4. `state.db` payload weight + cumulative input:output token bleed ratio.
  5. Largest retained tool results in `messages.content` (permanent per-turn overhead in active sessions).

Usage:
    python scripts/audit_prompt_fat.py
    HERMES_HOME=~/.hermes/profiles/custom python scripts/audit_prompt_fat.py
"""
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = H / "state.db"


def ro_conn(path: Path) -> sqlite3.Connection:
    """Open in read-only URI mode so we never contend with a live gateway."""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def audit_system_prompt(cur) -> str | None:
    section("1. REAL RENDERED SYSTEM PROMPT (source: system_prompts table)")
    cur.execute("SELECT COUNT(*) FROM system_prompts")
    print(f"Cached prompt snapshots: {cur.fetchone()[0]}")

    cur.execute("SELECT prompt FROM system_prompts ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    if not row or not row[0]:
        print("!! No rendered system prompt found in system_prompts.")
        return None
    sp = row[0]
    print(f"Latest rendered prompt : {len(sp):,} chars (~{len(sp) // 4:,} tok/turn)")

    cur.execute("SELECT MAX(LENGTH(prompt)) FROM system_prompts")
    print(f"Largest ever recorded  : {cur.fetchone()[0]:,} chars")

    marks = [
        ("<available_skills>", "</available_skills>"),
        ("MEMORY (your personal notes)", "USER PROFILE"),
        ("USER PROFILE", "Conversation started"),
        ("# SOUL.md", "## Skills"),
    ]
    print("\n-- Section breakdown --")
    for start_tag, end_tag in marks:
        i = sp.find(start_tag)
        j = sp.find(end_tag, i + 1) if i >= 0 else -1
        if i >= 0 and j > i:
            n = j - i
            print(f"  {start_tag[:34]:36s} {n:7,} chars (~{n // 4:6,} tok)")
        else:
            print(f"  {start_tag[:34]:36s} not present")
    return sp


def audit_phantom_fat(sp: str) -> None:
    section("2. PHANTOM FAT PROBES (0x = dead config, removing saves 0 tokens)")
    probes = [
        "Neko-chan", "pirate", "uwu", "shakespeare", "noir",
        "Deferred tool catalog", "tool_search",
        "BOOTSTRAP.md", "IDENTITY.md", "HEARTBEAT.md",
    ]
    for p in probes:
        n = sp.count(p)
        flag = "  <-- active in prompt" if n else ""
        print(f"  {p:26s} -> {n}x{flag}")


def audit_true_skill_usage(cur) -> None:
    section("3. TRUE SKILL USAGE (structured messages.tool_calls inspection)")
    skills = {}
    skills_root = H / "skills"
    if skills_root.exists():
        for p in skills_root.rglob("SKILL.md"):
            txt = p.read_text(encoding="utf-8", errors="ignore")[:1200]
            m = re.search(r"^name:\s*(.+)$", txt, re.M)
            name = m.group(1).strip().strip('"\'') if m else p.parent.name
            d = re.search(r"^description:\s*(.+)$", txt, re.M)
            desc = d.group(1).strip() if d else ""
            skills[name] = {
                "dir": str(p.parent.relative_to(skills_root)),
                "idx_chars": len(name) + min(len(desc), 60) + 8,
            }

    total_idx = sum(s["idx_chars"] for s in skills.values())
    print(f"Installed skills on disk: {len(skills)}")
    print(f"Estimated <available_skills> index cost: ~{total_idx:,} chars (~{total_idx // 4:,} tok/turn)")

    # Inspect structured tool_calls instead of messages.content (avoids matching the index itself)
    used = Counter()
    cur.execute("SELECT tool_calls FROM messages WHERE tool_calls LIKE '%skill%'")
    for (tc,) in cur.fetchall():
        try:
            arr = json.loads(tc) if isinstance(tc, str) else tc
        except Exception:
            continue
        for item in (arr if isinstance(arr, list) else [arr]) or []:
            fn = (item or {}).get("function", item) or {}
            if "skill_view" not in str(fn.get("name", "")).lower():
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if isinstance(args, dict) and args.get("name"):
                used[str(args["name"]).strip()] += 1

    installed_used = {k: v for k, v in used.items() if k in skills}
    never = sorted(set(skills) - set(installed_used))
    print(f"Loaded via skill_view at least once: {len(installed_used)} | Never loaded: {len(never)}")
    if installed_used:
        print("\n-- Top 10 most loaded skills --")
        for k, cnt in Counter(installed_used).most_common(10):
            print(f"  {cnt:5d}x  {k}")


def audit_db_bleed(cur) -> None:
    section("4. STATE.DB PAYLOAD & INPUT:OUTPUT BLEED RATIO")
    print(f"state.db file size: {DB.stat().st_size / 1024 / 1024:.1f} MB")
    for t in ("messages", "sessions", "system_prompts"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t:16s}: {cur.fetchone()[0]:,}")

    cur.execute("SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions")
    tin, tout = cur.fetchone()
    tin, tout = tin or 0, tout or 1
    ratio = tin // tout
    print(f"\nCumulative input_tokens : {tin:,}")
    print(f"Cumulative output_tokens: {tout:,}")
    print(f"INPUT:OUTPUT RATIO      : {ratio}:1")
    if ratio > 100:
        print("  >> High ratio (>100:1) confirms history/tool-result re-transmission bleed.")
        print("     Primary lever: lower tool_output.max_bytes / max_lines and filter in execute_code.")

    section("5. LARGEST RETAINED TOOL RESULTS (re-sent on every subsequent turn)")
    cur.execute(
        "SELECT role, LENGTH(content) L, "
        "substr(replace(content, char(10), ' '), 1, 60) "
        "FROM messages ORDER BY L DESC LIMIT 8"
    )
    for role, length, preview in cur.fetchall():
        print(f"  {str(role)[:9]:9s} {length:>9,} chars | {preview}")


def main() -> None:
    if not DB.exists():
        print(f"!! state.db not found at {DB}")
        return
    con = ro_conn(DB)
    try:
        cur = con.cursor()
        sp = audit_system_prompt(cur)
        if sp:
            audit_phantom_fat(sp)
        audit_true_skill_usage(cur)
        audit_db_bleed(cur)
    finally:
        con.close()


if __name__ == "__main__":
    main()
