# IBKR Execution Service — Requirements

Oct 23, 2026 · @Cesar

Build a small server that is the only component allowed to trade a Brazilian-resident IBKR account: it receives buy/sell intents from a separate strategy bot, enforces risk limits, places protected orders through IB Gateway, and keeps an audit trail. Paper account first; live only after weeks of matching results.

## Context

The account and its limits are already set; the service must work within them, not change them.

| Item | Decision |
| --- | --- |
| Broker | Interactive Brokers, IBKR Pro (required for API access) |
| Owner | Brazilian tax resident (Alpaca does not accept Brazil) |
| Account type | No Borrow Margin: own cash only, no shorting, no borrowing |
| Trading permissions | Stocks only (US stocks and ETFs); options and prediction markets off |
| Stock Yield Enhancement | Not enrolled |
| Bot login | A dedicated second username with trading rights only; no funding, withdrawal, transfer, account-config or trading-permission rights |
| Environment | IBKR paper account first; live after results match the backtest |
| Strategy | Intraday, long only, flat by the close (opening-range breakout style). Strategy logic lives in a separate bot, not in this service. |

The design comes from an audit of the day-trader-skill pack: its sizing and risk logic (`trader-budget/size.py`, `trader-risk/check.py`) is worth porting, but its safety depended on a human typing "yes". This service replaces that human with enforced code.

## Architecture

Three processes, one job each; only the execution service holds broker access, and it trusts no signal.

1. **Strategy bot**: scans, decides, and POSTs signals. Never talks to IBKR. Can be a scheduled script, the day-trader pack's scanner, or Claude reviewing news.
2. **Execution service** (this project): validates and risk-checks each signal, sizes the position, places a bracket order, tracks fills, reconciles with the broker, runs the flatten and kill switch, alerts.
3. **IB Gateway**: IBKR's login process, on the same host as the service. Its API port listens on localhost only.
4. **Postgres** (Supabase is fine): signals, decisions, orders, fills, daily P&L. The audit trail and the backtest-vs-live comparison data.
5. **Alert channel**: Slack incoming webhook to a private channel, for anything a human must see.

