# Hermes Token Diet & MCP Offload Toolkit

Empirical token auditing, SQLite WAL maintenance, and zero-bloat MCP offload patterns for [Hermes Agent](https://github.com/NousResearch/hermes-agent) and autonomous LLM agents.

> **Why this exists:** While the industry debates "MCP vs. Code Mode", we audited a production Hermes Agent database (`state.db`) with **139,951 messages**, **1,660 sessions**, and **4.63 billion input tokens**. This repository packages the diagnostic scripts and architectural patterns that cut our per-turn token overhead and cron execution costs by up to **93%** — using zero LLM tokens to run the audit itself.

---

## Key Empirical Findings (Measured, Not Estimated)

### 1. The 360:1 Input:Output Bleed Ratio (`tool results` > System Prompt)
In production multi-turn sessions, the static system prompt (~11.5k–14.5k tokens) was **not** the primary cost driver.
- **Measured cumulative input:** `4,630,000,000` tokens
- **Measured cumulative output:** `12,800,000` tokens
- **Ratio:** **`360:1`**

**Root cause:** Unbounded `tool results` (raw PDF extractions, 100KB+ MCP search dumps, verbose terminal logs) stored in `messages.content` and re-sent on **every subsequent turn** of an active session.
- **High-impact lever:** Lowering `tool_output.max_bytes` (e.g. `20000` → `8000`) and filtering/reducing large payloads inside `execute_code` or 1-pass Python scripts before returning to chat.
- **Low-impact myth (Refuted):** Archiving unused skills saved only `~30 tokens/skill` in `<available_skills>`, because Hermes only injects `name` + `description` until `skill_view` is called.Similarly, inactive sessions in `state.db` consume **0 tokens** unless resumed.

### 2. Always-On MCPs vs. On-Demand CLI Offload (`-2,000 to -5,000 tok/turn`)
An always-on MCP server with 10–15 tools injects ~1,800–3,000 tokens of JSON schema into **every turn**, even when unused.
- **Pattern:** Keep heavy/infrequent MCP servers (semantic memory, media generators, AST indexers) **disabled** in `config.yaml` and wrap them in lightweight CLI scripts invoked on demand, or rely on Hermes's native deferred `tool_search` / `execute_code`.

### 3. The Pre-Collect Cron Pattern (`-93% tokens per run`)
Instead of waking an LLM agent on a schedule to call `web_search`, scrape pages, filter duplicates, and format output across 10+ tool-call round-trips:
1. **Stage 1 (0 LLM tokens):** A deterministic Python script (`script:` in Hermes cron) fetches APIs/feeds, deduplicates against local state, and emits a compact JSON summary to stdout.
2. **Stage 2 (Single-pass LLM):** The agent wakes up with the pre-collected JSON already injected into context and performs only high-value reasoning/copywriting.

### 4. The SQLite WAL Reader-Snapshot Trap (`1.15 GB WAL on a 658 MB DB`)
- `ls -la state.db` lies about disk usage: always sum `state.db` + `state.db-wal` + `state.db-shm`.
- By default, SQLite uses `PRAGMA journal_size_limit = -1` (unlimited), and a long-running gateway process holds a read snapshot that prevents `PRAGMA wal_checkpoint(TRUNCATE)` from recycling pages in-process (`busy=0`, yet `ckpt < log`).
- Running `scripts/wal_checkpoint_external.py` during a brief gateway stop reclaimed **1.15 GB** (`1,812 MB` → `657 MB`) in seconds.

### 5. Measuring Real Skill Usage (Avoiding the `<available_skills>` False Positive)
Running `SELECT content FROM messages WHERE content LIKE '%skill_name%'` matches the `<available_skills>` catalog injected into system prompts, producing a **100% false-positive rate** where every installed skill appears "used".
- `scripts/audit_prompt_fat.py` inspects the structured `messages.tool_calls` JSON column to count **true** `skill_view` invocations.

---

## Included Tools

| Script | Purpose | LLM Tokens Used |
|---|---|---|
| `scripts/audit_prompt_fat.py` | Audits real rendered system prompt from `state.db`, detects phantom config fat, measures true `skill_view` usage via `tool_calls`, and computes the input:output bleed ratio. | **0** (Read-only SQLite URI) |
| `scripts/audit_db_storage.py` | Audits physical disk footprint (`db + wal + shm`), WAL pragmas, `freelist` VACUUM yield, FTS5 index weight, and float-epoch session age distribution. | **0** |
| `scripts/wal_checkpoint_external.py` | Safely drains and truncates multi-GB `.db-wal` files outside the gateway process and verifies `quick_check`, `foreign_key_check`, and FTS parity. | **0** |
| `patterns/pre_collect_cron_example.py` | Reference implementation of the **Pre-Collect Cron Pattern** + **CLI Offload Wrapper**. | **0** |

---

## Quick Start

```bash
# 1. Audit your rendered system prompt, skill usage, and token bleed ratio (read-only, ~5s)
python scripts/audit_prompt_fat.py

# 2. Audit physical SQLite + WAL storage and FTS5 index health
python scripts/audit_db_storage.py

# 3. Truncate a bloated state.db-wal (run from a shell outside the gateway)
hermes gateway stop
python scripts/wal_checkpoint_external.py
hermes gateway start
```

Custom Hermes home or profile directory:
```bash
HERMES_HOME=~/.hermes/profiles/my-profile python scripts/audit_prompt_fat.py
```

---

## License
MIT
