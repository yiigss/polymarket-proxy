#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP via GitHub Actions
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>
"""
import os, sys, json, traceback
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
import requests as req_lib

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ─── patch HTTP helpers to log body + try version field ────────────────────
import py_clob_client.http_helpers.helpers as _hh

_original_request = _hh.request
_version_results: list = []   # filled by patched request; [(version, status, resp)]

def _patched_request(endpoint, method, headers=None, data=None):
    """On POST /order, log body and try version combos; return first success."""
    global _version_results
    if method == "POST" and endpoint.endswith("/order"):
        # Serialize once so we can see it
        if isinstance(data, dict):
            base_str = json.dumps(data, default=str)
        else:
            base_str = str(data) if data else "{}"
        print(f"[HTTP] POST {endpoint}")
        print(f"[Body0] {base_str[:600]}")

        # Try: no version, outer=2 (int), outer="2" (str)
        combos = [(None, None), (2, None), ("2", None), (None, 2), (None, "2"), (2, 2)]
        last_err = None
        for outer_v, inner_v in combos:
            body = json.loads(base_str)
            if outer_v is not None:
                body["version"] = outer_v
            if inner_v is not None:
                body.setdefault("order", {})
                body["order"]["version"] = inner_v
            body_str = json.dumps(body, separators=(",", ":"))
            print(f"[Try] outer={outer_v!r} inner={inner_v!r}")
            try:
                resp = _original_request(endpoint, method, headers, body_str)
                print(f"[Resp] OK: {str(resp)[:300]}")
                _version_results.append(("ok", outer_v, inner_v, resp))
                return resp
            except Exception as e:
                err_str = str(e)
                print(f"[Resp] Error: {err_str[:200]}")
                _version_results.append(("err", outer_v, inner_v, err_str))
                last_err = e
                if "version" not in err_str.lower() and "mismatch" not in err_str.lower():
                    # Non-version error — no point trying more versions
                    print("[Stop] Non-version error, halting version loop")
                    raise
        raise last_err
    return _original_request(endpoint, method, headers, data)

_hh.request = _patched_request
# ───────────────────────────────────────────────────────────────────────────


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

    api_key     = os.environ["POLYMARKET_API_KEY"]
    api_secret  = os.environ["POLYMARKET_SECRET"]
    passphrase  = os.environ["POLYMARKET_PASSPHRASE"]
    private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder      = os.environ["POLYMARKET_WALLET_ADDRESS"]

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

    # Clamp price to valid range (0.01–0.99)
    if not (0.01 <= price <= 0.99):
        print(f"[Warn] Price {price} out of range — clamping")
        price = max(0.05, min(0.95, price))

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)

    # SignatureType: EOA=0, POLY_PROXY=1
    for sig_type, sig_label in [(1, "POLY_PROXY"), (0, "EOA")]:
        print(f"\n[Order] sig_type={sig_type} ({sig_label})")
        try:
            client = ClobClient(
                CLOB_HOST, key=private_key, chain_id=POLYGON,
                creds=creds, funder=funder, signature_type=sig_type,
            )
            order_args = MarketOrderArgs(
                token_id=token_id, price=price, amount=eff_amt, side="BUY",
            )
            signed = client.create_market_order(order_args)
            print(f"[Order] Created market order, posting...")
            # post_order internally calls _patched_request which tries all version combos
            resp = client.post_order(signed, OrderType.FOK)
            # If we get here without exception, the post succeeded
            print(f"[OK] Post succeeded! resp={resp}")
            oid = (resp or {}).get("orderID") or (resp or {}).get("order", {}).get("id") or "?"
            print(f"[OK] Order placed via {sig_label}! id={oid} ${eff_amt}@{price}")
            sys.exit(0)

        except Exception as e:
            print(f"[{sig_label}] failed: {e}")
            traceback.print_exc()

    print("\n[Results summary]")
    for r in _version_results:
        print(" ", r)
    raise RuntimeError("All attempts exhausted — order not placed")


if __name__ == "__main__":
    main()
