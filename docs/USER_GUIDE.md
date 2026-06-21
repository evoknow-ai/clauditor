# clauditor — User Guide

clauditor is a **local-first, self-hosted, open-source** dashboard for tracking
your Anthropic token usage and dollar spend. It pulls usage from your Claude Code
sessions and (optionally) from your own Claude API calls, prices every event from
a user-editable rate table, and gives you spend breakdowns, budget alerts, and
concrete cost-saving suggestions.

Everything runs on your own machine: the web server binds to `127.0.0.1` only,
`~/.claude` is read-only, token counts are taken verbatim from the logs (never
re-tokenized), and there is no telemetry. The only outbound network calls the
tool can make are the **optional, opt-in** admin-API poll (off by default) and
the optional alert webhook — nothing else phones home.

It is free to run and free to modify.

---

## Table of contents

1. [The CLI](#the-cli)
2. [Reading the dashboard](#reading-the-dashboard)
3. [How clauditor discovers projects](#how-clauditor-discovers-projects)
4. [How budgets work](#how-budgets-work)
5. [How the savings suggestions work](#how-the-savings-suggestions-work)
6. [Logging your own API calls](#logging-your-own-api-calls)
7. [Updating `pricing.json`](#updating-pricingjson)

---

## The CLI

The console script is `clauditor` (entry point `cli:main`). Three **global path
flags** are accepted by every subcommand (they hang off a shared parent parser,
so they work both before and after the subcommand name):

| Flag | Default | Meaning |
|------|---------|---------|
| `--config PATH` | project-root `config.json` | Path to the config file. |
| `--pricing PATH` | project-root `pricing.json` | Path to the pricing table. |
| `--db PATH` | `data/clauditor.db` | Path to the SQLite database. |

Running `clauditor` with no subcommand prints help to stderr and exits with code
`2`.

The subcommands that exist are: `ingest`, `serve`, `suggest`, `status`,
`reset`, and `refresh-pricing`.

### `clauditor ingest`

```bash
clauditor ingest
```

Runs every enabled collector once and prints the number of rows added per source.
It loads config + pricing, initializes the SQLite DB idempotently, then runs each
registered collector:

- `claude_code` — reads your Claude Code session logs.
- `admin_api` — the optional org-wide Admin Usage/Cost collector. With the
  default config (`admin_api.enabled = false`) it is a complete no-op and
  contributes 0 rows.

Each source is **fail-soft**: if one collector raises, ingest prints
`<source>: skipped (error: ...)` to stderr and continues with the others — a
single bad source never aborts the run. Output includes a per-source line such
as `claude_code: 12 rows added (3 files scanned, 1 lines skipped)` and a final
`Done. N new rows (database now holds X events; was Y).`

After ingest, clauditor evaluates your budgets and persists any **new** alert
crossings (this is the only path that fires alerts). Re-running is safe:
ingestion is deduplicated by a `UNIQUE(event_uid)` constraint, so it never
double-counts. Exit code `0` on success.

### `clauditor serve`

```bash
clauditor serve
```

Starts the local dashboard server and auto-opens your browser. It loads config +
pricing, ensures the DB schema exists, and — when `ingest_on_serve` is true (the
default) — runs an ingest pass first so the dashboard opens on fresh data
(fail-soft per source, same as `ingest`). It then prints the local URL and starts
uvicorn.

The server **binds to `127.0.0.1` only**. The bind host is hardcoded inside
`server.app.run_server`; there is no flag to bind elsewhere. The port comes from
the `port` config key (default `4747`), so the dashboard opens at
`http://localhost:4747` by default. Press Ctrl-C to stop. The browser open is
best-effort and runs on a background thread; a headless machine will not crash
`serve`.

### `clauditor suggest`

```bash
clauditor suggest
```

Prints the analyzer's savings suggestions to the terminal — **no server is
started**. It opens the DB read-only and runs the three savings rules over the
configured lookback window (default 30 days). For each suggestion it prints the
title, the estimated monthly savings, the confidence level, and the detail text.

If the DB does not exist yet, it prints
`No data yet -- run 'clauditor ingest' first. No suggestions.` and exits `0`. If
the data is efficient and nothing qualifies, it prints
`No savings suggestions -- usage looks efficient over the window.` and exits `0`.

### `clauditor status`

```bash
clauditor status
```

Prints current-period spend versus your configured budgets — **a pure read that
never fires alerts**. It opens the DB read-only and reports, for each configured
budget, the scope/period/period-key, current spend, the budget amount, the
fraction used, and an `[AMBER]` flag at 80% or `[RED]` flag at 100%.

If the DB does not exist yet, it prints
`No data yet -- run 'clauditor ingest' first. No budget status.` If no budgets
are configured, it prints
`No budgets configured. Set 'budgets' in config.json (SPEC §8).` Exit code `0`.

### `clauditor reset`

```bash
clauditor reset            # interactive: requires you to type "reset"
clauditor reset --yes      # skip the prompt (automation); --force is an alias
```

Wipes **only** the resolved clauditor database file — the path from `--db`,
defaulting to `data/clauditor.db`. It never touches `~/.claude`, `config.json`,
`pricing.json`, or any source file.

Confirmation behavior:

- **Interactive (default):** you are shown the exact DB path that will be deleted
  and must type the literal word `reset` (case-insensitive, whitespace-stripped)
  to confirm. A wrong word, an empty line, or EOF/Ctrl-D **declines** the wipe.
  Declining prints `Reset aborted, nothing was changed.` and exits `0` (declining
  is not an error).
- **`--yes` / `--force`:** an explicit opt-in that skips the prompt, for
  automation and tests.

If there is no database to remove, it prints `Nothing to reset -- no database at
<path>.` and exits `0`. On a successful delete it prints `Reset complete --
removed <path>. A fresh, empty database is recreated on the next ingest/serve.`
and exits `0`. Only a deletion failure (e.g. a permissions error) exits non-zero
(`1`).

### `clauditor refresh-pricing`

```bash
clauditor refresh-pricing
```

**Optional and not implemented.** The command is reserved so the CLI surface
stays stable, but invoking it just prints
`clauditor refresh-pricing: optional, not implemented (see SPEC §4.3).` to stderr
and exits with code `2`. Pricing is maintained by hand — see
[Updating `pricing.json`](#updating-pricingjson).

---

## Reading the dashboard

The dashboard is a single static page served from `/`; all data comes from
read-only `/api/*` endpoints. Each panel fetches independently and fills in as
its request resolves, so a slow query never blocks the rest of the UI.

### Header bar

The header carries the global controls that drive every panel:

- **Date-range presets:** `7d`, `30d`, `90d`, and `All`. A preset sets the
  `from`/`to` query parameters used by the data fetches (`All` clears them so the
  server's default applies). The default on load is **30d**.
- **Custom range:** `From`/`To` date inputs plus an `Apply` button. Applying sets
  an explicit range and clears the preset highlight.
- **Project filter:** a dropdown listing every project found in the data (plus
  "All projects"). Selecting one adds `project=<name>` to every fetch. Its
  options are populated from an unfiltered `/api/breakdown?by=project` call.
- **Pricing-updated badge:** shows `pricing updated: <date>` from
  `/api/health` (`pricing_updated`). It turns **amber/stale** when the date is
  unknown or **more than 30 days old**, signalling that you should review your
  rates.

### Panel → endpoint map

| Panel | Endpoint | What it shows |
|-------|----------|---------------|
| Summary cards | `GET /api/summary` | Total spend, total tokens, call count, cache efficiency % |
| Spend over time | `GET /api/timeseries` (`granularity=day`) | Daily spend, stacked by model |
| Breakdown | `GET /api/breakdown` (`by=project\|model\|source`) | Grouped spend/tokens/calls |
| Savings suggestions | `GET /api/suggestions` | Analyzer savings suggestions |
| Budgets | `GET /api/alerts` | Current-period budget gauges |
| Pricing badge | `GET /api/health` | `pricing_updated` date |

All of `summary`, `timeseries`, `breakdown`, and `suggestions` honor the shared
`from` / `to` / `project` / `model` filters. `/api/alerts` is the current period
only and ignores the range filters.

### Summary cards

Four cards from `/api/summary`:

- **Total spend** — sum of `cost_usd` over the range (`total_spend_usd`).
- **Total tokens** — `input + output + cache_creation + cache_read` tokens.
- **Calls** — number of usage events (`call_count`).
- **Cache efficiency** — `cache_read_tokens / (input_tokens + cache_read_tokens)`,
  shown as a percentage. It is `0.0` when there is no input/cache-read traffic.
  A higher number means more of your input was served from prompt cache.

### Spend over time

From `/api/timeseries` at day granularity. It is a **stacked bar chart**: the
x-axis is the day bucket and each stacked segment is one model's spend for that
day, so you can see both total daily spend and the model mix. Empty ranges show
"No usage in this range."

### Breakdown

From `/api/breakdown`. A toggle switches the grouping dimension between **By
project**, **By model**, and **By source** (`source` is e.g. `claude_code` or
`api`). It renders a horizontal bar chart of spend plus a table of
Key / Spend / Tokens / Calls, ordered by spend descending. A `null` group key is
displayed as `(none)`.

### Savings suggestions

From `/api/suggestions`. Each card shows the suggestion **title**, the
**estimated monthly savings** (`$X.XX/mo`), the **detail** text explaining how
the figure was derived, and a **confidence** badge (`high` / `medium`). Cards are
sorted by estimated savings descending. An efficient dataset shows "No savings
suggestions for this range. Nice and efficient." See
[How the savings suggestions work](#how-the-savings-suggestions-work).

### Budgets

From `/api/alerts` (`budgets`). One gauge per configured budget period, labelled
`<scope> — <period>` (e.g. `Global — monthly` or `my-app — monthly`). The fill
bar width is the fraction of budget used, and its color follows the level the
server computes: **amber at 80%**, **red at 100%**. Each gauge shows
`spend / budget (percent)`. If no budgets are configured, it shows
"No budgets configured. Set them in config.json under "budgets"."

---

## How clauditor discovers projects

clauditor keeps no list of projects. There is nothing to register and no
`projects` array to maintain — every project you see on the dashboard is
**derived from the data that was ingested**, not configured anywhere. A project
exists because usage attributed to it landed in the database; it disappears from
a view only when no events match the current filters.

**Claude Code projects are automatic.** The Claude Code collector scans
`~/.claude/projects/` (override with the `claude_code_path` config key), where
Claude Code creates **one directory per project**. The project's real filesystem
path is encoded into the directory name — path separators become `-`, so
`/Users/you/projects/clauditor` becomes a directory named
`-Users-you-projects-clauditor`. The collector globs `*/*.jsonl` under that root,
so every project directory that has session history is picked up, and the project
label is decoded from the directory name by `decode_project_name` (it returns the
trailing path segment — `clauditor` in the example above). No setup is required:
a project shows up on its own once it has local Claude Code session history.

**API projects require labeling.** A raw Anthropic API response carries no
project name, so clauditor cannot infer one. API-sourced spend appears **only**
where you have wrapped your own calls with `log_usage`, passing the label
explicitly:

```python
log_usage(response, project="my-rag-app")
```

The `project` you pass *is* the project name on the dashboard. This means
**unwrapped API workloads are invisible to clauditor** — if you call the
Anthropic API without `log_usage` (or the `track(...)` wrapper), that spend is
not tracked at all. Don't assume your API usage shows up automatically; it only
does where you instrumented it. (See
[Logging your own API calls](#logging-your-own-api-calls) for the full
signature and integration modes.)

**Discovery happens at ingest time.** Projects appear when their data is
ingested — by running `clauditor ingest`, or on `clauditor serve` when
`ingest_on_serve` is enabled. A project you started using since the last ingest
shows up **after the next ingest, not instantly**. Ingest is incremental: each
session file is read from where it left off (by byte offset, skipping files whose
mtime hasn't changed), so re-ingesting an existing project just appends its new
sessions rather than re-reading everything.

> Usage from the Claude apps (claude.ai web/desktop) and from other machines is
> out of scope for the local collectors, which only read this machine's
> `~/.claude` logs and your own `log_usage`-instrumented API calls — the optional
> Admin API collector is what covers org-wide rollups.

---

## How budgets work

Budgets are configured in `config.json` under `budgets`, with a `global` scope
and an optional per-project map. Each scope can set `daily`, `weekly`, and
`monthly` limits independently; a value of **`null` means no limit** for that
period.

```json
"budgets": {
  "global":  { "daily": null, "weekly": null, "monthly": 200 },
  "projects": {
    "my-app":         { "monthly": 50 },
    "nightly-report": { "daily": 5, "monthly": 100 }
  }
}
```

The shipped `config.json` defaults `budgets.projects` to **`{}`** (empty) — no
phantom project is baked in. Because your config is deep-merged onto the in-code
defaults, any example project left in the defaults would leak into every user's
live config; so the per-project shape lives in `config.example.json` (shown
above) instead of the defaults.

### Periods and period keys

Spend is summed over the current calendar period (UTC):

- **daily** → keyed `YYYY-MM-DD`
- **weekly** → keyed `YYYY-Www` (ISO week, Monday start)
- **monthly** → keyed `YYYY-MM`

### Alert fractions and fire-once behavior

`alert_fractions` (default `[0.8, 1.0]`) sets the budget fractions at which an
alert fires — i.e. 80% and 100%. Alerts are persisted during the
ingest/serve analyze step (never on a dashboard refresh). Each crossing fires
**once per period per fraction**: the fired fraction is encoded into the
`alerts_log` dedupe key, rendered compactly via `format(round(fraction, 4), "g")`
(so `0.8` → `0.8` but `1.0` → `1`) — e.g. `2026-06@0.8` and `2026-06@1`. This way
re-running ingest never re-fires the same crossing, and 0.8 and 1.0 each fire
exactly once within a period.

The dashboard gauges (and `clauditor status`) color/flag at **amber ≥ 80%** and
**red ≥ 100%**, independent of when alerts fired.

### Optional alert delivery

When a **new** crossing fires, two best-effort deliveries run (a failure in
either never blocks ingest):

- **Desktop notification** — when `desktop_notifications` is true *and* the
  optional `plyer` extra is installed. If `plyer` is absent, it degrades silently
  to a no-op.
- **Webhook** — when `alert_webhook_url` is set (non-`null`), a JSON POST is sent
  to that URL via stdlib `urllib` (no extra dependency). The payload includes
  `title`, `message`, `scope`, `period`, `fraction`, `threshold`, `actual`, and
  `period_key`.

---

## How the savings suggestions work

The analyzer runs three rules over the lookback window (explicit `from`/`to`, or
the last `lookback_days` — default 30). Every dollar figure is computed from real
rows (priced by the same engine as the dashboard) and scaled to a per-30-day
monthly figure; nothing is hardcoded. Each suggestion has the shape
`{title, detail, estimated_monthly_savings_usd, confidence}`. A clean dataset
produces an empty list.

### Rule 1 — model downgrade (confidence: high)

Detects Opus-family work that looks like cheaper-model work. Per project, it
considers events whose model name contains `opus`, and qualifies the project when
**all** of the following hold (constants from the top of `core/analyzer.py`):

- call count ≥ `MIN_CALLS` (**100**), **and**
- median `output_tokens` < `MEDIAN_OUTPUT_TOKENS_MAX` (**300**) — outputs are
  small, **and**
- the coefficient of variation of `input_tokens` ≤ `MAX_INPUT_CV` (**0.5**) —
  prompts are repetitive.

The estimate is `(current Opus cost) − (same events recomputed at
DOWNGRADE_TARGET_MODEL rates)`, where the target is `claude-haiku-4-5`, scaled to
monthly. The `estimated_monthly_savings_usd` is what you'd save per month if that
work ran on the cheaper model. Skipped if the target model is absent from
`pricing.json`.

### Rule 2 — missing prompt cache (confidence: medium)

Detects recurring large uncached inputs that should be cached. It groups events
by `(project, model, input_tokens bucketed to the nearest 500)`, considering only
events with `input_tokens ≥ CACHE_INPUT_TOKENS_MIN` (**2000**) and
`cache_read_tokens == 0`. A group qualifies when it recurs ≥ `CACHE_MIN_REPEATS`
(**10**) times.

The estimate is `(repeated input tokens × input rate × 0.90 cache-read discount)
− (one-time 5-minute cache write at 1.25× input)`, scaled to monthly — i.e. the
net monthly savings from caching the repeated prompt. "Repeated" means every call
after the first (the first pays the write). Confidence is **medium** because it
infers cacheability from token sizes alone.

### Rule 3 — batch candidates (confidence: medium)

Detects bursts of synchronous API calls that could run on the (cheaper) Batch
API. It considers only `source='api'`, `is_batch=0` events. Per project, it finds
the densest `BATCH_WINDOW_MINUTES` (**60**) window; the project qualifies if that
window holds ≥ `BATCH_MIN_CALLS` (**100**) calls.

The estimate is `(total cost of the project's non-batch API events) × 0.50`
(`BATCH_SAVINGS_FRACTION`), scaled to monthly — what you'd save by moving that
synchronous traffic onto the 50%-off Batch API. Confidence is **medium**.

---

## Logging your own API calls

To track your own Anthropic API spend alongside Claude Code, log each response.
Token counts are read **verbatim** from the response (never re-tokenized), the
cost is computed by the same pricing engine, and the row is recorded with
`source='api'`. The `anthropic` SDK is an **optional** extra — the wrapper is
fully duck-typed and works with plain dicts, so `from clauditor import log_usage`
never requires the SDK.

### `log_usage` signature

```python
log_usage(
    response,
    project=None,
    is_batch=False,
    cache_ttl=None,
    *,
    conn=None,
    db_path=None,
    pricing=None,
)
```

- `response` — a plain `dict` **or** the Anthropic SDK's typed response object.
  clauditor reads `response.model`, `response.usage.input_tokens` /
  `.output_tokens`, and (if present) `cache_creation_input_tokens` /
  `cache_read_input_tokens`. Missing cache fields default to `0`.
- `project` — the project name recorded on the row.
- `is_batch` — `True` applies Batch-API (50% off) pricing and stores `is_batch=1`.
- `cache_ttl` — `"5m"` or `"1h"`; applies cache-write pricing.
- `conn` (keyword-only) — reuse an existing connection (you own commit/close).
- `db_path` (keyword-only) — target a non-default database (default
  `data/clauditor.db`; the schema is created if needed). When `conn` is not
  given, the connection is opened, committed, and closed for you.
- `pricing` (keyword-only) — supply a pricing table; otherwise the shipped
  `pricing.json` is loaded.

It returns `True` if a new row was inserted, or `False` if the same response was
already logged (deduped by the `event_uid` UNIQUE constraint). An unknown model
is still recorded and flagged `raw_meta.unknown_model = true`.

### Mode A — explicit logging

```python
from clauditor import log_usage

resp = client.messages.create(...)
log_usage(resp, project="my-rag-app", is_batch=False, cache_ttl="5m")
```

A plain dict works identically:

```python
from clauditor import log_usage

resp = {
    "id": "msg_123",
    "model": "claude-sonnet-4-6",
    "usage": {"input_tokens": 1200, "output_tokens": 300},
}
log_usage(resp, project="my-rag-app")
```

### Mode B — wrapped client

`track` returns a thin proxy that auto-logs every `messages.create()` call. A
logging error never breaks your real API call (fail-soft), and the real response
is always returned unchanged.

```python
from clauditor import track

client = track(anthropic.Anthropic(), project="my-app")
resp = client.messages.create(...)   # logged automatically
```

`track` takes `client` first, then `project`, and accepts `is_batch`, `cache_ttl`,
`conn`, `db_path`, and `pricing` as **keyword-only** arguments (unlike `log_usage`,
where `is_batch`/`cache_ttl` may also be passed positionally). Always pass them by
keyword, e.g. `track(client, project="my-app", is_batch=True, cache_ttl="5m")`.

---

## Updating `pricing.json`

Model rates live in `pricing.json` at the project root and are **user-editable**.
All rates are **per million tokens**. The dashboard surfaces the `updated` date
in the header badge so stale prices are visible (amber when older than 30 days).

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

- **`models`** — a non-empty map of model name → rates. Each model **must** have
  `input` and `output` rates (non-negative numbers). To add or reprice a model,
  edit this map.
- **`modifiers`** — multipliers applied on top of the base rates. All four are
  required:
  - `cache_read_multiplier` (e.g. `0.10`) — cache reads cost 10% of the input rate.
  - `cache_write_5m_multiplier` (e.g. `1.25`) — a 5-minute cache write costs 1.25× input.
  - `cache_write_1h_multiplier` (e.g. `2.00`) — a 1-hour cache write costs 2× input.
  - `batch_multiplier` (e.g. `0.50`) — the Batch API is 50% off.
- **`fallback_model`** — must name a model present in `models`. It prices any
  model not listed; such events are still recorded and flagged as an unknown
  model rather than dropped.
- **`updated`** — the date shown in the dashboard header. Bump it when you edit
  rates.

`pricing.json` has no in-code default table, so the file must exist; an invalid
or missing file prints a clear configuration error and exits non-zero. Just edit
and save — changes apply on the next command. The `refresh-pricing` command is
reserved but **not implemented**; pricing is maintained by hand.
