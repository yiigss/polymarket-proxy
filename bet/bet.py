#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP via GitHub Actions
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>
"""
import os, sys, json, traceback, time, hmac, hashlib, base64
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.utilities import order_to_json
import requests as req_lib

CLOB_HOST = "https://clob.polymarket.com"
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


def l2_headers(api_key: str, api_secret: str, passphrase: str, funder: str,
               method: str, path: str, body: str = "") -> dict:
    ts  = str(int(time.time()))
    msg = ts + method + path + body
    raw = base64.b64decode(api_secret)
    sig = base64.b64encode(
        hmac.new(raw, msg.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    return {
        "POLY_ADDRESS":    funder,
        "POLY_SIGNATURE":  sig,
        "POLY_TIMESTAMP":  ts,
        "POLY_API_KEY":    api_key,
        "POLY_PASSPHRASE": passphrase,
        "Content-Type":    "application/json",
    }


def post_order_manual(api_key, api_secret, passphrase, funder,
                      order_dict: dict, signature: str,
                      order_type: str = "FOK",
                      outer_version=None, inner_version=None) -> tuple:
    """POST the signed order directly, optionally injecting version fields."""
    inner = dict(order_dict)
    if inner_version is not None:
        inner["version"] = inner_version

    body_dict = {
        "order":     inner,
        "signature": signature,
        "owner":     api_key,
        "orderType": order_type,
    }
    if outer_version is not None:
        body_dict["version"] = outer_version

    body_str = json.dumps(body_dict, separators=(",", ":"))
    hdrs = l2_headers(api_key, api_secret, passphrase, funder, "POST", "/order", body_str)

    label = f"outer={outer_version!r} inner={inner_version!r}"
    print(f"[POST] /order  {label}")
    print(f"[Body] {body_str[:600]}")

    resp = req_lib.post(f"{CLOB_HOST}/order", headers=hdrs, data=body_str, timeout=20)
    data = resp.json() if resp.content else {}
    print(f"[Resp] {resp.status_code} {json.dumps(data)}")
    return resp.status_code, data


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

    print(f"[Market] {market['slug']} | negRisk={market['neg_risk']} | price={price}")

    # Clamp price to valid range (market might have resolved partially)
    if not (0.01 <= price <= 0.99):
        print(f"[Warn] Price {price} outside valid range — clamping to 0.05/0.95")
        price = max(0.05, min(0.95, price))

    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)

    for sig_type, sig_label in [(1, "POLY_PROXY"), (0, "EOA")]:
        print(f"\n[Order] sig_type={sig_type} ({sig_label})")
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
                price=price,
                amount=eff_amt,
                side="BUY",
            )
            signed = client.create_market_order(order_args)

            # Use the library's own serializer — avoids Uint/enum conversion issues
            canonical_body_str = order_to_json(signed, api_key, OrderType.FOK)
            canonical = json.loads(canonical_body_str)
            order_dict = canonical["order"]
            signature  = canonical["signature"]
            print(f"[Order] makerAmount={order_dict.get('makerAmount')} takerAmount={order_dict.get('takerAmount')} signatureType={order_dict.get('signatureType')}")

            # Try all combinations of version placement that could fix order_version_mismatch:
            # (outer_version, inner_version)
            version_combos = [
                (None,  None),   # original format — no version anywhere
                (2,     None),   # outer body integer
                ("2",   None),   # outer body string
                (None,  2),      # inner order integer
                (None,  "2"),    # inner order string
                (2,     2),      # both integer
            ]
            for outer_v, inner_v in version_combos:
                status, resp = post_order_manual(
                    api_key, api_secret, passphrase, funder,
                    order_dict, signature,
                    outer_version=outer_v, inner_version=inner_v,
                )
                err = resp.get("error", "")
                if status == 200 and (resp.get("success") or resp.get("orderID") or resp.get("order")):
                    oid = resp.get("orderID") or (resp.get("order") or {}).get("id") or "?"
                    print(f"[OK] PLACED! {sig_label} outer={outer_v!r} inner={inner_v!r} id={oid} ${eff_amt}@{price}")
                    sys.exit(0)
                if err and "version" not in str(err):
                    print(f"[Skip] Non-version error '{err}' — stop trying versions for this sig_type")
                    break

        except Exception as e:
            print(f"[{sig_label}] create_order exception: {e}")
            traceback.print_exc()

    raise RuntimeError("All attempts exhausted — order not placed")


if __name__ == "__main__":
    main()
