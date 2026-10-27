#!/usr/bin/env python3
"""
Hermes Agent deep audit: system prompt, skill usage, retained tool results.
Read-only, stdlib only, 0 LLM tokens.

    python hermes/audit_prompt.py
    HERMES_HOME=~/.hermes/profiles/work python hermes/audit_prompt.py

For token totals across Hermes, Claude Code, Codex and Antigravity use
../agent_token_audit.py instead.
"""
import json
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

H = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
DB = H / "state.db"

# Section markers Hermes writes into the rendered system prompt.
SECTIONS = [
    ("skills index", r"<available_skills>.*?</available_skills>"),
    ("memory", r"MEMORY \(your personal notes\).*?(?=USER PROFILE|\Z)"),
    ("user profile", r"USER PROFILE \(who the user is\).*?(?=\n# |\Z)"),
    ("project context", r"# Project Context.*?(?=\n## Skills|\Z)"),
]


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def audit_system_prompt(cur) -> None:
    section("1. RENDERED SYSTEM PROMPT (latest row of system_prompts)")
    row = cur.execute("SELECT prompt FROM system_prompts ORDER BY rowid DESC LIMIT 1").fetchone()
    if not row or not row[0]:
        print("No rendered system prompt stored.")
        return
    sp = row[0]
    print(f"Total: {len(sp):,} chars (~{len(sp) // 4:,} tokens, chars/4 estimate)")
    for name, pattern in SECTIONS:
        m = re.search(pattern, sp, re.S)
        n = len(m.group(0)) if m else 0
        share = f"{n / len(sp):5.1%}" if n else "  -  "
        print(f"  {name:16s} {n:8,} chars  {share}")
    print("A static prompt is cached by most providers after the first call; it costs "
          "full price only on cache misses (model switch, edited memory, new session).")


def audit_skill_usage(cur) -> None:
    section("2. SKILL USAGE (skill_view calls in messages.tool_calls)")
    installed = {}
    root = H / "skills"
    if root.exists():
        for p in root.rglob("SKILL.md"):
            head = p.read_text(encoding="utf-8", errors="ignore")[:1200]
            m = re.search(r"^name:\s*(.+)$", head, re.M)
            name = m.group(1).strip().strip("\"'") if m else p.parent.name
            installed[name] = p.stat().st_size

    used = Counter()
    for (tc,) in cur.execute("SELECT tool_calls FROM messages WHERE tool_calls LIKE '%skill_view%'"):
        try:
            calls = json.loads(tc) if isinstance(tc, str) else tc
        except ValueError:
            continue
        for item in calls if isinstance(calls, list) else [calls]:
            fn = (item or {}).get("function", item) or {}
            if "skill_view" not in str(fn.get("name", "")):
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if isinstance(args, dict) and args.get("name") and not args.get("file_path"):
                used[str(args["name"]).strip()] += 1

    never = sorted(set(installed) - set(used))
    print(f"Installed: {len(installed)} | loaded at least once: "
          f"{len(set(used) & set(installed))} | never loaded: {len(never)}")
    print("Only name + description sit in the prompt; a skill body costs tokens only when "
          "loaded. Big bodies that load often are the ones worth trimming:")
    ranked = sorted(((used[n] * installed[n], used[n], installed[n], n)
                     for n in installed if used[n]), reverse=True)[:10]
    for _, count, size, name in ranked:
        print(f"  {count:4d}x  {size / 1024:6.1f} KB  {name}")


def audit_tool_results(cur) -> None:
    section("3. LARGEST TOOL RESULTS (role='tool')")
    for sid, tool, length, ts in cur.execute(
            "SELECT session_id, tool_name, LENGTH(content) L, timestamp FROM messages "
            "WHERE role='tool' ORDER BY L DESC LIMIT 10"):
        print(f"  {length:>9,} chars  {str(tool or '?'):20s} session {str(sid)[:20]}")
    total, n = cur.execute(
        "SELECT SUM(LENGTH(content)), COUNT(*) FROM messages WHERE role='tool'").fetchone()
    print(f"All tool results: {n:,} rows, {(total or 0) / 1e6:.1f} M chars.")
    print("A tool result is re-sent on every later turn of the SAME session until "
          "compression. Cap it with tool_output.max_bytes (default 50000).")


def audit_sessions(cur) -> None:
    section("4. TOKEN TOTALS (sessions table, lifetime)")
    i, cr, cw, o = cur.execute(
        "SELECT SUM(input_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens), "
        "SUM(output_tokens) FROM sessions").fetchone()
    i, cr, cw, o = i or 0, cr or 0, cw or 0, o or 0
    total_in = i + cr + cw
    print(f"input uncached {i:,} | cache read {cr:,} | cache write {cw:,} | output {o:,}")
    if total_in and o:
        print(f"cache hit {cr / total_in:.0%} | input:output {total_in / o:.0f}:1")
    print("sessions.input_tokens is cumulative per session. An idle session costs 0 "
          "tokens; check last_activity_at before blaming an old session.")


def main() -> int:
    if not DB.exists():
        print(f"state.db not found at {DB}")
        return 1
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        audit_system_prompt(cur)
        audit_skill_usage(cur)
        audit_tool_results(cur)
        audit_sessions(cur)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
