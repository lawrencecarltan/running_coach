# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Install dependencies
pip install -r requirements.txt

# Authenticate with Strava (one-time, saves tokens to .tokens.json)
python auth.py

# Run the Telegram bot
python bot.py

# Run the CLI interface
python cli.py
```

There are no tests or linting configurations in this project.

## Environment variables

Copy `.env.example` to `.env`. Required variables:

- `ANTHROPIC_API_KEY` — Anthropic console
- `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` — Strava API settings
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_USER_ID` — for bot mode only
- `DATABASE_URL` — PostgreSQL connection string (optional; falls back to in-memory)

On Railway (no `.tokens.json`), set `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`, and `STRAVA_TOKEN_EXPIRES_AT` as env vars instead.

## Architecture

Two entry points share the same core logic:

- **`bot.py`** — Telegram bot; creates a single `CoachSession` at startup, routes Telegram messages to `coach.agent.CoachSession.chat(chat_id, message)`. Supports `/reset`, `/memory`, `/clearmemory` commands.
- **`cli.py`** — local interactive REPL; runs a standalone agentic loop directly (no `CoachSession`; no DB persistence, no memory tool).

### CoachSession (`coach/agent.py`)

The core class used by the bot. It owns:
- One persistent MCP subprocess (`strava_mcp/server.py` over stdio) shared across all chats
- Per-chat in-memory history (`_histories`) and athlete facts (`_memories`), both lazy-loaded from PostgreSQL on first message
- A `save_memory` tool defined inline (not in the MCP server) that persists athlete facts to DB

`chat()` drives an agentic loop: send messages → if `tool_use`, call the MCP tool → append result → repeat until `end_turn`.

### Token management

History trimming happens in two passes before every API call:
1. **`_truncate()`** — caps message count at `_MAX_HISTORY` (10) and total serialised size at `_MAX_HISTORY_CHARS` (40 000), always removing oldest pairs
2. **`_compress_old_tool_results()`** — replaces tool result content in messages older than `_KEEP_FULL_TOOL_RESULTS` (6) with `[data used above]`

Tool results from the MCP server are also truncated to `_MAX_TOOL_RESULT_CHARS` (3000) on receipt.

**Prompt caching** is applied to two layers:
- The static `SYSTEM_PROMPT` block in `coach/prompts.py` — cached with `cache_control: ephemeral`
- The last tool definition in the tools list — cached so the full tool list (which is static per session) is reused across calls

### MCP server (`strava_mcp/server.py`)

Uses FastMCP to expose Strava API endpoints as tools. Runs as a subprocess — **stdout is reserved for the MCP protocol; never add `print()` calls here**. Uses `sys.stderr` for logging.

Response fields are filtered via `_ACTIVITY_KEEP` / `_ACTIVITY_SUMMARY_KEEP` / `_BEST_EFFORT_KEEP` sets to strip coaching-irrelevant data (map polylines, social counts, segment efforts) before returning to Claude.

### Database (`db.py`)

`asyncpg`-based pool. Two tables: `chat_history` (JSONB messages per chat_id) and `user_memory` (JSONB facts list per chat_id). All functions are no-ops if `DATABASE_URL` is not set — the bot falls back to the in-memory dicts on `CoachSession`. Tables are created on `init_pool()` at startup.

### Token/config (`config.py`)

`Config` loads Strava tokens from `.tokens.json` first, then falls back to env vars. `update_tokens()` writes back to `.tokens.json` (used by `auth.py` after OAuth and by the Strava client after token refresh).
