#!/usr/bin/env python3
"""
bet.py — place a Polymarket CLOB order from a Swiss IP
Usage: python3 bet.py <up|down> <amount_usdc> <candle_open_time_ms>

Uses the new Polymarket V2 SDK (polymarket-client) after py-clob-client was
archived in April 2026 due to CLOB v2 migration.
"""
import os, sys, json, traceback
from decimal import Decimal
import requests as req_lib

# Optional SOCKS5 routing — activated when SOCKS5_PROXY env var is set
# (format: user:pass@ip:port). Not used in GHA path — VPN handles routing at OS level.
_socks5 = os.environ.get("SOCKS5_PROXY")
if _socks5:
    try:
        import socks as _socks_lib, socket as _socket_lib
        _auth, _addr = _socks5.rsplit("@", 1)
        _user, _pass = _auth.split(":", 1)
        _ip,   _port = _addr.rsplit(":", 1)
        _socks_lib.set_default_proxy(_socks_lib.SOCKS5, _ip, int(_port), username=_user, password=_pass)
        _socket_lib.socket = _socks_lib.socksocket
        print(f"[SOCKS5] Patched all sockets -> {_ip}:{_port}", flush=True)
    except Exception as _e:
        print(f"[SOCKS5] Setup failed: {_e}", flush=True)

GAMMA_API = "https://gamma-api.polymarket.com"

# ── On-chain USDC balance (native USDC on Polygon) ─────────────────────────
# Used to report actual wallet balance back to the server — bypasses CLOB API entirely.
NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
]

def get_onchain_usdc(wallet: str) -> float | None:
    """Query native USDC balance on Polygon via public RPC (no auth required)."""
    padded = wallet.replace("0x", "").lower().zfill(64)
    data   = "0x70a08231" + padded  # balanceOf(address)
    for rpc in POLYGON_RPCS:
        try:
            r = req_lib.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": NATIVE_USDC, "data": data}, "latest"],
            }, timeout=8)
            result = r.json().get("result", "0x")
            if result and result != "0x":
                return int(result, 16) / 1_000_000
            return 0.0
        except Exception as e:
            print(f"[Balance] RPC {rpc} failed: {e}", flush=True)
    return None


CLOB_BASE = "https://clob.polymarket.com"

def get_clob_balance(client) -> float | None:
    """Fetch deposited CLOB balance (the real Polymarket account balance) via SecureClient.
    This is the $266 figure — funds deposited into the exchange, not raw wallet USDC."""
    # 1. Try named SDK methods
    for method_name in ("get_balance", "balance", "get_usdc_balance", "usdc_balance", "get_collateral_balance"):
        method = getattr(client, method_name, None)
        if callable(method):
            try:
                result = method()
                if isinstance(result, (int, float)):
                    return float(result)
                if isinstance(result, dict):
                    for key in ("balance", "usdc", "amount", "collateral"):
                        if key in result:
                            return float(result[key])
                if hasattr(result, "balance"):
                    return float(result.balance)
            except Exception as e:
                print(f"[Balance] SDK {method_name}() failed: {e}", flush=True)

    # 2. Try the client's internal authenticated HTTP session
    for attr in ("_session", "session", "_http", "_client", "http"):
        sess = getattr(client, attr, None)
        if sess is None:
            continue
        for path in ("/balance", "/v2/balance", "/api/balance"):
            try:
                r = sess.get(f"{CLOB_BASE}{path}", timeout=10)
                if r.ok:
                    data = r.json()
                    for key in ("balance", "usdc", "amount", "collateral"):
                        if key in data:
                            print(f"[Balance] CLOB {path} → {key}={data[key]}", flush=True)
                            return float(data[key])
            except Exception as e:
                print(f"[Balance] Client {attr}.get({path}) failed: {e}", flush=True)

    return None


def report_balance(wallet: str, report_url: str, signal_secret: str, client=None) -> None:
    """Fetch CLOB deposited balance via authenticated client and POST to server."""
    if client is None:
        print("[Balance] No client — skipping report", flush=True)
        return

    balance = get_clob_balance(client)
    if balance is None:
        print("[Balance] Could not fetch CLOB balance — skipping report", flush=True)
        return

    source = "gha_clob"
    print(f"[Balance] CLOB deposited: ${balance:.4f}", flush=True)

    try:
        r = req_lib.post(
            report_url,
            json={"balance": balance, "source": source},
            headers={"X-Signal-Secret": signal_secret, "Content-Type": "application/json"},
            timeout=8,
        )
        if r.ok:
            print(f"[Balance] Reported ${balance:.4f} ({source}) to server ✓", flush=True)
        else:
            print(f"[Balance] Report failed: {r.status_code} {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"[Balance] Report POST failed: {e}", flush=True)


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
    outcomes  = json.loads(mkt.get("outcomes",      "[]"))
    token_ids = json.loads(mkt.get("clobTokenIds",  "[]"))
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

    private_key   = os.environ["POLYMARKET_PRIVATE_KEY"]
    wallet        = os.environ["POLYMARKET_WALLET_ADDRESS"]
    signal_secret = os.environ.get("SIGNAL_SECRET", "")
    # REPORT_URL: base URL for balance reporting (e.g. https://<domain>/api/btc/balance-report)
    # Injected by the workflow as: REPORT_URL=$(echo "$SIGNAL_URL" | sed 's|/bet-signal|/balance-report|')
    report_url    = os.environ.get("REPORT_URL", "")

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
    print(f"[Market] {market['slug']} | price={price} | negRisk={market['neg_risk']}")

    from polymarket import SecureClient
    from polymarket.errors import RequestRejectedError, UserInputError

    print(f"[SDK] Creating SecureClient for wallet={wallet[:10]}...")
    client = SecureClient.create(private_key=private_key, wallet=wallet)

    print(f"[Order] Placing FAK BUY: token={token_id[:20]}... amount=${amount}")
    import time as _time
    MAX_ATTEMPTS = 5
    RETRY_DELAY  = 5  # seconds between retries
    NO_MATCH_MSG = "no orders found to match"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.place_market_order(
                token_id=token_id,
                side="BUY",
                amount=Decimal(str(amount)),
                order_type="FAK",
            )
            print(f"[OK] Order placed! attempt={attempt} id={response.order_id} status={response.status}")
            # Report updated balance after successful bet (pass client so we get CLOB deposited balance)
            if report_url:
                report_balance(wallet, report_url, signal_secret, client=client)
            sys.exit(0)
        except RequestRejectedError as e:
            msg = str(e).lower()
            if NO_MATCH_MSG in msg and attempt < MAX_ATTEMPTS:
                print(f"[Retry {attempt}/{MAX_ATTEMPTS}] No liquidity yet — waiting {RETRY_DELAY}s...")
                _time.sleep(RETRY_DELAY)
                continue
            print(f"[Error] RequestRejectedError (attempt {attempt}): {e}")
            traceback.print_exc()
            sys.exit(1)
        except UserInputError as e:
            print(f"[Error] UserInputError: {e}"); traceback.print_exc(); sys.exit(1)
        except Exception as e:
            print(f"[Error] {type(e).__name__}: {e}"); traceback.print_exc(); sys.exit(1)


if __name__ == "__main__":
    main()
