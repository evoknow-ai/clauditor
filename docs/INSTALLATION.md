# clauditor — Installation & Setup

clauditor is **open-source and free to run**. It is a local-first dashboard for
your Anthropic token usage and dollar spend — no cloud, no accounts, no external
database. The server binds to `127.0.0.1` only, `~/.claude` is read-only, and
there is no telemetry. This guide gets you from a clean checkout to a running
dashboard.

---

## Prerequisites

- **Python 3.11+** (the project declares `requires-python = ">=3.11"`).
- The core runtime dependencies, installed automatically:
  - **FastAPI** (`fastapi>=0.110`)
  - **Uvicorn** (`uvicorn>=0.29`)

The core tool runs with just those two. The following are **optional extras**,
named exactly as in `pyproject.toml`:

| Extra | Package(s) | What it enables |
|-------|------------|-----------------|
| `anthropic` | `anthropic>=0.40` | The API wrapper's typed-response support. The wrapper also works with plain dicts, so the SDK is never strictly required. |
| `notifications` | `plyer>=2.1` | The optional desktop notification on a budget crossing. Without it, alerts degrade gracefully (dashboard + optional webhook). |
| `dev` | `pytest>=8.0`, `httpx>=0.27` | Running the test suite (FastAPI's `TestClient` uses `httpx`). |

---

## Install

From the project root (the directory containing `pyproject.toml`):

```bash
pip install -e .
```

or, with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install -e .
```

This installs the core dependencies (**FastAPI** and **Uvicorn**) and exposes the
`clauditor` console command (entry point `cli:main`). The packaged static
dashboard (`web/`) ships with the install.

To add optional extras:

```bash
pip install -e ".[anthropic]"      # Anthropic SDK, only for the API wrapper
pip install -e ".[notifications]"  # plyer, for desktop budget-alert popups
pip install -e ".[dev]"            # pytest + httpx, for running the test suite
```

---

## First run

### With your real history

If you already use Claude Code, ingest your sessions and then serve:

```bash
clauditor ingest     # read Claude Code sessions into data/clauditor.db
clauditor serve      # open the dashboard
```

`serve` runs an ingest pass first by default (`ingest_on_serve = true`), so in
practice `clauditor serve` alone is enough after the first ingest. It prints the
local URL, binds to `127.0.0.1` only, and auto-opens your browser at
**`http://localhost:4747`** (the port comes from the `port` config key, default
`4747`). Press Ctrl-C to stop.

### Without any history (demo data)

To see a fully populated dashboard with no real `~/.claude` history, seed
synthetic demo data first:

```bash
python seed_demo.py     # fills data/clauditor.db with synthetic events
clauditor serve         # open the populated dashboard
```

`seed_demo.py` accepts `--db PATH` (default `data/clauditor.db`) and
`--pricing PATH` (default project-root `pricing.json`), so you can target a
throwaway database:

```bash
python seed_demo.py --db /tmp/demo.db
clauditor serve --db /tmp/demo.db
clauditor suggest --db /tmp/demo.db
```

The demo dataset spans several projects, models, and sources over the last ~30
days, with enough cache traffic to make the cache-efficiency metric meaningful
and with patterns that make all three savings rules fire. It is re-runnable
(each row has a stable `event_uid`, so a second run is deduped, not
double-counted).

---

## Configuration

Settings live in `config.json` at the project root. Any key you omit falls back
to the in-code defaults (your config is **deep-merged** onto them), so a minimal
config is fine. Validation runs on load — a malformed config prints a clear error
and exits non-zero. You can also point at a different file with `--config PATH`.

### Config keys and defaults

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | `4747` | Port the dashboard serves on (1–65535). |
| `claude_code_path` | `null` | Override the Claude Code session-log location; `null` uses the default `~/.claude/projects` (read-only). |
| `currency` | `"USD"` | Currency label shown in reports. |
| `lookback_days` | `30` | Window for savings suggestions and the default dashboard range (positive integer). |
| `ingest_on_serve` | `true` | Run an ingest pass before serving. |
| `budgets.global` | `{ "daily": null, "weekly": null, "monthly": 200 }` | Global budget per period; `null` = no limit. |
| `budgets.projects` | `{}` | Per-project budgets (empty by default — no phantom project). |
| `alert_fractions` | `[0.8, 1.0]` | Budget fractions at which alerts fire (80%, 100%). |
| `alert_webhook_url` | `null` | Optional URL to POST when an alert fires; `null` disables. |
| `desktop_notifications` | `true` | When true and `plyer` is installed, raise a desktop notification on a crossing. |
| `admin_api.enabled` | `false` | Enable the optional org-wide Admin Usage/Cost collector. |
| `admin_api.key_env` | `"ANTHROPIC_ADMIN_KEY"` | Env var name to read the admin key from. |
| `admin_api.key` | `null` | Inline admin key (prefer the env var). |
| `admin_api.base_url` | `null` | Endpoint override for a gateway/proxy. |

