# Build Spec: Claude Cost & Token Dashboard ("clauditor")

> **For the agent building this:** This is your complete build brief. Read it fully before
> writing code. Build in the phase order given at the end. Everything runs **locally** on
> the user's machine — no cloud, no accounts, no external database. The only outbound network
> call the app makes is an optional, user-triggered pricing refresh.

---

## 1. What this is

A local-first, self-hosted dashboard that tracks Anthropic token usage and dollar spend
across (a) Claude Code sessions and (b) the user's own Claude API calls. It breaks spend down
by project, model, and time; fires budget alerts; and surfaces concrete cost-saving
suggestions (prompt caching, model downgrades, batch API).

The user runs one command, a local web server starts, and a dashboard opens at
`http://localhost:4747`.

### Non-goals (do NOT build these)
- No user authentication / multi-tenant accounts.
- No cloud sync, no remote database, no telemetry phoning home.
- No re-tokenizing of text to estimate tokens — **always use the token counts already
  present in the logs/responses.** Tokenizers differ between model generations; the logged
  counts are authoritative.
- No editing or deleting of the user's `~/.claude` files. Read-only access there.

---

## 2. Architecture overview

```
clauditor/
  collectors/         # pull raw usage into the DB
    claude_code.py    # parses ~/.claude/projects/**/*.jsonl
    api_wrapper.py    # library users import to log their own API calls
    admin_api.py      # optional: polls Anthropic Admin Usage/Cost API
  core/
    db.py             # SQLite schema + queries
    pricing.py        # pricing engine (tokens -> $)
    analyzer.py       # alerts + savings suggestions
    config.py         # loads config.json + pricing.json
  server/
    app.py            # FastAPI app, serves API + static dashboard
    routes.py         # JSON endpoints consumed by the frontend
  web/                # frontend (single-page app)
    index.html
    main.js
    styles.css
  data/
    clauditor.db      # SQLite (created on first run)
  config.json         # user budgets + settings
  pricing.json        # model rates (editable, dated)
  cli.py              # entrypoint: `clauditor ingest`, `clauditor serve`, etc.
  pyproject.toml
  README.md
```

**Stack:** Python 3.11+, FastAPI + Uvicorn (server), SQLite (stdlib `sqlite3`), plain
HTML/CSS/JS frontend with a charting lib loaded from CDN (Chart.js). No build step for the
frontend — keep it a static SPA so the user never runs npm.

**Data flow:** collectors → `usage_events` table → pricing engine computes `cost_usd` at
insert time → analyzer reads the table → server exposes aggregates as JSON → frontend renders.

---

## 3. Data model (SQLite)

Create these tables in `core/db.py` via an idempotent `init_db()` (use
`CREATE TABLE IF NOT EXISTS`).

### 3.1 `usage_events` — one row per API call / per assistant message

```sql
CREATE TABLE IF NOT EXISTS usage_events (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uid              TEXT UNIQUE,        -- dedupe key (see 5.1)
  ts                     TEXT NOT NULL,      -- ISO 8601 UTC
  source                 TEXT NOT NULL,      -- 'claude_code' | 'api' | 'admin_api'
  project                TEXT,               -- repo/dir name or API-key label
  model                  TEXT NOT NULL,      -- e.g. 'claude-opus-4-8'
  input_tokens           INTEGER DEFAULT 0,
  output_tokens          INTEGER DEFAULT 0,
  cache_creation_tokens  INTEGER DEFAULT 0,
  cache_read_tokens      INTEGER DEFAULT 0,
  is_batch               INTEGER DEFAULT 0,  -- 0/1
  cache_ttl              TEXT,               -- '5m' | '1h' | NULL
  cost_usd               REAL NOT NULL,      -- computed at insert
  session_id             TEXT,
  raw_meta               TEXT                -- optional JSON blob for debugging
);

CREATE INDEX IF NOT EXISTS idx_events_ts      ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_project ON usage_events(project);
CREATE INDEX IF NOT EXISTS idx_events_model   ON usage_events(model);
```

