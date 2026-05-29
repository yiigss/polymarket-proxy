#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP via GitHub Actions
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>

Uses the new Polymarket V2 SDK (polymarket-client) after py-clob-client was
archived in April 2026 due to CLOB v2 migration.
"""
import os, sys, json, traceback
from decimal import Decimal
import requests as req_lib

GAMMA_API = "https://gamma-api.polymarket.com"


def get_btc5m_market(candle_open_ms: int) -> dict:
    sec  = candle_open_ms // 1000
    slug = f"btc-updown-5m-{sec}"
    print(f"[Market] Looking up {slug}")
    r = req_lib.get(f"{GAMMA_API}/events?slug={slug}", timeout=15)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise RuntimeError(f"No market for slug {slug}")
    mkt = events[0]["markets"][0]

    outcomes  = json.loads(mkt.get("outcomes",  "[]"))
    token_ids = json.loads(mkt.get("clobTokenIds", "[]"))
    prices    = json.loads(mkt.get("outcomePrices", "[]"))
    neg_risk  = mkt.get("negRisk", False)

    up_idx   = next(i for i, o in enumerate(outcomes) if o.lower() == "up")
    down_idx = next(i for i, o in enumerate(outcomes) if o.lower() == "down")

    return {
        "slug":       slug,
        "up_token":   token_ids[up_idx],
        "down_token": token_ids[down_idx],
        "up_price":   float(prices[up_idx]),
        "down_price": float(prices[down_idx]),
        "neg_risk":   neg_risk,
    }


def main():
    if len(sys.argv) < 4:
        print("Usage: bet.py <up|down> <amount> <candle_time_ms>", file=sys.stderr)
        sys.exit(1)

    side_str, amount_str, candle_ms_str = sys.argv[1], sys.argv[2], sys.argv[3]
    amount    = float(amount_str)
    candle_ms = int(candle_ms_str)
    print(f"[Bet] side={side_str} amount=${amount} candle={candle_ms}")

    private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
    wallet      = os.environ["POLYMARKET_WALLET_ADDRESS"]

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    # Confirm Swiss IP
    ip_data = req_lib.get("https://ipinfo.io/json", timeout=10).json()
    print(f"[IP] {ip_data.get('ip')} — {ip_data.get('city')}, {ip_data.get('country')}")
    if ip_data.get("country") != "CH":
        raise RuntimeError(f"Not CH — got {ip_data.get('country')}")

    market   = get_btc5m_market(candle_ms)
    token_id = market["up_token"]  if side_str == "up"   else market["down_token"]
    price    = market["up_price"]  if side_str == "up"   else market["down_price"]
    eff_amt  = max(amount, 15)
    if eff_amt != amount:
        print(f"[Bet] Amount ${amount} < $15 minimum — using ${eff_amt}")
    print(f"[Market] {market['slug']} | price={price} | negRisk={market['neg_risk']}")

    # ── New V2 SDK (polymarket-client) ─────────────────────────────────────
    from polymarket import SecureClient
    from polymarket.errors import RequestRejectedError, UserInputError

    print(f"[SDK] Creating SecureClient for wallet={wallet[:10]}...")
    client = SecureClient.create(
        private_key=private_key,
        wallet=wallet,
    )

    print(f"[Order] Placing FAK BUY market order: token={token_id[:20]}... amount=${eff_amt}")
    try:
        response = client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=Decimal(str(eff_amt)),
            order_type="FAK",
        )
        print(f"[OK] Order placed! id={response.order_id} status={response.status}")
        sys.exit(0)
    except RequestRejectedError as e:
        print(f"[Error] RequestRejectedError: {e}")
        traceback.print_exc()
        sys.exit(1)
    except UserInputError as e:
        print(f"[Error] UserInputError: {e}")
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"[Error] {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
