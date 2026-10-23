"""Read-only smoke test against a running IB Gateway (paper, port 4002 by default).

    uv run python scripts/smoke_ib.py

Connects, prints the account id and balances, one live quote, open orders and positions.
Places nothing.
"""

from __future__ import annotations

import asyncio
import os
import sys

from ibkr_exec.broker.base import BrokerError
from ibkr_exec.broker.ib import IBBroker
from ibkr_exec.clock import SystemClock
from ibkr_exec.logging import setup_logging


async def main() -> int:
    setup_logging()
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", "4002"))
    symbol = os.environ.get("SMOKE_SYMBOL", "SPY")
    b = IBBroker(host, port, int(os.environ.get("IB_CLIENT_ID", "99")), SystemClock())
    await b.connect()
    try:
        acct = await b.account_state()
        kind = "PAPER" if acct.account_id.startswith("DU") else "LIVE"
        print(f"account       {acct.account_id} ({kind})")
        print(f"net liq       {acct.net_liquidation:,.2f}")
        print(f"available     {acct.available_funds:,.2f}")
        print(f"realized P&L  {acct.realized_pnl_today:,.2f}")
        print(f"server time   {await b.server_time()}")
        await asyncio.sleep(2)  # let the PnL subscription deliver
        try:
            q = await b.quote(symbol)
            kind = (
                "DELAYED (no live subscription; entries will be rejected)" if q.delayed else "live"
            )
            print(f"quote {q.symbol}  bid {q.bid} ask {q.ask} last {q.last} at {q.ts} {kind}")
        except BrokerError as exc:
            print(f"quote {symbol}  FAILED: {exc}")
        print(f"positions     {await b.positions()}")
        print(f"open orders   {len(await b.open_orders())}")
        return 0
    finally:
        await b.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
