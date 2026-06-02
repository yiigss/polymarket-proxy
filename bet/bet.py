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
def get_clob_balance_l1(private_key: str, wallet: str) -> float | None:
    """Fetch deposited CLOB balance using direct L1 auth (ECDSA, sign timestamp only)."""
    import time as _time
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError as e:
        print(f"[Balance] eth_account not available: {e}", flush=True)
        return None
    try:
        ts = str(int(_time.time()))
        # L1 auth: sign just the timestamp string (not ts+method+path)
        signed = Account.sign_message(encode_defunct(text=ts), private_key=private_key)
        sig = signed.signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        headers = {
            "POLY-ADDRESS":   wallet,
            "POLY-SIGNATURE": sig,
            "POLY-TIMESTAMP": ts,
            "POLY-NONCE":     "0",
        }
        for path in ("/balance", "/data/balance", "/credit-balance", "/cash-balance"):
            r = req_lib.get(f"{CLOB_BASE}{path}", headers=headers, timeout=10)
            print(f"[Balance] CLOB {path} HTTP {r.status_code}: {r.text[:120]}", flush=True)
            if r.ok:
                data = r.json()
                for key in ("balance", "usdc", "amount", "collateral", "USDC", "deposited"):
                    if key in data:
                        return float(data[key])
    except Exception as e:
        print(f"[Balance] L1 auth call failed: {e}", flush=True)
    return None
def get_clob_balance_sdk(client) -> float | None:
    """Fetch CLOB USDC balance via SecureClient SDK methods."""
    # 1. get_balance_allowance(asset_type='COLLATERAL') — USDC deposited balance
    #    Returns raw on-chain units (6 decimals for USDC on Polygon) — divide by 1e6.
    for asset_type in ("COLLATERAL", "CONDITIONAL"):
        try:
            result = client.get_balance_allowance(asset_type=asset_type)
            print(f"[Balance] get_balance_allowance({asset_type!r}) → {result}", flush=True)
            raw: float | None = None
            if isinstance(result, (int, float)):
                raw = float(result)
            elif isinstance(result, dict):
                for key in ("balance", "usdc", "amount", "collateral", "USDC", "deposited", "allowance"):
                    if key in result:
                        raw = float(result[key])
                        break
            else:
                for attr in ("balance", "allowance", "amount", "value"):
                    if hasattr(result, attr):
                        raw = float(getattr(result, attr))
                        break
            if raw is not None:
                # Values > 1000 are raw on-chain (6-decimal micro-USDC), convert to dollars
                return raw / 1e6 if raw > 1000 else raw
        except Exception as e:
            print(f"[Balance] get_balance_allowance({asset_type!r}) failed: {e}", flush=True)
    # 2. get_portfolio_values() — tuple of PortfolioValue(value=Decimal(...))
    try:
        result = client.get_portfolio_values()
        print(f"[Balance] get_portfolio_values() → {result}", flush=True)
        # result is a tuple — take the first element's .value
        if isinstance(result, (list, tuple)) and len(result) > 0:
            item = result[0]
            if hasattr(item, "value"):
                return float(item.value)
        if isinstance(result, (int, float)):
            return float(result)
        if isinstance(result, dict):
            for key in ("balance", "cash", "total", "usdc", "portfolio", "value"):
                if key in result:
                    return float(result[key])
    except Exception as e:
        print(f"[Balance] get_portfolio_values() failed: {e}", flush=True)
    return None
