# clauditor

**clauditor** is a local-first, self-hosted dashboard that tracks your Anthropic
token usage and dollar spend across both your Claude Code sessions and your own
Claude API calls. It breaks spend down by project, model, and time; fires budget
alerts; and surfaces concrete cost-saving suggestions (prompt caching, model
downgrades, the Batch API). You run one command, a local web server starts, and
a dashboard opens at `http://localhost:4747`. Everything runs on your machine —
no cloud, no accounts, no external database.

## Safety posture

clauditor is built to be trustworthy with your data:

- **Localhost only.** The server binds to `127.0.0.1` and is never exposed
  remotely.
- **Read-only on `~/.claude`.** Session logs are opened read-only; clauditor
  never edits or deletes anything there.
- **No telemetry.** The only outbound network call the tool can make is the
  optional, manually triggered pricing refresh (and an optional admin-API poll,
  off by default). Nothing phones home.
- **Authoritative token counts.** clauditor never re-tokenizes your text to
  estimate usage — it uses the token counts already in the logs/responses.
- **Rigorous dedupe and fail-soft parsing.** Re-ingesting never double-counts,
  and a single malformed log line never aborts an ingest.

## Install

Requires **Python 3.11+**.

From the project root:

```bash
pip install -e .
```

or, with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install -e .
```

This installs the core dependencies, **FastAPI** and **Uvicorn**, and exposes
the `clauditor` command. The core tool runs without any optional extras.

Optional extras:

```bash
pip install -e ".[anthropic]"      # Anthropic SDK, only for the API wrapper
pip install -e ".[notifications]"  # plyer, for desktop budget-alert popups
pip install -e ".[dev]"            # pytest + httpx, for running the test suite
```

- `anthropic` is only needed if you want to log your own API calls *and* you
  pass the SDK's typed response objects — the wrapper also works with plain
  dicts, so the SDK is never strictly required.
- `notifications` (plyer) powers the optional desktop notification on a budget
  crossing; without it, budget alerts degrade gracefully to the dashboard and
  the optional webhook.

## The commands you need

clauditor has a small CLI. The three global path flags `--config`, `--pricing`,
and `--db` are accepted by every command and let you point at non-default files
(handy for demos and tests):

| Flag | Default |
|------|---------|
| `--config PATH` | project-root `config.json` |
| `--pricing PATH` | project-root `pricing.json` |
| `--db PATH` | `data/clauditor.db` |

### `clauditor serve` — the headline command

```bash
clauditor serve
```

Starts the local dashboard at `http://localhost:4747` (bound to `127.0.0.1`
only) and auto-opens your browser. If `ingest_on_serve` is true (the default),
it runs an ingest pass first so the dashboard opens on fresh data. The port is
configurable in `config.json`.

### `clauditor ingest`

```bash
clauditor ingest
```

