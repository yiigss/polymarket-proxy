#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP via GitHub Actions
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>
"""
import os, sys, json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
import requests

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"


def get_btc5m_market(candle_open_ms: int) -> dict:
    sec  = candle_open_ms // 1000
    slug = f"btc-updown-5m-{sec}"
    print(f"[Market] Looking up {slug}")
    r = requests.get(f"{GAMMA_API}/events?slug={slug}", timeout=15)
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
        "slug":        slug,
        "up_token":    token_ids[up_idx],
        "down_token":  token_ids[down_idx],
        "up_price":    float(prices[up_idx]),
        "down_price":  float(prices[down_idx]),
        "neg_risk":    neg_risk,
    }


def main():
    if len(sys.argv) < 4:
        print("Usage: bet.py <up|down> <amount> <candle_time_ms>", file=sys.stderr)
        sys.exit(1)

    side_str, amount_str, candle_ms_str = sys.argv[1], sys.argv[2], sys.argv[3]
    amount       = float(amount_str)
    candle_ms    = int(candle_ms_str)
    print(f"[Bet] side={side_str} amount=${amount} candle={candle_ms}")

    api_key     = os.environ["POLYMARKET_API_KEY"]
    api_secret  = os.environ["POLYMARKET_SECRET"]
    passphrase  = os.environ["POLYMARKET_PASSPHRASE"]
    private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder      = os.environ["POLYMARKET_WALLET_ADDRESS"]

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    # Confirm Swiss IP
    ip_data = requests.get("https://ipinfo.io/json", timeout=10).json()
    print(f"[IP] {ip_data.get('ip')} — {ip_data.get('city')}, {ip_data.get('country')}")
    if ip_data.get("country") != "CH":
        raise RuntimeError(f"Not CH — got {ip_data.get('country')}")

    market   = get_btc5m_market(candle_ms)
    token_id = market["up_token"] if side_str == "up" else market["down_token"]
    price    = market["up_price"]   if side_str == "up"   else market["down_price"]
    eff_amt  = max(amount, 15)
    if eff_amt != amount:
        print(f"[Bet] Amount ${amount} below $15 minimum — using ${eff_amt}")

    print(f"[Market] {market['slug']} | negRisk={market['neg_risk']} | token={token_id[:12]}... | price={price}")

    creds  = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)

    # Try POLY_PROXY (2) then EOA (0) — POLY_PROXY is standard for Polymarket accounts
    for sig_type, label in [(2, "POLY_PROXY"), (0, "EOA")]:
        print(f"[Order] Trying signature_type={sig_type} ({label})")
        try:
            client = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=POLYGON,
                creds=creds,
                funder=funder,
                signature_type=sig_type,
            )
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=eff_amt,
                side="BUY",
            )
            signed = client.create_market_order(order_args)
            print(f"[Order] Signed order keys: {list(signed.__dict__.keys()) if hasattr(signed,'__dict__') else type(signed)}")
            resp = client.post_order(signed, OrderType.FOK)
            print(f"[Order] Response: {resp}")

            if isinstance(resp, dict) and resp.get("errorMsg"):
                print(f"[Warn] {label} failed: {resp['errorMsg']}")
                continue

            if isinstance(resp, dict) and (resp.get("success") or resp.get("orderID")):
                order_id = resp.get("orderID") or resp.get("order_id") or "?"
                print(f"[OK] Order placed via {label}! id={order_id} amount=${eff_amt} price={price}")
                print(f"::set-output name=order_id::{order_id}")
                sys.exit(0)

            print(f"[Warn] {label} unexpected response — trying next")
        except Exception as e:
            print(f"[{label}] exception: {e}")

    raise RuntimeError("All signature types exhausted — order not placed")


if __name__ == "__main__":
    main()