The shipped `config.json`:

```json
{
  "port": 4747,
  "claude_code_path": null,
  "currency": "USD",
  "lookback_days": 30,
  "ingest_on_serve": true,
  "budgets": {
    "global":  { "daily": null, "weekly": null, "monthly": 200 },
    "projects": {}
  },
  "alert_fractions": [0.8, 1.0],
  "alert_webhook_url": null,
  "desktop_notifications": true,
  "admin_api": { "enabled": false, "key_env": "ANTHROPIC_ADMIN_KEY" }
}
```

The shipped `config.json` lists only `enabled` and `key_env` under `admin_api`.
The in-code defaults (`DEFAULT_CONFIG`) additionally define `admin_api.key` and
`admin_api.base_url`, both `null`, which are deep-merged in at load — so the
effective config always includes them (as the keys table above shows), and
`config.example.json` spells them out explicitly:

```json
"admin_api": { "enabled": false, "key_env": "ANTHROPIC_ADMIN_KEY", "key": null, "base_url": null }
```

### Setting budgets

`budgets.global` and each `budgets.projects.<name>` take `daily`, `weekly`, and
`monthly` keys; a value of **`null` means no limit** for that period. The
`projects` map is empty by default, so a per-project budget only appears once you
add it. The per-project shape ships in `config.example.json` — copy it to
`config.json` and edit:

```json
"budgets": {
  "global":  { "daily": null, "weekly": null, "monthly": 200 },
  "projects": {
    "my-app":         { "monthly": 50 },
    "nightly-report": { "daily": 5, "monthly": 100 }
  }
}
```

### Pointing at your Claude Code logs

Set `claude_code_path` to override where clauditor looks for your Claude Code
session logs. Leave it `null` to use the default, `~/.claude/projects` — clauditor
reads `~/.claude/projects/<project-dir>/<session>.jsonl` (globbing `*/*.jsonl`)
**read-only**. If set, the override may point either at the `~/.claude` root or
directly at a `projects` directory.

### Admin API (optional, off by default)

`admin_api` is the only collector that polls Anthropic over the network, and only
when you opt in. With `admin_api.enabled = false` (the default) it is completely
inert: it reads no key, imports no HTTP client, and makes **no network call**. To
enable org-wide rollups, set `enabled: true` and supply an admin key either via
the environment variable named by `key_env` (default `ANTHROPIC_ADMIN_KEY`,
preferred) or inline via `key`. With `enabled: true` but no resolvable key, it
stays a silent no-op. `base_url` overrides the endpoint for a gateway/proxy.

---

## Troubleshooting

**No data showing on the dashboard.**
You have no usage yet (e.g. no Claude Code history, or ingest never ran). By
default clauditor reads session logs from `~/.claude/projects` (the
`~/.claude/projects/<project-dir>/<session>.jsonl` files); verify that directory
exists and contains `.jsonl` files, or set `claude_code_path` if your logs live
elsewhere. The read endpoints return well-formed zeros for a missing/empty DB
rather than erroring. Run `clauditor ingest`, or seed demo data with
`python seed_demo.py` and reload.

**Port already in use.**
The server binds the `port` config key (default `4747`). Change `port` in
`config.json` to a free port and re-run `clauditor serve`. The host is always
`127.0.0.1` and cannot be changed.

**Pricing badge is amber/stale.**
The header badge turns amber when `pricing.json`'s `updated` date is unknown or
**more than 30 days old**. Edit `pricing.json`, refresh your rates, and bump the
`updated` date. See the User Guide's "Updating `pricing.json`" section.

**A model is flagged as unknown.**
Any model not present in `pricing.json`'s `models` map is still recorded and
priced via `fallback_model` (the row is flagged `raw_meta.unknown_model = true`,
not dropped). Add the model and its `input`/`output` rates to `models` to price
it correctly.

**Admin API isn't pulling org-wide data.**
The `admin_api` collector is **disabled by default** (`admin_api.enabled =
false`) and is a complete no-op until enabled. Set `enabled: true` and provide a
key via `ANTHROPIC_ADMIN_KEY` (or the `key_env` you configured, or inline `key`).
With `enabled: true` but no resolvable key it stays a silent no-op.

**Desktop notifications aren't firing.**
Desktop notifications require the optional `plyer` extra. Install it with
`pip install -e ".[notifications]"` and ensure `desktop_notifications` is `true`.
Without `plyer`, alerts degrade gracefully to the dashboard gauges and the
optional webhook — notification delivery is best-effort and never blocks ingest.

**Want to start over.**
`clauditor reset` wipes only the database (the `--db` path, default
`data/clauditor.db`) after you type `reset` to confirm (or pass `--yes`/`--force`
to skip the prompt). It never touches `~/.claude`, `config.json`, or
`pricing.json`. The next `ingest`/`serve` recreates an empty database.