Stack: Python 3.12+, FastAPI, `ib_async` over the IB Gateway socket API (IBC handles the gateway's login and restarts), SQLAlchemy or asyncpg, APScheduler for jobs. Isolate IBKR behind a `Broker` interface so the risk engine and tests never import `ib_async`.

## Signal API

Signals are intents, not orders: the bot says what it wants; the service decides quantity and protection.

```json
POST /signals
{
  "signal_id": "orb-TSLA-2026-10-23-1015",
  "symbol": "TSLA",
  "side": "buy",
  "entry": {"type": "limit", "price": 364.20},
  "stop": 358.32,
  "target": 370.04,
  "risk_usd": 50,
  "expires_at": "2026-10-23T10:20:00-04:00",
  "strategy": "orb-v1",
  "reason": "15m opening-range breakout, close 364.18 > 362.18"
}
```

- `signal_id` is required and unique; it becomes the IBKR `orderRef`. A repeated `signal_id` returns the original decision and never places a second order.
- `side` is `buy` (open) or `sell` (close an existing position). A sell larger than the broker-reported position is rejected.
- `expires_at`: signals received after it are rejected as stale.
- Quantity = floor(`risk_usd` / (entry − stop)), then capped by the risk engine. `risk_usd` is a request, never above the configured max.
- Response: `{decision: "accepted" | "rejected", reasons: [...], order_id?, qty?}`. Every signal and decision is stored, accepted or not.

| Endpoint | Purpose |
| --- | --- |
| `POST /signals` | Submit an entry or exit intent |
| `GET /signals/{id}` | Decision and order status for one signal |
| `GET /positions` | Positions, read live from IBKR |
| `GET /orders` | Open and today's orders, from IBKR |
| `GET /account` | Cash, settled cash, buying power, today's realized and unrealized P&L |
| `POST /flatten` | Close one symbol or all: cancel open child orders, then market-sell |
| `POST /halt` | Kill switch on: reject new entries, still allow exits and flatten |
| `POST /resume` | Kill switch off (manual only; never automatic) |
| `GET /health` | Gateway connected, session age, last reconcile time, halt state, today's P&L |

## Risk engine

A pure function `check(signal, account_state, config, now) -> Decision`, ported from `trader-risk/check.py` and run on broker-reported state, never on a local file. Entries must pass every rule; exits (sells that only reduce a held position) are checked for format and quantity only, so getting out is always possible.

| Rule | Default | Blocks an entry when |
| --- | --- | --- |
| Kill switch | off | halt is on (manual or tripped) |
| Daily realized loss | $150 | today's realized loss reaches it; also trips the halt |
| Max loss per trade | $50 | qty × (entry − stop) exceeds it |
| Max order value | $4,000 | qty × entry exceeds it |
| Max position | 75% of equity | the position would exceed it |
| Cash buffer | 10% | the buy would leave settled + available cash under it (no margin exists) |
| Max trades per day | 3 | that many entries already placed today |
| Max open positions | 2 | that many positions already open |
| Entry window | 09:45–15:30 New York | outside it, on weekends, NYSE holidays, or early-close days after 12:30 |
| Stop required | on | no stop, or stop not below entry |
| Min reward:risk | 1.0 | (target − entry) / (entry − stop) under it |
| Price sanity | 1% | entry is more than 1% from the latest quote, or the quote is over 15 s old |
| Duplicate | on | same symbol already open or entered today |
| Allowed symbols | US stocks and ETFs | not on the allow list, or matches the deny list |
| Signal freshness | expires\_at | received after `expires_at` |
| PDT guard | on while equity < $25,000 | 3 day trades already in the last 5 business days (until IBKR confirms the rule no longer applies to this account) |

- All limits live in a config file loaded at start; the API cannot change them.
- Risk is measured from the higher of the signal's entry and the live ask, so a stale signal cannot understate it.
- Count pending (submitted, not yet filled) entries against trade and position limits.
- Log every rule result with the decision, so a rejection is explainable from the database alone.

## Order execution

Every entry goes to IBKR as one bracket, so a stop sits at the broker from the moment the entry can fill.

- **Bracket**: parent limit (or marketable limit) buy, regular hours only; two children in one OCA group: stop-loss at `stop` and take-profit limit at `target`. With `ib_async`, `ib.bracketOrder()` builds it; parent and first child `transmit=False`, last child `transmit=True`.
- **Children are GTC**, so the stop does not expire at the close if the flatten fails. The parent stays DAY.
- **Idempotency**: `orderRef = signal_id`. Before placing, look up open and today's orders with that ref; if one exists, return it instead of placing again. Retries after a timeout must go through this check.
- **Confirmation prompts**: IBKR can hold an order behind a precautionary warning. Configure IB Gateway's API precaution settings for API orders, and log every warning received through the `ib_async` error event. A warning type not on an explicit allow list means the order is cancelled and an alert is sent; never auto-accept it.
- **Partial fills**: resize child quantities to the filled quantity; cancel the unfilled parent remainder at the entry window end.
- **Rejected or unfilled parent**: cancel after a configurable timeout (default 5 min); record why.
- **Flatten** (`POST /flatten` and the 15:55 job): for each position, cancel its open children, wait for the cancel confirmation, then send a marketable sell for the broker-reported quantity. If the sell fails, re-place a protective stop and alert. Never leave a position without a working stop.
- **Exits from the bot**: a `sell` signal runs the same flatten path for that symbol.
- **Never** places options, short sales, outside-RTH orders, or orders for symbols outside the allow list, even if a signal asks.

## State and reconciliation

IBKR is the source of truth for cash, positions and orders; the database records intent and history and is corrected to match the broker, never the reverse.

| Table | Holds |
| --- | --- |
| `signals` | Every signal received: payload, received\_at, decision, reasons, rule results |
| `orders` | One row per broker order: signal\_id, ib order id, perm id, role (parent, stop, target, flatten), status, qty, prices, timestamps |
| `fills` | Executions: order id, exec id (unique), qty, price, commission, time |
| `positions_snapshot` | Broker positions at each reconcile, for drift detection |
| `daily_pnl` | Per New York date: realized P&L, commissions, trades, halt tripped yes/no |
| `events` | Session up/down, warnings, rejections, halts, alerts sent |

- Reconcile every 60 s during market hours and on every reconnect: positions, open orders, executions since last run.
- Any drift (a position or order the database does not know, or a missing stop for an open position) triggers an alert; a position without a working stop gets a protective stop placed immediately.
- On startup, reconcile before accepting any signal; `/health` reports not ready until then.
- Store all times in UTC; evaluate trading rules in America/New\_York with a real time zone library (not hand-rolled DST).

## Background jobs and alerting

The service must be safe when nobody is watching, so every failure ends in either a protective action or an alert.

| Job | When (New York) | Does |
| --- | --- | --- |
| Session keepalive | Every 60 s | Check `ib.isConnected()` and request the server time; reconnect with backoff |
| Reconcile | Every 60 s, 09:25–16:05 | See State and reconciliation |
| Stale-entry cleanup | Every minute | Cancel unfilled parents past their timeout |
| Flatten | 15:55 | Close every position (Order execution) |
| End-of-day check | 16:10 | Confirm zero positions and zero open orders; write `daily_pnl`; send a summary |
| Pre-open check | 09:15 | Gateway logged in, paper/live matches config, market data live, limits loaded |

Alert immediately on: gateway session lost for over 2 minutes, re-login needed (two-factor), order rejected, warning prompt not on the allow list, halt tripped, reconcile drift, position without a stop, flatten failure, positions remaining after 16:00. Send a daily summary after the close. Alerts must not depend on the gateway being up.

## Security

Anyone who can reach `/signals` can trade the account, so the endpoint is authenticated, private and narrow.

- Every request carries an HMAC-SHA256 signature over method, path, timestamp and body, with a shared secret; reject if the timestamp is over 30 s old or the signature is wrong. Separate secrets for the bot and for admin actions (`/halt`, `/resume`, `/flatten`).
- Bind the API to a private network or localhost; no public port. If remote access is needed, put it behind a VPN or Tailscale, not the open internet.
- IB Gateway's API port listens on 127.0.0.1 only, with "trusted IPs" set to localhost.
- Credentials (IBKR bot username, password, HMAC secrets, database URL) come from environment variables or a secrets manager; never logged, never committed, never in signal payloads.
- The bot's IBKR username has trading rights only. Add an IBKR IP restriction for it once the server has a fixed IP.
- Rate-limit `/signals` (e.g. 10 per minute) and respect IBKR's API pacing limits (messages per second and historical-data request throttling); back off on pacing errors instead of retrying in a loop.
- Never auto-resume after a halt; `/resume` requires the admin secret.

## Deployment and configuration

Host on Render in the Virginia (US East) region, the closest Render region to the New York exchanges and IBKR's US servers, with the service and IB Gateway in one container so the gateway port never leaves localhost.

- One Docker **Background Worker** on Render running IB Gateway + IBC (automated login, daily restart) and this service. Base it on a maintained IB Gateway Docker image. Use a 2 GB instance or larger: the gateway is a Java process. Postgres in the same region (Render Postgres or Supabase us-east).
- If the strategy bot also runs on Render, it reaches the service over Render's private network; no public URL.
- Deploy outside market hours: every deploy restarts the gateway and may need a two-factor approval in IBKR Mobile.
- IB Gateway default API ports: **4002 paper**, **4001 live** (TWS desktop: 7497 / 7496).
- One brokerage session per username: the bot uses its own username so logging into the IBKR app does not drop it. Expect periodic two-factor re-approval in IBKR Mobile; the service alerts on Slack and waits, it never retries logins in a loop.
- IBKR's servers have a short daily reset (around 01:00 local time per region) and weekend maintenance; jobs must tolerate disconnects and reconnect on their own.
- Market data: paper starts on the free non-consolidated real-time quotes; for live, on the bot's username, non-professional: US Securities Snapshot & Futures Value Bundle (about $10/month, waived with about $30/month in commissions) plus US Equity and Options Add-On Streaming Bundle (about $4.50/month). The pre-open check fails if quotes are delayed.
- For an IBKR IP restriction on the bot user, use Render's outbound IP ranges for the Virginia region (shown in the service's settings).