def report_balance(private_key: str, wallet: str, report_url: str, signal_secret: str, client=None) -> None:
    """Fetch CLOB deposited balance (try SDK attrs first, then direct L1 auth) and POST to server."""
    balance: float | None = None
    # 1. Try SDK introspection (prints dir so we can find the right method name)
    if client is not None:
        balance = get_clob_balance_sdk(client)
    # 2. Fall back to direct L1 ECDSA auth
    if balance is None:
        balance = get_clob_balance_l1(private_key, wallet)
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
    import time as _time
    LIMIT_WAIT_S  = 5    # seconds to wait for limit fill before falling back to FAK
    POLL_INTERVAL = 2    # seconds between get_order polls
    MIN_FAK_USDC  = 0.50 # don't bother FAK-ing for less than this amount
    FAK_MAX_TRIES = 5
    FAK_RETRY_S   = 5
    NO_MATCH_MSG  = "no orders found to match"
    def place_fak(fak_amount: float) -> None:
        """Place a FAK market order with retry on no-liquidity."""
        print(f"[Order] FAK BUY: token={token_id[:20]}... amount=${fak_amount:.2f}")
        for attempt in range(1, FAK_MAX_TRIES + 1):
            try:
                resp = client.place_market_order(
                    token_id=token_id,
                    side="BUY",
                    amount=Decimal(str(round(fak_amount, 2))),
                    order_type="FAK",
                )
                print(f"[OK] FAK placed attempt={attempt} id={resp.order_id} status={resp.status}")
                return
            except RequestRejectedError as e:
                if NO_MATCH_MSG in str(e).lower() and attempt < FAK_MAX_TRIES:
                    print(f"[Retry {attempt}/{FAK_MAX_TRIES}] No liquidity — waiting {FAK_RETRY_S}s...")
                    _time.sleep(FAK_RETRY_S)
                    continue
                print(f"[Error] FAK RequestRejectedError (attempt {attempt}): {e}")
                traceback.print_exc()
                sys.exit(1)
            except UserInputError as e:
                print(f"[Error] FAK UserInputError: {e}"); traceback.print_exc(); sys.exit(1)
            except Exception as e:
                print(f"[Error] FAK {type(e).__name__}: {e}"); traceback.print_exc(); sys.exit(1)
    # ── Step 1: GTC limit order at current market price ──────────────────────
    # BTC 5-min markets can have thin liquidity right at open; a limit order
    # gives market makers a chance to fill at the quoted price.
    # size (tokens) = USDC amount / price per token
    size = Decimal(str(amount)) / Decimal(str(price))
    print(f"[Order] GTC LIMIT BUY: size={size:.4f} tokens @ {price} (~${amount:.2f})")
    limit_order_id: str | None = None
    try:
        limit_resp = client.place_limit_order(
            token_id=token_id,
            side="BUY",
            size=size,
            price=Decimal(str(price)),
        )
        if not limit_resp.ok:
            print(f"[Order] Limit rejected ({limit_resp.code}): {limit_resp.message} — falling back to FAK")
            place_fak(amount)
            if report_url:
                report_balance(private_key, wallet, report_url, signal_secret, client=client)
            sys.exit(0)
        limit_order_id = limit_resp.order_id
        print(f"[Order] Limit accepted: id={limit_order_id} status={limit_resp.status}")
        if limit_resp.status == "matched":
            print(f"[OK] Limit fully matched immediately ✓")
            if report_url:
                report_balance(private_key, wallet, report_url, signal_secret, client=client)
            sys.exit(0)
    except UserInputError as e:
        print(f"[Order] Limit UserInputError: {e} — falling back to FAK")
        place_fak(amount)
        if report_url:
            report_balance(private_key, wallet, report_url, signal_secret, client=client)
        sys.exit(0)
    except Exception as e:
        print(f"[Order] Limit placement error: {type(e).__name__}: {e} — falling back to FAK")
        place_fak(amount)
        if report_url:
            report_balance(private_key, wallet, report_url, signal_secret, client=client)
        sys.exit(0)
    # ── Step 2: Poll for fill for up to LIMIT_WAIT_S ─────────────────────────
    size_matched = Decimal("0")
    deadline = _time.time() + LIMIT_WAIT_S
    while _time.time() < deadline:
        _time.sleep(POLL_INTERVAL)
        try:
            open_order = client.get_order(order_id=limit_order_id)
            size_matched = open_order.size_matched
            remaining   = open_order.original_size - size_matched
            elapsed     = LIMIT_WAIT_S - (deadline - _time.time())
            print(f"[Order] Poll @{elapsed:.0f}s: matched={size_matched}/{open_order.original_size} tokens remaining={remaining:.4f}")
            if remaining <= Decimal("0.001"):
                print(f"[OK] Limit fully filled via polling ✓")
                if report_url:
                    report_balance(private_key, wallet, report_url, signal_secret, client=client)
                sys.exit(0)
        except Exception as e:
            # get_order failed — could be filled OR rejected; don't assume filled.
            # Break out of the poll loop and let the cancel+FAK path handle it.
            print(f"[Order] get_order error during poll: {e} — proceeding to cancel+FAK")
    # ── Step 3: Timeout — cancel limit and FAK for unfilled remainder ─────────
    print(f"[Order] Limit not filled in {LIMIT_WAIT_S}s — cancelling...")
    try:
        client.cancel_order(order_id=limit_order_id)
        print(f"[Order] Limit cancelled ✓")
    except Exception as e:
        print(f"[Order] Cancel error (may already be filled): {e}")
        # Re-check in case it filled during cancel attempt
        try:
            final = client.get_order(order_id=limit_order_id)
            size_matched = final.size_matched
        except Exception:
            # Order gone — assume filled
            print(f"[OK] Order gone after cancel attempt — assuming filled")
            if report_url:
                report_balance(private_key, wallet, report_url, signal_secret, client=client)
            sys.exit(0)
    filled_usdc   = float(size_matched) * float(price)
    remaining_usdc = amount - filled_usdc
    print(f"[Order] Partial fill: ${filled_usdc:.2f} filled, ${remaining_usdc:.2f} remaining")
    if remaining_usdc >= MIN_FAK_USDC:
        place_fak(remaining_usdc)
    else:
        print(f"[Order] Remaining ${remaining_usdc:.2f} below min ${MIN_FAK_USDC} — skipping FAK")
    if report_url:
        report_balance(private_key, wallet, report_url, signal_secret, client=client)
    sys.exit(0)
if __name__ == "__main__":
    main()
