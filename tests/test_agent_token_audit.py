"""Synthetic-fixture tests for agent_token_audit.py (stdlib unittest, no network).

    python -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_token_audit as ata  # noqa: E402

NOW_ISO = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
SINCE = time.time() - 86400


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class ClaudeCodeTest(unittest.TestCase):
    def test_dedups_split_responses_and_maps_tool_names(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            usage = {"input_tokens": 10, "cache_read_input_tokens": 900,
                     "cache_creation_input_tokens": 90, "output_tokens": 5}
            rows = [
                # one API response written as two lines (text + tool_use)
                {"type": "assistant", "timestamp": NOW_ISO, "sessionId": "s1",
                 "message": {"id": "msg_1", "usage": usage,
                             "content": [{"type": "text", "text": "hi"}]}},
                {"type": "assistant", "timestamp": NOW_ISO, "sessionId": "s1",
                 "message": {"id": "msg_1", "usage": usage,
                             "content": [{"type": "tool_use", "id": "tu_1", "name": "Read"}]}},
                {"type": "user", "timestamp": NOW_ISO, "sessionId": "s1",
                 "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_1",
                                          "content": "x" * 1234}]}},
                {"type": "system", "subtype": "compact_boundary", "timestamp": NOW_ISO},
            ]
            write_jsonl(base / "projects" / "p" / "s1.jsonl", rows)
            r = ata.audit_claude(base, SINCE, claude_json=base / "none.json")
            self.assertEqual(r.api_calls, 1)
            self.assertEqual(r.input_total, 1000)
            self.assertEqual(r.cache_read, 900)
            self.assertEqual(r.output, 5)
            self.assertEqual(r.compactions, 1)
            self.assertEqual(r.largest_tool_results[0], [1234, "Read"])
            self.assertAlmostEqual(r.cache_hit, 0.9)


class CodexTest(unittest.TestCase):
    def test_uses_last_usage_and_splits_cache(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            last = {"input_tokens": 1000, "cached_input_tokens": 800,
                    "cache_write_input_tokens": 0, "output_tokens": 20}
            total = dict(last)
            rows = [
                {"type": "session_meta", "timestamp": NOW_ISO, "payload": {"id": "abc"}},
                {"type": "response_item", "timestamp": NOW_ISO,
                 "payload": {"type": "function_call", "call_id": "c1", "name": "shell"}},
                {"type": "response_item", "timestamp": NOW_ISO,
                 "payload": {"type": "function_call_output", "call_id": "c1",
                             "output": [{"type": "input_text", "text": "y" * 500},
                                        {"type": "input_image", "image_url": "data:" + "A" * 3000}]}},
                {"type": "event_msg", "timestamp": NOW_ISO,
                 "payload": {"type": "token_count",
                             "info": {"last_token_usage": last, "total_token_usage": total}}},
                # duplicate event for the same response must not double count
                {"type": "event_msg", "timestamp": NOW_ISO,
                 "payload": {"type": "token_count",
                             "info": {"last_token_usage": last, "total_token_usage": total}}},
            ]
            write_jsonl(base / "sessions" / "2026" / "01" / "01" / "rollout-x.jsonl", rows)
            r = ata.audit_codex(base, SINCE)
            self.assertEqual(r.api_calls, 1)
            self.assertEqual(r.input_uncached, 200)
            self.assertEqual(r.cache_read, 800)
            self.assertEqual(r.tool_text_chars, 500)
            self.assertGreater(r.tool_image_chars, 3000)
            self.assertEqual(r.largest_tool_results[0][1], "shell")


class HermesTest(unittest.TestCase):
    def test_reads_sessions_and_tool_messages(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            con = sqlite3.connect(home / "state.db")
            con.executescript("""
                CREATE TABLE sessions (id TEXT, started_at REAL, last_activity_at REAL,
                    input_tokens INT, cache_read_tokens INT, cache_write_tokens INT,
                    output_tokens INT, api_call_count INT);
                CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT,
                    tool_name TEXT, timestamp REAL);
            """)
            now = time.time()
            con.execute("INSERT INTO sessions VALUES ('new', ?, ?, 100, 900, 0, 10, 3)", (now, now))
            con.execute("INSERT INTO sessions VALUES ('old', 1, 1, 999999, 0, 0, 1, 1)")
            con.execute("INSERT INTO messages VALUES ('new', 'tool', ?, 'web_extract', ?)",
                        ("z" * 777, now))
            con.execute("INSERT INTO messages VALUES ('new', 'user', ?, NULL, ?)", ("u" * 9999, now))
            con.commit()
            con.close()
            r = ata.audit_hermes(home, SINCE)
            self.assertEqual(r.sessions, 1)          # idle session excluded
            self.assertEqual(r.input_total, 1000)
            self.assertEqual(r.tool_results, 1)      # user message is not a tool result
            self.assertEqual(r.largest_tool_results[0], [777, "web_extract"])


class AntigravityTest(unittest.TestCase):
    def test_mcp_schema_cost_respects_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "antigravity"
            for server, tools in (("on", ["a", "b"]), ("off", ["c"])):
                for t in tools:
                    p = base / "mcp" / server / f"{t}.json"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps({"name": t, "description": "d" * 100}))
            (base / "mcp_config.json").write_text(json.dumps({"mcpServers": {
                "on": {"command": "x", "disabledTools": ["b"]},
                "off": {"command": "y", "disabled": True}}}))
            r = ata.audit_antigravity(base, SINCE)
            by = {m["name"]: m for m in r.mcp}
            self.assertTrue(by["on"]["enabled"])
            self.assertFalse(by["off"]["enabled"])
            single = (base / "mcp" / "on" / "a.json").stat().st_size
            self.assertEqual(by["on"]["schema_chars"], single)  # 'b' excluded
            self.assertFalse(r.tokens_available)


class HelpersTest(unittest.TestCase):
    def test_parse_ts_variants(self):
        self.assertIsNotNone(ata.parse_ts("2026-05-16 13:12:13.8192223+00:00"))
        self.assertIsNotNone(ata.parse_ts("2026-10-26T22:00:14.756Z"))
        self.assertEqual(ata.parse_ts(12.5), 12.5)
        self.assertIsNone(ata.parse_ts("garbage"))


if __name__ == "__main__":
    unittest.main()