### 3.2 `ingest_state` — tracks file offsets so re-ingest only reads new data

```sql
CREATE TABLE IF NOT EXISTS ingest_state (
  file_path     TEXT PRIMARY KEY,
  last_offset   INTEGER DEFAULT 0,   -- byte offset already consumed
  last_mtime    REAL,
  last_ingested TEXT
);
```

### 3.3 `alerts_log` — so we don't re-fire the same budget alert repeatedly

```sql
CREATE TABLE IF NOT EXISTS alerts_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  scope       TEXT NOT NULL,   -- 'global' | 'project:<name>'
  period      TEXT NOT NULL,   -- 'daily' | 'weekly' | 'monthly'
  threshold   REAL NOT NULL,   -- the budget value crossed
  actual      REAL NOT NULL,   -- spend at fire time
  period_key  TEXT NOT NULL    -- e.g. '2026-06-20' / '2026-W25' / '2026-06'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_once
  ON alerts_log(scope, period, period_key);
```

---

## 4. Pricing engine

### 4.1 `pricing.json` (ship this file; user-editable)

```json
{
  "updated": "2026-06-20",
  "currency": "USD",
  "unit": "per_million_tokens",
  "models": {
    "claude-opus-4-8":   { "input": 5.00, "output": 25.00, "fast_input": 10.00, "fast_output": 50.00 },
    "claude-opus-4-7":   { "input": 5.00, "output": 25.00 },
    "claude-sonnet-4-6": { "input": 3.00, "output": 15.00 },
    "claude-haiku-4-5":  { "input": 1.00, "output": 5.00 },
    "claude-opus-4-1":   { "input": 15.00, "output": 75.00 }
  },
  "modifiers": {
    "cache_read_multiplier": 0.10,
    "cache_write_5m_multiplier": 1.25,
    "cache_write_1h_multiplier": 2.00,
    "batch_multiplier": 0.50
  },
  "fallback_model": "claude-sonnet-4-6"
}
```

> Current rates verified June 2026: Opus 4.8 $5/$25 (Fast Mode $10/$50), Opus 4.7 $5/$25,
> Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5, legacy Opus 4.1 $15/$75. Cache read ≈ 90% off input;
> 5-min cache write = 1.25× input, 1-hr cache write = 2× input; Batch API = 50% off.

### 4.2 `pricing.py` — `compute_cost(event, pricing) -> float`

All rates are per **million** tokens, so divide token counts by 1_000_000.

```
rates = pricing["models"].get(event.model)
if rates is None:
    rates = pricing["models"][pricing["fallback_model"]]   # and flag model as unknown

in_rate  = rates["input"]
out_rate = rates["output"]
mod      = pricing["modifiers"]

cache_write_mult = mod["cache_write_1h_multiplier"] if event.cache_ttl == "1h"
                   else mod["cache_write_5m_multiplier"]

cost = (
    event.input_tokens          * in_rate
  + event.output_tokens         * out_rate
  + event.cache_read_tokens     * in_rate * mod["cache_read_multiplier"]
  + event.cache_creation_tokens * in_rate * cache_write_mult
) / 1_000_000

if event.is_batch:
    cost *= mod["batch_multiplier"]

return round(cost, 6)
```

Edge cases the engine must handle:
- Unknown model → use `fallback_model`, set `raw_meta.unknown_model = true`, still record.
- Missing token fields → treat as 0.
- Never crash ingestion because of a pricing miss.

### 4.3 Optional pricing refresh

`clauditor refresh-pricing` may fetch updated rates. Default OFF and manual only. If you
implement it, it must (a) only write to `pricing.json`, (b) preserve the `updated` date,
(c) never block ingest. Surface the `updated` date in the dashboard header so stale prices
are visible.

---

## 5. Collectors

### 5.1 Claude Code collector (`collectors/claude_code.py`) — PRIMARY, build first

**Source location:** `~/.claude/projects/<project-dir>/<session-uuid>.jsonl`
(resolve `~` via `Path.home()`; allow override via `config.json -> claude_code_path`).

