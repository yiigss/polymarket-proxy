#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP via GitHub Actions
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>
"""
import os, sys, json, traceback, time, hmac, hashlib, base64
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
import requests as req_lib

CLOB_HOST  = "https://clob.polymarket.com"
GAMMA_API  = "https://gamma-api.polymarket.com"


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
        "slug":        slug,
        "up_token":    token_ids[up_idx],
        "down_token":  token_ids[down_idx],
        "up_price":    float(prices[up_idx]),
        "down_price":  float(prices[down_idx]),
        "neg_risk":    neg_risk,
    }


def l2_headers(api_key: str, api_secret: str, passphrase: str, funder: str,
               method: str, path: str, body: str = "") -> dict:
    ts   = str(int(time.time()))
    msg  = ts + method + path + body
    raw_secret = base64.b64decode(api_secret)
    sig  = base64.b64encode(
        hmac.new(raw_secret, msg.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "POLY_ADDRESS":    funder,
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_API_KEY":    api_key,
        "POLY_PASSPHRASE": passphrase,
        "Content-Type":    "application/json",
    }


def post_order_direct(api_key, api_secret, passphrase, funder,
                      order_dict: dict, signature: str, order_type: str = "FOK",
                      version: str = None) -> dict:
    body_dict = {
        "order":     order_dict,
        "signature": signature,
        "owner":     api_key,
        "orderType": order_type,
    }
    if version is not None:
        body_dict["version"] = version

    body_str = json.dumps(body_dict, separators=(",", ":"))
    hdrs = l2_headers(api_key, api_secret, passphrase, funder, "POST", "/order", body_str)

    print(f"[HTTP] POST {CLOB_HOST}/order  version={version!r}")
    print(f"[Body] {body_str[:800]}")

    resp = req_lib.post(f"{CLOB_HOST}/order", headers=hdrs, data=body_str, timeout=20)
    print(f"[Resp] {resp.status_code} {resp.text[:500]}")
    return resp.status_code, resp.json() if resp.content else {}


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
    token_id = market["up_token"]   if side_str == "up"   else market["down_token"]
    price    = market["up_price"]   if side_str == "up"   else market["down_price"]
    eff_amt  = max(amount, 15)
    if eff_amt != amount:
        print(f"[Bet] Amount ${amount} below $15 minimum — using ${eff_amt}")

    print(f"[Market] {market['slug']} | negRisk={market['neg_risk']} | token={token_id[:12]}... | price={price}")

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)

    # SignatureType: EOA=0, POLY_PROXY=1, POLY_GNOSIS_SAFE=2
    for sig_type, label in [(1, "POLY_PROXY"), (0, "EOA")]:
        print(f"\n[Order] Trying sig_type={sig_type} ({label})")
        try:
            client = ClobClient(
                CLOB_HOST,
                key=private_key,
                chain_id=POLYGON,
                creds=creds,
                funder=funder,
                signature_type=sig_type,
            )
            # Pass price explicitly — avoids orderbook fetch (handles markets with no active book)
            order_args = MarketOrderArgs(
                token_id=token_id,
                price=price,
                amount=eff_amt,
                side="BUY",
            )
            signed = client.create_market_order(order_args)

            # Serialize order fields
            o = signed.order
            order_dict = {
                "salt":          str(o.salt),
                "maker":         o.maker,
                "signer":        o.signer,
                "taker":         o.taker,
                "tokenId":       str(o.tokenId),
                "makerAmount":   str(o.makerAmount),
                "takerAmount":   str(o.takerAmount),
                "expiration":    str(o.expiration),
                "nonce":         str(o.nonce),
                "feeRateBps":    str(o.feeRateBps),
                "side":          int(o.side),
                "signatureType": int(o.signatureType),
            }
            print(f"[Order] {json.dumps(order_dict)}")

            # Try without version, then version="1", then version="2"
            for ver in [None, "1", "2"]:
                status, resp = post_order_direct(
                    api_key, api_secret, passphrase, funder,
                    order_dict, signed.signature, version=ver
                )
                if status == 200 and (resp.get("success") or resp.get("orderID") or resp.get("order")):
                    order_id = resp.get("orderID") or (resp.get("order") or {}).get("id") or "?"
                    print(f"[OK] Order placed via {label} version={ver!r}! id={order_id} ${eff_amt} @ {price}")
                    sys.exit(0)
                if resp.get("error") and "version" not in str(resp.get("error", "")):
                    # Non-version error — stop trying versions
                    print(f"[Skip] Non-version error: {resp} — trying next sig_type")
                    break

        except Exception as e:
            print(f"[{label}] exception: {e}")
            traceback.print_exc()

    raise RuntimeError("All attempts exhausted — order not placed")


if __name__ == "__main__":
    main()