Runs every enabled collector once and prints the number of rows added per
source (Claude Code sessions, and any API calls you've logged). Safe to re-run —
incremental and deduplicated. After ingest it evaluates your budgets and fires
any new alert crossings.

### `clauditor suggest`

```bash
clauditor suggest
```

Prints the analyzer's savings suggestions to the terminal — no server. Each
suggestion shows a dollar estimate and a confidence level, covering model
downgrades, missing prompt caches, and Batch-API candidates over your lookback
window (default last 30 days).

### `clauditor status`

```bash
clauditor status
```

Prints current-period spend versus your configured budgets (global and
per-project, for daily/weekly/monthly), flagging anything at 80% (`[AMBER]`) or
100% (`[RED]`). This is a pure read — it never fires alerts.

### Other commands

- `clauditor reset` — wipes **only** the clauditor database (the path resolved
  from `--db`, defaulting to `data/clauditor.db`) after a typed confirmation.
  By default it prompts and you must type `reset` to confirm; anything else
  (including an empty line or Ctrl-D) aborts with nothing changed (exit 0).
  Pass `--yes`/`--force` to skip the prompt for automation. It never touches
  `~/.claude`, `config.json`, or `pricing.json`. The next ingest/serve recreates
  an empty database.
- `clauditor refresh-pricing` — **optional and not implemented.** Pricing is
  user-edited in `pricing.json`. The command is reserved in the CLI and reports
  that it is not implemented if invoked.

## Try it without any history (demo data)

To see a fully populated dashboard without real `~/.claude` history, seed
synthetic demo data and serve it:

```bash
python seed_demo.py     # fills data/clauditor.db with synthetic events
clauditor serve         # open the populated dashboard
```

`seed_demo.py` inserts realistic events across several projects, models, and
sources (spread over the last ~30 days), with enough cache traffic to make the
cache-efficiency metric meaningful and with patterns that make all three savings
rules fire. Use `--db <path>` to target a throwaway database instead:

```bash
python seed_demo.py --db /tmp/demo.db
clauditor suggest --db /tmp/demo.db
```

## Setting budgets

Budgets and settings live in `config.json` at the project root. Any key you omit
falls back to the in-code defaults, so a minimal config is fine. Validation runs
on load — a malformed config prints a clear error and exits non-zero.

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
      "my-app":         { "monthly": 50 },
      "nightly-report": { "daily": 5, "monthly": 100 }
    }
  },
  "alert_fractions": [0.8, 1.0],
  "alert_webhook_url": null,
  "desktop_notifications": true,
  "admin_api": { "enabled": false, "key_env": "ANTHROPIC_ADMIN_KEY", "key": null, "base_url": null }
}
```

- **`budgets.global`** and **`budgets.projects.<name>`** each take `daily`,
  `weekly`, and `monthly` keys. A value of **`null` means no limit** for that
  period. The `projects` map **defaults to empty** — no example project is baked
  in, so a per-project budget only appears once you add it yourself. The shape of
  a per-project budget is shown above and in the shipped `config.example.json`.
- **`alert_fractions`** sets the fractions of a budget at which alerts fire
  (default `0.8` and `1.0`, i.e. 80% and 100%). Each crossing fires once per
  period.
- **`alert_webhook_url`** — optional URL that receives a JSON POST when an alert
  fires (`null` to disable).
- **`desktop_notifications`** — when true and `plyer` is installed, a budget
  crossing also raises a desktop notification (best-effort).
- **`lookback_days`** — the window used by the savings suggestions and the
  dashboard's default date range.
- **`claude_code_path`** — override the location of your Claude Code session
  logs (`null` uses `~/.claude/projects`).
- **`admin_api`** — **optional, org-wide, default OFF.** When `enabled` is
  `false` (the default) this collector is completely inert: it reads no key,
  imports no HTTP client, and makes **no network call**. To opt in for org-wide
  rollups, set `enabled: true` and supply an admin key either inline via `key`
  or — preferred — via the environment variable named by `key_env` (default
  `ANTHROPIC_ADMIN_KEY`). With `enabled: true` but no key resolvable, it stays a
  silent no-op (never an error). `base_url` (default `null`) overrides the
  endpoint for a gateway/proxy. This is the only collector that polls
  Anthropic's network endpoints, and only when you turn it on.

A copy of this structure with sample per-project budgets ships as
`config.example.json` — copy it to `config.json` and edit.

## Logging your own API calls

To track your own Anthropic API spend alongside Claude Code, drop one line into
your code. Token counts are read verbatim from the response (never
re-tokenized), the cost is computed by the same pricing engine, and a row is
inserted with `source='api'`.

The signature is:

```python
log_usage(response, project, is_batch=False, cache_ttl=None)
```

**Mode A — explicit logging:**

```python
from clauditor import log_usage

resp = client.messages.create(...)
log_usage(resp, project="my-rag-app", is_batch=False, cache_ttl="5m")
```

`response` may be the Anthropic SDK's typed response object **or** a plain
`dict` — both are duck-typed, so importing `clauditor` never requires the
`anthropic` SDK. clauditor reads `response.model`, `response.usage.input_tokens`,
`.output_tokens`, and (if present) `cache_creation_input_tokens` /
`cache_read_input_tokens`. `is_batch=True` applies Batch-API pricing; `cache_ttl`
(`"5m"` or `"1h"`) applies cache-write pricing. Logging the same response twice
is deduplicated.

**Mode B — wrapped client:** wrap your client once and every
`messages.create()` is logged automatically (and a logging error never breaks
your real API call):

```python
from clauditor import track

client = track(anthropic.Anthropic(), project="my-app")
resp = client.messages.create(...)   # logged automatically
```

By default the row is written to `data/clauditor.db`; pass `db_path=...` to
`log_usage` / `track` to target another database.

## Updating pricing

Model rates live in `pricing.json` at the project root and are **user-editable**.
All rates are **per million tokens**:

```json
{
  "updated": "2026-06-20",
  "currency": "USD",
  "unit": "per_million_tokens",
  "models": {
    "claude-opus-4-8":   { "input": 5.00, "output": 25.00, "fast_input": 10.00, "fast_output": 50.00 },
    "claude-sonnet-4-6": { "input": 3.00, "output": 15.00 },
    "claude-haiku-4-5":  { "input": 1.00, "output": 5.00 }
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

- **`models`** — per-model `input` / `output` rates. To add or reprice a model,
  edit this map.
- **`modifiers`** — multipliers applied on top: cache reads cost 10% of the
  input rate, a 5-minute cache write costs 1.25× input, a 1-hour cache write 2×
  input, and the Batch API is 50% off.
- **`fallback_model`** — used to price any model not listed; such events are
  still recorded and flagged as an unknown model rather than dropped.
- **`updated`** — the date shown in the dashboard header so stale prices are
  visible (it turns amber when older than 30 days). Bump it when you edit rates.

Just edit the file and save — changes apply on the next command. The optional
`refresh-pricing` command is reserved but not implemented; pricing is maintained
by hand.