Each `.jsonl` file is one session; each line is a JSON object. Assistant message lines carry
a `usage` object. Be defensive — the exact schema varies by Claude Code version, so look up
fields by **key**, not position, and skip lines that don't parse or lack usage.

**Per line, attempt to extract:**
| Field | Where to look (try in order) |
|-------|------------------------------|
| timestamp | `obj.timestamp`, `obj.ts`, file mtime fallback |
| model | `obj.message.model`, `obj.model` |
| usage object | `obj.message.usage`, `obj.usage` |
| input_tokens | `usage.input_tokens` |
| output_tokens | `usage.output_tokens` |
| cache_creation_tokens | `usage.cache_creation_input_tokens` |
| cache_read_tokens | `usage.cache_read_input_tokens` |
| session_id | filename stem |
| project | parent directory name (sanitize Claude Code's path-encoding of dir names) |

**Dedupe (`event_uid`):** build a stable hash from
`source + file_path + line_number + message_id (if present)`. Use `INSERT OR IGNORE` on the
`event_uid` UNIQUE constraint so re-ingesting is safe.

**Incremental ingest:** read `ingest_state.last_offset` for the file; `seek()` to it; read new
bytes only; update offset + mtime after. If file mtime is unchanged since `last_ingested`,
skip the file entirely.

Mark all rows `source='claude_code'`, `is_batch=0`. Claude Code usage is interactive.

### 5.2 API wrapper (`collectors/api_wrapper.py`) — for the user's own API calls

A small importable helper the user drops into their code. Two integration modes:

**Mode A — explicit logging:**
```python
from clauditor import log_usage   # thin re-export

resp = client.messages.create(...)
log_usage(resp, project="my-rag-app", is_batch=False, cache_ttl="5m")
```

**Mode B — wrapped client (nice-to-have):** a `track(client, project=...)` that returns a
proxy logging every `.messages.create()` automatically.

`log_usage(response, project, is_batch=False, cache_ttl=None)` reads:
- `response.model`
- `response.usage.input_tokens`, `.output_tokens`
- `getattr(response.usage, "cache_creation_input_tokens", 0)`
- `getattr(response.usage, "cache_read_input_tokens", 0)`

…computes cost via the pricing engine and inserts a row with `source='api'`. Must work with
both `dict` responses and the Anthropic SDK's typed objects (duck-type with `getattr`/`.get`).

### 5.3 Admin API collector (`collectors/admin_api.py`) — OPTIONAL, build last

For org-wide rollups without instrumenting code. Polls Anthropic's Admin Usage & Cost
endpoints on demand using an admin key from `config.json` (or env var
`ANTHROPIC_ADMIN_KEY`). Lower resolution; mark rows `source='admin_api'`. Gate the entire
collector behind a config flag, default OFF. If no key, the collector is a silent no-op.

---

## 6. Analyzer (`core/analyzer.py`) — the differentiator

Two outputs: **budget alerts** and **savings suggestions**. Both are pure functions over the
`usage_events` table; they never mutate usage data.

### 6.1 Budget alerts

Read budgets from `config.json` (see §8). After every ingest, for each configured budget,
compute spend in the current period and compare:

- Periods: `daily` (UTC day), `weekly` (ISO week), `monthly` (calendar month).
- Scopes: `global`, and per-project (`project:<name>`).
- Fire at configurable fractions (default `[0.8, 1.0]` of budget).
- Use `alerts_log` UNIQUE(scope, period, period_key) to fire **once** per crossing per period.
- Delivery: write to an `alerts` API endpoint (always), plus optional desktop notification
  (use `plyer` if available; degrade gracefully) and optional webhook URL from config.

### 6.2 Savings suggestions — three rules

Each rule returns: `{title, detail, estimated_monthly_savings_usd, confidence}`. Compute over
a configurable lookback window (default last 30 days). Always show the dollar figure.

**Rule 1 — Model downgrade.** Find projects/sessions heavily on Opus whose usage pattern
looks like Haiku/Sonnet work: high call count + small median output tokens + low input
variance (repetitive prompts). For matching events, recompute cost at the cheaper model's
rates and report the delta:
> "1,212 Opus calls in `etl-pipeline` look like extraction/classification. Re-running on
> Haiku 4.5 would have cost $4.10 instead of $20.50 — **save ~$16.40/mo (80%)**."

Thresholds (tunable constants at top of file): `median_output_tokens < 300`,
`calls >= 100`, model in Opus family.

**Rule 2 — Missing prompt cache.** Find events with large `input_tokens` and
`cache_read_tokens == 0` that recur with a near-identical prompt prefix (same project + model +
similar input size repeated ≥ N times). Estimated savings = repeated input tokens ×
input_rate × 0.90 (the cache-read discount), minus one-time write cost.
> "`support-bot` sends ~8k identical system tokens on every call, uncached. Enabling prompt
> caching would **save ~$22/mo**."

Since the collector may not have prompt text, approximate "identical prefix" using
(project, model, input_tokens bucketed to nearest 500) repeated frequently. Mark confidence
`medium` when inferring from token sizes alone.

**Rule 3 — Batch candidates.** Find bursts of non-interactive `source='api'` events (many
calls in a short window, `is_batch=0`) that could tolerate latency. Estimated savings = their
cost × 0.50.
> "412 calls in `nightly-report` ran synchronously. The Batch API would **save ~$9/mo**."

### 6.3 Cache efficiency metric

Expose a headline number: `cache_read_tokens / (input_tokens + cache_read_tokens)` over the
selected window. This is the single most actionable health metric — surface it prominently.

---

## 7. Server (`server/app.py`, `server/routes.py`)

FastAPI app. Serves the static frontend from `/` and JSON from `/api/*`. Bind to
`127.0.0.1` only (never `0.0.0.0`) — it's a local tool. Default port `4747`, configurable.

### Endpoints (all read from SQLite; all accept `?from=&to=&project=&model=` filters)

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/summary` | totals: spend, tokens, call count, cache-efficiency %, for the range |
| GET | `/api/timeseries?granularity=day` | spend + tokens per bucket, split by model |
| GET | `/api/breakdown?by=project\|model\|source` | grouped spend + tokens |
| GET | `/api/suggestions` | analyzer savings suggestions (§6.2) |
| GET | `/api/alerts` | recent fired alerts + current budget status/gauges |
| GET | `/api/pricing` | current pricing.json contents + `updated` date |
| POST | `/api/ingest` | trigger a fresh ingest run; returns rows added |
| GET | `/api/health` | `{status, db_path, event_count, pricing_updated}` |

Validate date params; default range = last 30 days. Return clean JSON, HTTP 4xx on bad input.

---

## 8. Config (`config.json`, user-editable; ship sensible defaults)

```json
{
  "port": 4747,
  "claude_code_path": null,
  "currency": "USD",
  "lookback_days": 30,
  "ingest_on_serve": true,
  "budgets": {
    "global":  { "daily": null, "weekly": null, "monthly": 200 },
    "projects": {
      "etl-pipeline": { "monthly": 50 }
    }
  },
  "alert_fractions": [0.8, 1.0],
  "alert_webhook_url": null,
  "desktop_notifications": true,
  "admin_api": { "enabled": false, "key_env": "ANTHROPIC_ADMIN_KEY" }
}
```

`null` budget = no limit for that period. Missing keys fall back to defaults defined in
`core/config.py`. Validate on load; print a clear error and exit non-zero on malformed config.

---

## 9. CLI (`cli.py`)

Use `argparse` or `typer`. Commands:

| Command | Action |
|---------|--------|
| `clauditor ingest` | run all enabled collectors once, print rows added per source |
| `clauditor serve` | start server (runs ingest first if `ingest_on_serve`), open browser |
| `clauditor suggest` | print savings suggestions to terminal (no server) |
| `clauditor status` | print current-period spend vs budgets |
| `clauditor refresh-pricing` | (optional) update pricing.json |
| `clauditor reset` | wipe data/clauditor.db after a typed confirmation |

`serve` is the headline command. On launch, print the local URL and auto-open it.

---

## 10. Frontend (`web/`)

Single-page app, no framework, no build step. `index.html` + `main.js` + `styles.css`.
Chart.js from CDN. Fetch from `/api/*`. Render progressively (show skeletons, fill as data
arrives; don't block the whole UI on one slow query).

**Layout (top to bottom):**
1. **Header bar** — app name, date-range picker (presets: 7d / 30d / 90d / all + custom),
   global project filter, and a small "pricing updated: YYYY-MM-DD" badge (amber if > 30 days old).
2. **Summary cards** — total spend, total tokens, call count, **cache-efficiency %**.
3. **Spend-over-time** — stacked area/bar chart by model.
4. **Breakdown** — toggle between by-project / by-model / by-source (bar + table).
5. **Suggestions feed** — analyzer cards, each showing the dollar estimate and confidence.
6. **Budget gauges** — progress bars per configured budget; turn amber at 80%, red at 100%.

Keep it clean and readable; this is a utility, not a marketing page. Dark mode is a plus.
No `localStorage`/`sessionStorage` for app state if you reuse this in a sandboxed renderer —
hold UI state in JS variables. (In a normal browser it's fine, but keep state in-memory to be safe.)

---

## 11. Accuracy & safety requirements (non-negotiable)

- **Never re-tokenize text to estimate counts.** Use logged token counts only.
- **Treat `~/.claude` as read-only.** Open files read-only; never write there.
- **Dedupe rigorously** so repeated ingests never double-count (`event_uid` UNIQUE).
- **Fail soft on parse errors** — skip bad lines, keep a count, never abort a whole ingest.
- **Show the pricing date** in the UI; flag unknown models rather than silently mispricing.
- **Bind to localhost only.** No remote exposure.
- **No network calls** except the optional, manual pricing refresh and optional admin-API poll.

---

## 12. Testing

- Unit-test `pricing.py` with known token counts → expected dollar values (include cache +
  batch + unknown-model cases).
- Unit-test the Claude Code parser against 2–3 fixture `.jsonl` lines (vary the schema:
  `message.usage` vs `usage`, missing cache fields).
- Test dedupe: ingest the same fixture twice → row count unchanged.
- Test incremental ingest: append a line to a fixture → only the new line is added.
- Test the analyzer rules against a seeded DB with planted downgrade/cache/batch patterns.
- Provide a `seed_demo.py` that fills the DB with synthetic data so the dashboard can be
  demoed without real `~/.claude` history.

---

## 13. Deliverables

1. Working repo matching the structure in §2.
2. `clauditor serve` brings up the dashboard at `http://localhost:4747` with real data from
   the user's `~/.claude` history (or seeded demo data if none).
3. `pyproject.toml` with pinned deps (fastapi, uvicorn, and `anthropic` only as an optional
   extra for the API wrapper — the core tool must run without it).
4. `README.md`: install, the four commands users actually need, how to set budgets, how to
   integrate `log_usage` into their own code, and how to update `pricing.json`.

---

## 14. Build order (do in this sequence)

1. `core/db.py` (schema + init) and `core/config.py` (load config + pricing).
2. `core/pricing.py` + its unit tests. ← prove cost math before anything else.
3. `collectors/claude_code.py` + parser tests. ← gives real data with zero user setup.
4. `cli.py` with `ingest` working end-to-end into SQLite.
5. `server/app.py` + `/api/summary`, `/api/timeseries`, `/api/breakdown`, `/api/health`.
6. `web/` dashboard reading those endpoints; `clauditor serve` opens it.
7. `collectors/api_wrapper.py` (`log_usage`) + tests.
8. `core/analyzer.py` (suggestions + alerts) + `/api/suggestions`, `/api/alerts`; wire the
   suggestions feed and budget gauges into the UI.
9. `seed_demo.py`, full README, polish.
10. `collectors/admin_api.py` (optional, flag-gated) last.

Ship after step 6 is a usable MVP; steps 7–9 complete the promised feature set; step 10 is
the org/team extension.
