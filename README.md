# IBKR execution service

The only component allowed to trade the IBKR account. A separate strategy bot POSTs
buy/sell *intents*; this service risk-checks each one against hard limits in
`config.yaml`, sizes the position, places a bracket order (parent limit buy + GTC stop
and target) through IB Gateway, reconciles against the broker every minute, flattens
before the close and alerts on Slack. Paper account first. Full requirements are in
[`requirements.md`](requirements.md).

```
strategy bot ──HMAC──▶ execution service ──socket API──▶ IB Gateway (same container)
                           │       │
                       Postgres   Slack
```

## Layout

| Path | What |
| --- | --- |
| `ibkr_exec/risk/engine.py` | `check(signal, state, config, now) -> Decision`: every rule, pure, no I/O |
| `ibkr_exec/broker/base.py` | `Broker` protocol; `fake.py` for tests, `ib.py` the only module importing `ib_async` |
| `ibkr_exec/orders/` | bracket placement/idempotency/partial fills (`bracket.py`), flatten and protective stops (`flatten.py`) |
| `ibkr_exec/jobs/` | reconcile, scheduler (keepalive, stale entries, flatten, end-of-day, pre-open) |
| `ibkr_exec/service.py` | composition root: startup gate, halt state, signal flow, jobs |
| `ibkr_exec/api/` | FastAPI routes, HMAC auth, rate limit |
| `ibkr_exec/db/` | SQLAlchemy models and the repository; `alembic/` migrations |
| `deploy/` | Dockerfile (IB Gateway + IBC + service) and `start.sh` |
| `scripts/setup-wizard.sh` | interactive wizard for the IBKR user, paper account, Slack, Render |
| `scripts/smoke_ib.py` | read-only connection test against a running gateway |

## Run locally

```bash
uv sync                                  # Python 3.12, all deps
uv run ruff check . && uv run pytest     # 90+ tests, fake broker, fixed clock, SQLite

docker compose up -d                     # Postgres on localhost:55432
cp .env.example .env                     # or run scripts/setup-wizard.sh
export $(grep -v '^#' .env | xargs)
export DATABASE_URL=postgresql+asyncpg://ibkr:ibkr@localhost:55432/ibkr_exec
uv run alembic upgrade head

BROKER=fake uv run python -m ibkr_exec.main          # in-memory broker on :8000
curl -s localhost:8000/health
uv run python scripts/sign.py bot POST /signals '{"signal_id":"demo-1","symbol":"TSLA","side":"buy","entry":{"type":"limit","price":364.2},"stop":358.32,"target":370.04,"risk_usd":50,"expires_at":"2026-10-23T10:20:00-04:00","strategy":"orb-v1"}'
```

Against the paper gateway (IB Gateway logged in to the paper user, API on 4002,
"Allow connections from localhost only", read-only API off):

```bash
uv run python scripts/smoke_ib.py        # connects, prints DU account, a quote; places nothing
BROKER=ib uv run python -m ibkr_exec.main
```

Startup refuses to run when `mode` in `config.yaml` and the connected account disagree
(paper account ids start with `DU`). Live additionally needs `CONFIRM_LIVE=yes`.

## API

Every request carries `X-Key-Id` (`bot` or `admin`), `X-Timestamp` (unix seconds) and
`X-Signature` = HMAC-SHA256 over `METHOD\nPATH\nTIMESTAMP\nsha256hex(body)`. Requests
older than 30 s, replays and bad signatures are rejected. `/halt`, `/resume` and
`/flatten` need the admin key.

| Endpoint | Purpose |
| --- | --- |
| `POST /signals` | entry or exit intent; `{decision, reasons, order_id?, qty?}` |
| `GET /signals/{id}` | decision, rule results and order status for one signal |
| `GET /positions` `GET /orders` `GET /account` | live from IBKR |
| `POST /flatten` | `{symbol?}` close one or all: cancel children, then marketable sell |
| `POST /halt` `POST /resume` | kill switch; resume is manual only |
| `GET /health` | ready, gateway connected, session age, last reconcile, halt, today's P&L |

`signal_id` is the IBKR `orderRef`; a repeated id returns the first decision and never
places a second order, even after a crash between placement and the database write.

## Deploy (Render, Virginia)

`render.yaml` defines one Docker Background Worker and a Postgres instance. The worker
image is based on `ghcr.io/gnzsnz/ib-gateway:stable` (IB Gateway + IBC) with the
service added; IBC settings come from environment variables (`READ_ONLY_API=no`,
`BYPASS_WARNING=yes`, `TWOFA_TIMEOUT_ACTION=restart`, `AUTO_RESTART_TIME`). The gateway
API stays on `127.0.0.1:4002` inside the container.

1. Run `scripts/setup-wizard.sh` once for the IBKR bot user, paper credentials, Slack
   webhook and Render resources.
2. Create the Blueprint from `render.yaml`; paste the secrets it marks `sync: false`.
3. Deploy outside market hours and approve the IB Key prompt in IBKR Mobile.
4. Paper acceptance checklist: `requirements.md`, "Testing and acceptance criteria".

Never set `CONFIRM_LIVE` on the paper deployment.
