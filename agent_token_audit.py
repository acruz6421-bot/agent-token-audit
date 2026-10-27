#!/usr/bin/env python3
"""
agent-token-audit -- find where your coding agents actually spend tokens.

Reads the local session logs of Hermes Agent, Claude Code, OpenAI Codex and
Google Antigravity. Read-only, Python stdlib only, uses 0 LLM tokens.

    python agent_token_audit.py                 # all agents found, last 30 days
    python agent_token_audit.py --days 7
    python agent_token_audit.py --agent codex --agent claude
    python agent_token_audit.py --json > report.json

Path overrides: HERMES_HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, ANTIGRAVITY_HOME.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

HOME = Path.home()
NOW = time.time()

# Approximate price weights relative to uncached input (Anthropic-style:
# cache read 0.1x, cache write 1.25x). Only used for the "billable-equivalent"
# line, which is labelled as an approximation.
W_CACHE_READ = 0.10
W_CACHE_WRITE = 1.25


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def env_path(var: str, default: Path) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else default


def parse_ts(value) -> float | None:
    """Epoch float from an epoch number or an ISO-8601 string (any precision)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim >6 fractional digits
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def human(n: float) -> str:
    n = float(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def short(p: Path | str) -> str:
    """Hide the home directory in printed paths."""
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s.startswith(h) else s


def iter_jsonl(path: Path):
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def text_len(content) -> tuple[int, int]:
    """(text_chars, image_chars) of a tool-result payload of any shape."""
    if content is None:
        return 0, 0
    if isinstance(content, str):
        return len(content), 0
    if isinstance(content, list):
        t = i = 0
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("image", "input_image"):
                i += len(json.dumps(item))
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                t += len(item["text"])
            else:
                a, b = text_len(item)
                t, i = t + a, i + b
        return t, i
    return len(json.dumps(content)), 0


def load_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def load_toml(path: Path):
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# report model
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    agent: str
    found: bool = False
    source: str = ""
    tokens_available: bool = True
    sessions: int = 0
    api_calls: int = 0
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    tool_results: int = 0
    tool_text_chars: int = 0
    tool_image_chars: int = 0
    largest_tool_results: list = field(default_factory=list)  # [chars, tool]
    top_sessions: list = field(default_factory=list)
    compactions: int | None = None
    mcp: list = field(default_factory=list)  # {name, enabled, schema_chars}
    settings: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    tips: list = field(default_factory=list)

    # derived ------------------------------------------------------------- #
    @property
    def input_total(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write

    @property
    def cache_hit(self) -> float | None:
        return self.cache_read / self.input_total if self.input_total else None

    @property
    def reread_ratio(self) -> float | None:
        return self.input_total / self.output if self.output else None

    @property
    def billable_equiv(self) -> float:
        return (self.input_uncached + W_CACHE_READ * self.cache_read
                + W_CACHE_WRITE * self.cache_write)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(input_total=self.input_total, cache_hit=self.cache_hit,
                 reread_ratio=self.reread_ratio,
                 billable_equiv_input=round(self.billable_equiv))
        return d


class TopN:
    def __init__(self, n: int = 5):
        self.n, self.items = n, []

    def add(self, size: int, label: str) -> None:
        self.items.append([size, label])
        if len(self.items) > self.n * 4:
            self.items = sorted(self.items, reverse=True)[: self.n]

    def result(self) -> list:
        return sorted(self.items, reverse=True)[: self.n]


def top_sessions(per: dict, n: int = 5) -> list:
    rows = sorted(per.items(), key=lambda kv: kv[1]["input_total"], reverse=True)[:n]
    out = []
    for sid, v in rows:
        age = (NOW - v["last"]) / 86400 if v.get("last") else None
        out.append({"id": str(sid)[:12], "input_total": v["input_total"],
                    "output": v["output"],
                    "last_active_days": None if age is None else round(age, 1)})
    return out


# --------------------------------------------------------------------------- #
# Hermes Agent  (~/.hermes/state.db)
# --------------------------------------------------------------------------- #
def audit_hermes(home: Path, since: float) -> Report:
    r = Report("Hermes Agent")
    db = home / "state.db"
    if not db.exists():
        return r
    r.found, r.source = True, short(db)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}

        def col(name: str) -> str:
            return f"COALESCE({name},0)" if name in cols else "0"

        act_cols = [c for c in ("last_activity_at", "ended_at", "started_at") if c in cols]
        act = f"COALESCE({', '.join(act_cols)})" if len(act_cols) > 1 else act_cols[0]
        rows = con.execute(
            f"SELECT id, {col('input_tokens')}, {col('cache_read_tokens')}, "
            f"{col('cache_write_tokens')}, {col('output_tokens')}, "
            f"{col('api_call_count')}, {act} FROM sessions WHERE {act} >= ?",
            (since,)).fetchall()
        per = {}
        for sid, i, cr, cw, o, calls, last in rows:
            r.sessions += 1
            r.api_calls += calls
            r.input_uncached += i
            r.cache_read += cr
            r.cache_write += cw
            r.output += o
            per[sid] = {"input_total": i + cr + cw, "output": o, "last": last}
        r.top_sessions = top_sessions(per)
        r.notes.append("Hermes stores per-session CUMULATIVE totals: sessions active in "
                       "the window are counted with their whole lifetime usage.")

        top = TopN()
        mcols = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        tool_col = "tool_name" if "tool_name" in mcols else "NULL"
        for length, tool in con.execute(
                f"SELECT LENGTH(content), {tool_col} FROM messages "
                "WHERE role='tool' AND timestamp >= ?", (since,)):
            length = length or 0
            r.tool_results += 1
            r.tool_text_chars += length
            top.add(length, tool or "?")
        r.largest_tool_results = top.result()

        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "system_prompts" in tables:
            sp = con.execute("SELECT prompt FROM system_prompts "
                             "ORDER BY rowid DESC LIMIT 1").fetchone()
            if sp and sp[0]:
                r.settings["system_prompt_chars"] = len(sp[0])
                m = re.search(r"<available_skills>.*?</available_skills>", sp[0], re.S)
                if m:
                    r.settings["skills_index_chars"] = len(m.group(0))
    finally:
        con.close()

    cfg = load_yaml(home / "config.yaml")
    if cfg is None:
        r.notes.append("config.yaml not parsed (pip install pyyaml for config checks).")
        return r
    to = cfg.get("tool_output") or {}
    r.settings["tool_output.max_bytes"] = to.get("max_bytes", "default 50000")
    r.settings["compression.threshold"] = (cfg.get("compression") or {}).get("threshold", "default")
    ts = ((cfg.get("tools") or {}).get("tool_search"))
    r.settings["tools.tool_search"] = ts if ts is not None else "default (auto)"
    for name, spec in (cfg.get("mcp_servers") or {}).items():
        spec = spec or {}
        enabled = str(spec.get("enabled", True)).lower() not in ("false", "0", "no", "off")
        r.mcp.append({"name": name, "enabled": enabled, "schema_chars": None})
    return r


# --------------------------------------------------------------------------- #
# Claude Code  (~/.claude/projects/**/*.jsonl)
# --------------------------------------------------------------------------- #
CLAUDE_ENV_KNOBS = {
    "MAX_MCP_OUTPUT_TOKENS": "25000",
    "BASH_MAX_OUTPUT_LENGTH": "30000",
    "ENABLE_TOOL_SEARCH": "on (MCP tools deferred)",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "model default",
}


def audit_claude(base: Path, since: float, claude_json: Path | None = None) -> Report:
    r = Report("Claude Code")
    proj = base / "projects"
    if not proj.exists():
        return r
    r.found, r.source = True, short(proj)
    seen: set = set()
    tool_names: dict = {}
    per: dict = {}
    sessions: set = set()
    top = TopN()
    r.compactions = 0

    for f in proj.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        for o in iter_jsonl(f):
            ts = parse_ts(o.get("timestamp"))
            if ts is None or ts < since:
                continue
            kind = o.get("type")
            msg = o.get("message") or {}
            if kind == "assistant":
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names[block.get("id")] = block.get("name", "?")
                usage = msg.get("usage")
                key = msg.get("id") or o.get("requestId") or o.get("uuid")
                if not usage or key in seen:
                    continue  # one API response is split across several lines
                seen.add(key)
                sid = o.get("sessionId") or f.stem
                sessions.add(sid)
                i = int(usage.get("input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                r.api_calls += 1
                r.input_uncached += i
                r.cache_read += cr
                r.cache_write += cw
                r.output += out
                s = per.setdefault(sid, {"input_total": 0, "output": 0, "last": 0})
                s["input_total"] += i + cr + cw
                s["output"] += out
                s["last"] = max(s["last"], ts)
            elif kind == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        t, img = text_len(block.get("content"))
                        r.tool_results += 1
                        r.tool_text_chars += t
                        r.tool_image_chars += img
                        top.add(t + img, tool_names.get(block.get("tool_use_id"), "?"))
            elif kind == "system" and o.get("subtype") == "compact_boundary":
                r.compactions += 1

    r.sessions = len(sessions)
    r.top_sessions = top_sessions(per)
    r.largest_tool_results = top.result()

    # settings: env vars from the shell or from settings.json "env"
    env_cfg = {}
    try:
        env_cfg = (json.loads((base / "settings.json").read_text(encoding="utf-8"))
                   .get("env") or {})
    except Exception:
        pass
    for k, default in CLAUDE_ENV_KNOBS.items():
        r.settings[k] = os.environ.get(k) or env_cfg.get(k) or f"unset ({default})"

    cj = claude_json or (base / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR")
                         else HOME / ".claude.json")
    try:
        data = json.loads(cj.read_text(encoding="utf-8"))
        names = set((data.get("mcpServers") or {}).keys())
        for p in (data.get("projects") or {}).values():
            names |= set((p.get("mcpServers") or {}).keys())
        r.mcp = [{"name": n, "enabled": True, "schema_chars": None} for n in sorted(names)]
    except Exception:
        pass
    return r


# --------------------------------------------------------------------------- #
# OpenAI Codex  (~/.codex/sessions/**/rollout-*.jsonl)
# --------------------------------------------------------------------------- #
def audit_codex(base: Path, since: float) -> Report:
    r = Report("Codex")
    dirs = [d for d in (base / "sessions", base / "archived_sessions") if d.exists()]
    if not dirs:
        return r
    r.found, r.source = True, short(base / "sessions")
    per: dict = {}
    top = TopN()
    r.compactions = 0

    for d in dirs:
        for f in d.rglob("*.jsonl"):
            try:
                if f.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            prev_total = None
            names: dict = {}
            sid = f.stem
            for o in iter_jsonl(f):
                kind = o.get("type")
                p = o.get("payload") or {}
                ts = parse_ts(o.get("timestamp"))
                if kind == "session_meta":
                    sid = p.get("id") or sid
                if kind == "compacted" or p.get("type") in ("compacted", "context_compacted"):
                    if ts and ts >= since:
                        r.compactions += 1
                if ts is None or ts < since:
                    continue
                if kind == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    last, total = info.get("last_token_usage"), info.get("total_token_usage")
                    if not last or total == prev_total:
                        continue  # repeated event for the same response
                    prev_total = total
                    inp = int(last.get("input_tokens") or 0)  # includes cached
                    cr = int(last.get("cached_input_tokens") or 0)
                    cw = int(last.get("cache_write_input_tokens") or 0)
                    out = int(last.get("output_tokens") or 0)
                    r.api_calls += 1
                    r.input_uncached += max(0, inp - cr - cw)
                    r.cache_read += cr
                    r.cache_write += cw
                    r.output += out
                    s = per.setdefault(sid, {"input_total": 0, "output": 0, "last": 0})
                    s["input_total"] += inp
                    s["output"] += out
                    s["last"] = max(s["last"], ts)
                elif kind == "response_item":
                    pt = p.get("type")
                    if pt in ("function_call", "custom_tool_call", "local_shell_call"):
                        names[p.get("call_id")] = p.get("name") or pt
                    elif pt in ("function_call_output", "custom_tool_call_output"):
                        out_payload = p.get("output")
                        if isinstance(out_payload, dict):
                            out_payload = out_payload.get("content", out_payload)
                        t, img = text_len(out_payload)
                        r.tool_results += 1
                        r.tool_text_chars += t
                        r.tool_image_chars += img
                        top.add(t + img, names.get(p.get("call_id"), "?"))

    r.sessions = len(per)
    r.top_sessions = top_sessions(per)
    r.largest_tool_results = top.result()

    cfg = load_toml(base / "config.toml")
    if cfg is None:
        r.notes.append("config.toml not parsed (needs Python 3.11+ tomllib).")
    else:
        r.settings["tool_output_token_limit"] = cfg.get("tool_output_token_limit", "unset (default)")
        r.settings["model_auto_compact_token_limit"] = cfg.get(
            "model_auto_compact_token_limit", "unset (model default)")
        for name, spec in (cfg.get("mcp_servers") or {}).items():
            spec = spec or {}
            r.mcp.append({"name": name, "enabled": bool(spec.get("enabled", True)),
                          "schema_chars": None})
    return r


# --------------------------------------------------------------------------- #
# Google Antigravity  (~/.gemini/antigravity)
# --------------------------------------------------------------------------- #
def audit_antigravity(base: Path, since: float) -> Report:
    r = Report("Antigravity", tokens_available=False)
    if not base.exists():
        return r
    r.found, r.source = True, short(base)
    r.notes.append("Antigravity does not write token usage to disk (conversations are "
                   "binary .pb). Token totals are not available; MCP schema cost is.")

    db = base / "conversation_summaries.db"
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            for _cid, steps, mod in con.execute(
                    "SELECT conversation_id, step_count, last_modified_time "
                    "FROM conversation_summaries"):
                ts = parse_ts(mod)
                if ts is not None and ts >= since:
                    r.sessions += 1
                    r.api_calls += int(steps or 0)
        except sqlite3.Error:
            pass
        finally:
            con.close()
        r.notes.append("'API calls' for Antigravity = agent steps in the window.")

    conv = base / "conversations"
    if conv.exists():
        size = sum(f.stat().st_size for f in conv.glob("*.pb"))
        r.settings["conversations_on_disk"] = f"{size / 1e6:.1f} MB"

    cfg_path = next((p for p in (base / "mcp_config.json",
                                 base.parent / "config" / "mcp_config.json") if p.exists()), None)
    servers = {}
    if cfg_path:
        try:
            servers = json.loads(cfg_path.read_text(encoding="utf-8")).get("mcpServers") or {}
            r.settings["mcp_config"] = short(cfg_path)
        except Exception:
            pass
    cache = base / "mcp"
    names = set(servers) | ({d.name for d in cache.iterdir() if d.is_dir()} if cache.exists() else set())
    for name in sorted(names):
        spec = servers.get(name) or {}
        enabled = name in servers and not spec.get("disabled", False)
        off = set(spec.get("disabledTools") or [])
        chars = 0
        d = cache / name
        if d.exists():
            for tool in d.glob("*.json"):
                if tool.stem not in off:
                    chars += tool.stat().st_size
        r.mcp.append({"name": name, "enabled": enabled, "schema_chars": chars or None})
    return r


# --------------------------------------------------------------------------- #
# recommendations
# --------------------------------------------------------------------------- #
LIMIT_KNOB = {
    "Hermes Agent": "tool_output.max_bytes in config.yaml (default 50000)",
    "Claude Code": "BASH_MAX_OUTPUT_LENGTH (Bash), MAX_MCP_OUTPUT_TOKENS (MCP) or "
                   "offset/limit on Read",
    "Codex": "tool_output_token_limit in config.toml",
}
COMPACT_KNOB = {
    "Hermes Agent": "compression.threshold in config.yaml",
    "Claude Code": "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (or /compact, /clear)",
    "Codex": "model_auto_compact_token_limit in config.toml (or /compact)",
}


def add_tips(r: Report) -> None:
    if not r.found:
        return
    if r.tokens_available and r.input_total > 1_000_000:
        hit = r.cache_hit or 0
        if hit < 0.5:
            r.tips.append(
                f"Cache hit is only {hit:.0%}. Prompt caches are per model and per "
                "account: avoid switching model mid-session, keep proxies on a sticky "
                "account, and do not edit early context.")
        if (r.reread_ratio or 0) > 100:
            r.tips.append(
                f"Each output token costs {r.reread_ratio:.0f} input tokens: sessions run "
                f"long. Start fresh sessions per task or compact earlier "
                f"({COMPACT_KNOB.get(r.agent, 'compaction setting')}).")
    if r.largest_tool_results and r.largest_tool_results[0][0] > 50_000:
        size, tool = r.largest_tool_results[0]
        r.tips.append(
            f"Largest tool result: {human(size)} chars from '{tool}'. It is re-sent "
            f"until the session ends or compacts. Cap it with "
            f"{LIMIT_KNOB.get(r.agent, 'the tool output limit')} or filter before returning.")
    if r.tool_image_chars > 1_000_000:
        r.tips.append(
            f"Tool results carried {human(r.tool_image_chars)} chars of base64 images "
            "(screenshots). Images are billed by resolution, not by base64 length, but each "
            "one stays in context for the rest of the session: downscale or crop them.")
    if r.agent == "Claude Code" and str(r.settings.get("ENABLE_TOOL_SEARCH")).lower() == "false":
        r.tips.append("ENABLE_TOOL_SEARCH=false loads every MCP schema each turn; unset it.")
    if r.agent == "Hermes Agent":
        mb = r.settings.get("tool_output.max_bytes")
        if isinstance(mb, int) and mb > 20_000:
            r.tips.append(f"tool_output.max_bytes={mb}. 8000-20000 is usually enough.")
        elif mb == "default 50000":
            r.tips.append("tool_output.max_bytes is at the 50000 default; 8000-20000 is usually enough.")
    heavy = [m for m in r.mcp if m["enabled"] and (m["schema_chars"] or 0) > 8_000]
    for m in heavy:
        r.tips.append(
            f"MCP '{m['name']}' is enabled with ~{human(m['schema_chars'] / 4)} tokens of tool "
            "schemas. If you rarely use it, disable it or trim with disabledTools.")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def print_report(r: Report, days: int) -> None:
    print(f"\n== {r.agent}  [{r.source}]  last {days}d")
    if r.tokens_available:
        print(f"  sessions {r.sessions:,} | API calls {r.api_calls:,}")
        hit = f" ({r.cache_hit:.0%} of input)" if r.cache_hit is not None else ""
        print(f"  input  uncached {human(r.input_uncached)} | cache read "
              f"{human(r.cache_read)}{hit} | cache write {human(r.cache_write)}")
        ratio = f"{r.reread_ratio:.0f}:1" if r.reread_ratio else "n/a"
        print(f"  output {human(r.output)} | input:output {ratio} | "
              f"billable-equivalent input ~{human(r.billable_equiv)} (approx.)")
    else:
        print(f"  conversations {r.sessions:,} | agent steps {r.api_calls:,} | tokens: not logged")
    if r.tool_results:
        img = f" + {human(r.tool_image_chars)} image" if r.tool_image_chars else ""
        print(f"  tool results {r.tool_results:,}: {human(r.tool_text_chars)} chars text{img}")
        print("  largest: " + ", ".join(f"{human(s)} {t}" for s, t in r.largest_tool_results))
    if r.compactions is not None:
        print(f"  compactions {r.compactions}")
    if r.top_sessions:
        print("  top sessions by input (incl. cache):")
        for s in r.top_sessions:
            age = "?" if s["last_active_days"] is None else f"{s['last_active_days']}d ago"
            print(f"    {s['id']:<12} in {human(s['input_total']):>7}  out "
                  f"{human(s['output']):>6}  last active {age}")
    if r.mcp:
        on = [m for m in r.mcp if m["enabled"]]
        print(f"  MCP servers: {len(on)} enabled / {len(r.mcp)} configured")
        for m in r.mcp:
            sc = f" ~{human(m['schema_chars'] / 4)} tok schema" if m["schema_chars"] else ""
            print(f"    [{'on ' if m['enabled'] else 'off'}] {m['name']}{sc}")
    for k, v in r.settings.items():
        print(f"  {k} = {v}")
    for n in r.notes:
        print(f"  note: {n}")
    for t in r.tips:
        print(f"  -> {t}")


AGENTS = {
    "hermes": lambda since: audit_hermes(env_path("HERMES_HOME", HOME / ".hermes"), since),
    "claude": lambda since: audit_claude(env_path("CLAUDE_CONFIG_DIR", HOME / ".claude"), since),
    "codex": lambda since: audit_codex(env_path("CODEX_HOME", HOME / ".codex"), since),
    "antigravity": lambda since: audit_antigravity(
        env_path("ANTIGRAVITY_HOME", HOME / ".gemini" / "antigravity"), since),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    ap.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    ap.add_argument("--agent", action="append", choices=sorted(AGENTS),
                    help="limit to one agent (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    since = NOW - args.days * 86400
    reports = []
    for key in args.agent or list(AGENTS):
        try:
            rep = AGENTS[key](since)
        except Exception as exc:  # never let one agent break the others
            rep = Report(key, found=True, notes=[f"audit failed: {exc!r}"])
        add_tips(rep)
        reports.append(rep)

    if args.json:
        print(json.dumps([r.to_dict() for r in reports if r.found], indent=2, default=str))
        return 0
    found = [r for r in reports if r.found]
    if not found:
        print("No supported agent data found (Hermes, Claude Code, Codex, Antigravity).")
        return 1
    for r in found:
        print_report(r, args.days)
    missing = [r.agent for r in reports if not r.found]
    if missing:
        print(f"\n(not found: {', '.join(missing)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
