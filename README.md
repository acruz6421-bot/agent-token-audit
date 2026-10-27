# agent-token-audit

Find out where your coding agents actually spend tokens, from the logs they already write to disk.

One read-only Python script (stdlib only, 0 LLM tokens, runs in about a second) that audits:

| Agent | Reads | Token totals | Cache split | Tool-result sizes | MCP config |
|---|---|---|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | `~/.hermes/state.db` | ✅ | ✅ | ✅ | ✅ |
| Claude Code | `~/.claude/projects/**/*.jsonl` | ✅ | ✅ | ✅ | ✅ (names) |
| OpenAI Codex | `~/.codex/sessions/**/rollout-*.jsonl` | ✅ | ✅ | ✅ (text + images) | ✅ |
| Google Antigravity | `~/.gemini/antigravity/` | ❌ not logged | ❌ | ❌ | ✅ (with schema size) |

```bash
python agent_token_audit.py              # every agent found, last 30 days
python agent_token_audit.py --days 7 --agent claude
python agent_token_audit.py --json > report.json
```

Path overrides: `HERMES_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `ANTIGRAVITY_HOME`.
Optional: `pyyaml` for the Hermes config checks; Python 3.11+ for the Codex `config.toml` checks.

---

## What it reports

For each agent:

- **Input split three ways**: uncached, cache read, cache write. Most "my agent burned 5 billion tokens" numbers are mostly cache reads, which cost about a tenth of normal input on Anthropic and OpenAI. The cache-hit % is the first number to look at.
- **Input:output ratio.** Agents re-send the whole conversation on every call, so 100:1 to 300:1 is normal for long sessions. When it climbs, the fix is shorter sessions or earlier compaction, not a smaller system prompt.
- **Largest tool results**, with the tool that produced them. One 600k-char file read stays in context, and is re-sent, until the session ends or compacts.
- **Image payloads in tool results** (screenshots), separately from text.
- **Top sessions by input**, with days since last activity. An idle session costs nothing. Its lifetime total is history, not an ongoing bleed.
- **MCP servers** enabled vs configured. For Antigravity, which caches tool schemas on disk, the approximate schema size of each server too.
- **The relevant knobs and their current values**, plus concrete tips when a threshold is crossed.

### Where each agent's knobs are

| Lever | Hermes Agent | Claude Code | Codex |
|---|---|---|---|
| Cap tool output | `tool_output.max_bytes` (default 50000) | `BASH_MAX_OUTPUT_LENGTH` (30000), `MAX_MCP_OUTPUT_TOKENS` (25000) | `tool_output_token_limit` |
| Compact earlier | `compression.threshold` | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `/compact` | `model_auto_compact_token_limit`, `/compact` |
| Defer MCP schemas | `tools.tool_search` (auto) | `ENABLE_TOOL_SEARCH` (on by default) | `enabled_tools` / `disabled_tools` per server |
| Turn an MCP server off | `mcp_servers.<name>.enabled: false` | remove from `~/.claude.json` | `mcp_servers.<id>.enabled = false` |

Antigravity: `disabled: true` or `disabledTools` per server in `mcp_config.json`.

---

## Example: one heavy user, one machine

Real output, October 2026. These numbers are not a benchmark.

| | Hermes (30 d) | Claude Code (all history) | Codex (30 d) |
|---|---|---|---|
| API calls | 14,490 | 10,898 | 34 |
| Total input | 2.15 B | 3.39 B | 2.45 M |
| Cache read share | 69 % | 97 % | 93 % |
| Input:output | 225:1 | 310:1 | 92:1 |
| Largest tool result | 98 k chars (`web_extract`) | 613 k chars (`Read`) | 2.6 M chars (screenshot via `exec`) |

What it showed:

1. **Most of the scary totals were cache reads.** Claude Code's 3.39 B input works out to about 440 M at uncached-input prices.
2. **Lifetime Hermes cache hit was 40 %, the last 30 days 69 %.** The difference came from moving a Gemini proxy from round-robin to sticky accounts. Provider caches are per account and per model.
3. **Codex sessions were dominated by screenshots**: 14 M chars of base64 images against 77 k chars of text output.
4. **Single tool results of 100 k to 600 k chars** were the main avoidable cost in Hermes and Claude Code.

---

## Hermes Agent extras (`hermes/`)

| Script | What it does |
|---|---|
| `hermes/audit_prompt.py` | Breaks the rendered system prompt into sections (skills index, memory, user profile, project context). Ranks skills by *times loaded × body size* from `messages.tool_calls`, which avoids the false positive of grepping message text, where the skills index itself matches every skill. |
| `hermes/audit_db_storage.py` | Real disk footprint (`db + wal + shm`), WAL vs the 64 MiB cap, VACUUM yield, FTS index weight. |
| `hermes/wal_checkpoint_external.py` | Truncates an oversized WAL with the gateway stopped, then checks integrity. Only needed on old Hermes versions or with a stuck reader (see below). |

Facts these scripts rely on:

- Hermes puts only `name + description` of each skill in the prompt. A skill body costs tokens only when `skill_view` loads it. Deleting small unused skills saves almost nothing; trimming big skills that load often saves the most.
- `sessions.input_tokens` is cumulative per session. Check `last_activity_at` before calling an old session expensive. Archiving idle sessions saves disk, not tokens.
- Recent Hermes versions set `PRAGMA journal_size_limit` to 64 MiB on every connection, so `state.db-wal` stays at or below 64 MB. Running `PRAGMA journal_size_limit` from your own connection prints `-1`, because the pragma is per connection. That is not a bug.

---

## Pre-collect pattern (`patterns/`)

An agent that searches, scrapes and deduplicates by itself makes one model call per step, and each call re-sends the growing context. A plain script can do the collection for 0 tokens and hand the model a compact JSON for a single reasoning pass. The script prints nothing when there is nothing new, so the model call can be skipped entirely.

```bash
# Hermes cron (script must live in ~/.hermes/scripts/)
hermes cron create "every 6h" "Write a short digest of the new repos above." \
    --script pre_collect_cron_example.py

# Any CLI agent
out=$(python patterns/pre_collect_cron_example.py)
[ -n "$out" ] && echo "$out" | claude -p "Write a short digest of these repos."
[ -n "$out" ] && echo "$out" | codex exec "Write a short digest of these repos."
```

How much this saves depends on how many tool steps the agent was taking. Measure your own cron sessions before and after with `agent_token_audit.py`.

---

## Corrections to v0.1

The first release (October 2026) contained claims that did not hold up:

- **"360:1 input:output bleed".** It used only uncached input and ignored the cache split entirely. With cache reads included the lifetime ratio is about 305:1. 40 % of that input (69 % in the last 30 days) was cache reads, billed at about a tenth of the price, so the cost picture was very different from the raw ratio.
- **"-93% tokens per run" for pre-collect.** No measurement backed this number. It has been removed.
- **"SQLite WAL reader-snapshot trap / journal_size_limit = -1 is the root cause".** Hermes already caps the WAL at 64 MiB. The `--fix-wal` flag had no lasting effect and is gone.
- **"Largest retained tool results".** The query did not filter `role='tool'` and listed user messages.
- **Pre-collect dedup.** It kept 500 ids sorted alphabetically, so it forgot random items instead of the oldest. It now keeps insertion order.
- **Cron example.** It used a `--schedule` flag that does not exist; `schedule` is positional.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests use synthetic fixtures for each log format, including split Claude Code responses, duplicate Codex token events and old Hermes schemas. CI runs them on Linux, macOS and Windows.

## License

MIT