Config (`config.yaml`, loaded once, read-only at runtime):

```yaml
mode: paper            # paper | live; must match the gateway's account type or startup fails
ib: {host: 127.0.0.1, port: 4002, client_id: 17}
risk:
  max_loss_per_trade_usd: 50
  max_daily_loss_usd: 150
  max_order_usd: 4000
  max_position_pct: 75
  cash_buffer_pct: 10      # of IBKR-reported AvailableFunds
  max_trades_per_day: 3
  max_open_positions: 2
  min_reward_risk: 1.0
  entry_window: ["09:45", "15:30"]
  flat_by: "15:55"
  price_tolerance_pct: 1.0
  entry_timeout_min: 5
  pdt_guard: true          # max 3 day trades per 5 business days under $25k equity
symbols: {allow: ["*"], deny: []}   # US stocks/ETFs only; options never
alerts:
  slack_webhook_url: ${SLACK_WEBHOOK_URL}   # secret, from env
```

- Startup refuses to run if `mode` and the connected account disagree (`IBKR paper account ids start with DU`).
- Live mode additionally requires an env flag `CONFIRM_LIVE=yes`, set by hand.

## Build order

Build in small, testable steps; nothing connects to IBKR until steps 1–4 pass their tests.

1. Repo skeleton: FastAPI app, `config.yaml` loader with validation, Postgres schema and migrations, structured logging, Dockerfile.
2. `Broker` interface and an in-memory fake broker (orders, fills, positions, a controllable clock and quotes).
3. Risk engine as a pure function, with the unit tests from the checklist below.
4. Signal API with HMAC auth, idempotency on `signal_id`, and decision storage; order builder (bracket) against the fake broker.
5. Background jobs (reconcile, flatten, end-of-day, pre-open) and Slack alerts, tested with the fake broker and a fixed clock.
6. `ib_async` implementation of `Broker`, run locally against IB Gateway on the **paper** account (port 4002).
7. Render deployment: one Docker Background Worker in Virginia with IB Gateway + IBC + the service; Postgres in the same region.
8. Paper acceptance run (checklist below). Live mode is out of scope for the agent: it stops and asks before anything touches a live account.

## Testing and acceptance criteria

The risk engine and order logic are tested without IBKR through a fake `Broker`; then the whole service runs against the paper account before any live money.

Unit and integration tests (fake broker, fixed clock):

- [ ] Every risk rule has a passing and a blocking case, including holidays, early close and DST changeover days
- [ ] Duplicate `signal_id` returns the first decision and places nothing
- [ ] Stale signal (`expires_at` passed) is rejected
- [ ] Exit signals pass while halted and while over daily limits
- [ ] Partial fill resizes the children
- [ ] Flatten cancels children before selling; a failed sell re-places a stop and alerts
- [ ] Reconcile detects an unknown position and a missing stop, and places the stop
- [ ] Startup fails when `mode` and the account type disagree
- [ ] HMAC: bad signature, old timestamp and replayed request are rejected

Paper acceptance (IBKR paper account, real market hours):

- [ ] A bracket entry fills and the stop and target appear at the broker
- [ ] Daily loss limit trips the halt; further entries are rejected; flatten still works
- [ ] 15:55 flatten leaves zero positions and zero open orders on 10 consecutive trading days
- [ ] Killing the service mid-trade, then restarting, reconciles and keeps the stop working
- [ ] Gateway disconnect produces an alert within 2 minutes
- [ ] Two weeks of paper results logged and compared with the backtest (fills, slippage, P&L)

## Out of scope and decisions

The strategy, the backtester and live funding are separate work; this service only executes safely.

Out of scope: signal generation, backtesting, a UI beyond `/health` and the daily summary, options, shorting, margin, crypto, multi-account support.

| Question | Decision | Why |
| --- | --- | --- |
| IBKR API | Socket API: IB Gateway + IBC + `ib_async` | IBC automates the gateway login and restarts inside a container; retail Web API login needs a browser session |
| Host | Render, Virginia (US East), Docker Background Worker, 2 GB+ instance | Closest Render region to NY/NJ exchanges; regions offered: Oregon, Ohio, Virginia, Frankfurt, Singapore |
| Market data | Paper: the free US Real-Time Non Consolidated Streaming Quotes. Before live: add the Value Bundle (about $10/mo, waivable) + Add-On Streaming Bundle (about $4.50/mo), non-professional, on the bot username, if the API returns delayed data or the free quotes drift from NBBO | API quotes need Level 1 streaming; subscriptions are per username |
| Day-trading limits | Treat the account as PDT-limited: `pdt_guard` on, at most 3 day trades per rolling 5 business days while equity is under $25,000 | FINRA removed the PDT rule on June 4, 2026, but brokers have until Oct 20, 2027 to switch, and IBKR still lists the $25,000 rule for margin-type accounts during the phase-out |
| Alerts | Slack incoming webhook to a private channel | Existing Slack account |
| Settled cash | Size from IBKR's reported AvailableFunds; no settled-only rule | No Borrow Margin allows trading with unsettled proceeds, and the broker's number is authoritative |

- [ ] Ask IBKR (Message Center) whether PDT limits apply to this No Borrow Margin account, and check the day trades left shown in the Client Portal; turn `pdt_guard` off only after they confirm
- [ ] Logged in to the Client Portal as the bot user: Settings > Market Data Subscriptions; confirm non-professional status, then subscribe to US Securities Snapshot and Futures Value Bundle (NP, L1) and US Equity and Options Add-On Streaming Bundle (NP), checking the prices shown there ([public price list](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php))

## Sources

- [IBKR Campus — Web API Trading](https://ibkrcampus.com/campus/ibkr-api-page/web-api-trading/) (authentication, one session per username, order reply messages, rate limits, maintenance windows)
- [IBKR — Paper trading account settings](https://www.interactivebrokers.com/docs/general/market-data-subscriptions/market-data-users/finding-your-paper-user-credentials/step-four-open-the-paper-trading-account-settings)
- [day-trader-skill repository](https://github.com/Jakeschincariol/day-trader-skill) (risk and sizing logic to port; audited)
- [Alpaca — country availability](https://alpaca.markets/support/countries-alpaca-is-available) (why not Alpaca)

