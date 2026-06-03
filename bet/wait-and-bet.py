#!/usr/bin/env python3
"""
wait-and-bet.py — pre-warmed persistent polling process for the prewarm-and-bet GHA runner.

Startup sequence (all done BEFORE the signal fires):
  1. Confirm Swiss IP (ipinfo.io)
  2. Initialize Polymarket SecureClient
  3. Pre-fetch current AND next BTC 5m market from Gamma API into a local cache dict
  4. Begin polling GitHub Actions variable at 0.3s intervals

On signal fire:
  - Immediately look up the signaled candle in the in-memory cache
  - If found: use cached token_id + price — zero network latency
  - If not found (edge case): use the most recently fetched market with a warning — still
    no Gamma API call post-fire; the order fires immediately on cached data
  - No subprocess, no re-init, no extra network calls except the Polymarket order itself

Market cache is refreshed every 4 minutes during idle wait. Each refresh fetches both the
current 5m candle AND the next 5m candle, so the cache always contains the live market.
Logs "PREWARM READY — market cached, SDK warm" after each successful refresh.
"""
import os, sys, json, time, traceback
from decimal import Decimal
import requests as req_lib

# ── Environment ────────────────────────────────────────────────────────────────
SIGNAL_VAR    = os.environ.get("SIGNAL_VAR", "BET_SIGNAL")
GH_TOKEN      = os.environ["GH_TOKEN"]
GH_REPO       = os.environ.get("GH_REPO", "yiigss/polymarket-proxy")
PRIVATE_KEY   = os.environ["POLYMARKET_PRIVATE_KEY"]
WALLET        = os.environ["POLYMARKET_WALLET_ADDRESS"]
SIGNAL_SECRET = os.environ.get("SIGNAL_SECRET", "")
REPORT_URL    = os.environ.get("REPORT_URL", "")

if not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY

GAMMA_API = "https://gamma-api.polymarket.com"
GH_API    = "https://api.github.com"

POLL_INTERVAL_S   = 0.3   # poll GitHub variable every 0.3 seconds
CACHE_REFRESH_S   = 240   # refresh market data every 4 minutes (240 seconds)
FIVE_MIN_MS       = 5 * 60 * 1000
MIN_FAK_USDC      = 0.50  # skip retry if remaining amount is below this
MAX_ATTEMPTS      = 240   # up to 240 FAK attempts (~48s at 0.2s per sleep)
FAK_RETRY_SLEEP_S = 0.2   # sleep between FAK retry attempts (was 1s in bet.py)
NO_MATCH_MSG      = "no orders found to match"


# ── Utility: BTC 5m candle open times ─────────────────────────────────────────
def current_5m_open_ms() -> int:
    now_ms = int(time.time() * 1000)
    return (now_ms // FIVE_MIN_MS) * FIVE_MIN_MS


def next_5m_open_ms() -> int:
    return current_5m_open_ms() + FIVE_MIN_MS


# ── IP check ──────────────────────────────────────────────────────────────────
def confirm_swiss_ip() -> str:
    print("[IP] Checking current IP country...", flush=True)
    ip_data = req_lib.get("https://ipinfo.io/json", timeout=10).json()
    ip      = ip_data.get("ip", "?")
    city    = ip_data.get("city", "?")
    country = ip_data.get("country", "?")
    print(f"[IP] {ip} — {city}, {country}", flush=True)
    BLOCKED = {"US", "GB"}
    if country in BLOCKED:
        raise RuntimeError(f"Blocked country — got country={country!r}. Use a non-US/UK IP.")
    return ip


# ── Gamma API: fetch BTC 5m market by candle open time ───────────────────────
def fetch_btc5m_market(candle_open_ms: int) -> dict:
    sec  = candle_open_ms // 1000
    slug = f"btc-updown-5m-{sec}"
    print(f"[Market] Fetching {slug} from Gamma API...", flush=True)
    r = req_lib.get(f"{GAMMA_API}/events?slug={slug}", timeout=15)
    r.raise_for_status()
    events = r.json()
    if not events:
        raise RuntimeError(f"No Gamma event found for slug={slug!r}")
    mkt = events[0]["markets"][0]
    outcomes  = json.loads(mkt.get("outcomes",      "[]"))
    token_ids = json.loads(mkt.get("clobTokenIds",  "[]"))
    prices    = json.loads(mkt.get("outcomePrices", "[]"))
    neg_risk  = mkt.get("negRisk", False)
    up_idx   = next(i for i, o in enumerate(outcomes) if o.lower() == "up")
    down_idx = next(i for i, o in enumerate(outcomes) if o.lower() == "down")
    return {
        "slug":       slug,
        "candle_ms":  candle_open_ms,
        "up_token":   token_ids[up_idx],
        "down_token": token_ids[down_idx],
        "up_price":   float(prices[up_idx]),
        "down_price": float(prices[down_idx]),
        "neg_risk":   neg_risk,
        "fetched_at": time.time(),
    }


def refresh_market_cache(cache: dict) -> None:
    """
    Fetch current and next 5m candle markets into cache (keyed by candle_ms).
    Keeps the cache small by pruning entries older than 2 candles behind current.
    Never called after a fire signal — only during idle wait.
    """
    now_current = current_5m_open_ms()
    now_next    = next_5m_open_ms()

    fetched = []
    for candle_ms in (now_current, now_next):
        if candle_ms in cache:
            age_s = time.time() - cache[candle_ms]["fetched_at"]
            if age_s < CACHE_REFRESH_S:
                fetched.append(cache[candle_ms]["slug"])
                continue
        try:
            market = fetch_btc5m_market(candle_ms)
            cache[candle_ms] = market
            fetched.append(market["slug"])
        except Exception as e:
            print(f"[Market] Could not fetch candle {candle_ms}: {e}", flush=True)

    # Prune stale entries (more than one 5m window behind current)
    cutoff = now_current - FIVE_MIN_MS
    stale = [k for k in cache if k < cutoff]
    for k in stale:
        del cache[k]

    if fetched:
        slugs = ", ".join(fetched)
        print(f"[Market] Cache: {slugs}", flush=True)
        print("PREWARM READY — market cached, SDK warm", flush=True)


def best_cached_market(cache: dict, signal_candle_ms: int) -> dict:
    """
    Return the cached market for signal_candle_ms, or fall back to the most
    recently fetched market if exact match is missing.
    NEVER makes a network call — operates entirely on in-memory cache.
    """
    if signal_candle_ms in cache:
        m = cache[signal_candle_ms]
        age_s = time.time() - m["fetched_at"]
        print(f"[Market] Cache hit: {m['slug']} (age={age_s:.0f}s)", flush=True)
        return m

    # Fall back to the most recently fetched entry
    if cache:
        latest = max(cache.values(), key=lambda m: m["fetched_at"])
        print(
            f"[Market] WARNING: signal candle {signal_candle_ms} not in cache "
            f"(have: {sorted(cache.keys())}). Using {latest['slug']} — proceeding immediately.",
            flush=True,
        )
        return latest

    raise RuntimeError("Market cache is empty — cannot place order without market data")


# ── GitHub variable: read ─────────────────────────────────────────────────────
def get_signal_value() -> str:
    r = req_lib.get(
        f"{GH_API}/repos/{GH_REPO}/actions/variables/{SIGNAL_VAR}",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=8,
    )
    if not r.ok:
        return "idle"
    return r.json().get("value", "idle")


# ── GitHub variable: write (acknowledge signal) ───────────────────────────────
def set_signal_value(value: str) -> None:
    try:
        req_lib.patch(
            f"{GH_API}/repos/{GH_REPO}/actions/variables/{SIGNAL_VAR}",
            headers={
                "Authorization": f"Bearer {GH_TOKEN}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
            json={"name": SIGNAL_VAR, "value": value},
            timeout=8,
        )
    except Exception as e:
        print(f"[Signal] Failed to set variable: {e}", flush=True)


# ── FAK order loop ────────────────────────────────────────────────────────────
def place_fak_orders(client, side_str: str, amount: float, market: dict) -> float:
    """
    Place FAK market orders until the full amount is filled or MAX_ATTEMPTS reached.
    Returns total amount placed (USDC).
    Uses FAK_RETRY_SLEEP_S (0.2s) between retry attempts.
    """
    from polymarket.errors import RequestRejectedError, UserInputError

    token_id = market["up_token"]  if side_str == "up" else market["down_token"]
    price    = market["up_price"]  if side_str == "up" else market["down_price"]

    print(f"[Order] Starting FAK loop: target=${amount:.2f} token={token_id[:20]}... price={price}", flush=True)

    remaining = amount
    placed    = 0.0
    attempt   = 0

    while remaining >= MIN_FAK_USDC and attempt < MAX_ATTEMPTS:
        attempt += 1
        try:
            resp = client.place_market_order(
                token_id=token_id,
                side="BUY",
                amount=Decimal(str(round(remaining, 2))),
                order_type="FAK",
            )
            filled_tokens: float | None = None
            for attr in ("size_matched", "matched_size", "filled_size", "filled"):
                if hasattr(resp, attr):
                    try:
                        filled_tokens = float(getattr(resp, attr))
                        break
                    except Exception:
                        pass
            if filled_tokens is not None:
                filled_usdc = filled_tokens * price
            else:
                filled_usdc = remaining
            placed    += filled_usdc
            remaining  = round(amount - placed, 2)
            print(
                f"[OK] FAK attempt={attempt} id={resp.order_id} status={resp.status} "
                f"filled=${filled_usdc:.2f} total=${placed:.2f} remaining=${remaining:.2f}",
                flush=True,
            )
            if remaining < MIN_FAK_USDC:
                break
            time.sleep(FAK_RETRY_SLEEP_S)

        except RequestRejectedError as e:
            if NO_MATCH_MSG in str(e).lower():
                print(f"[Retry {attempt}/{MAX_ATTEMPTS}] No liquidity — waiting {FAK_RETRY_SLEEP_S}s...", flush=True)
                time.sleep(FAK_RETRY_SLEEP_S)
                continue
            print(f"[Error] FAK rejected (attempt {attempt}): {e}", flush=True)
            traceback.print_exc()
            raise

        except UserInputError as e:
            print(f"[Error] FAK UserInputError: {e}", flush=True)
            traceback.print_exc()
            raise

        except Exception as e:
            print(f"[Error] FAK {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            raise

    return placed


# ── Balance reporting ─────────────────────────────────────────────────────────
def report_balance(client) -> None:
    if not REPORT_URL:
        return
    balance: float | None = None

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
                balance = raw / 1e6 if raw > 1000 else raw
                break
        except Exception as e:
            print(f"[Balance] get_balance_allowance({asset_type!r}) failed: {e}", flush=True)

    if balance is None:
        try:
            result = client.get_portfolio_values()
            if isinstance(result, (list, tuple)) and len(result) > 0:
                item = result[0]
                if hasattr(item, "value"):
                    balance = float(item.value)
            elif isinstance(result, (int, float)):
                balance = float(result)
        except Exception as e:
            print(f"[Balance] get_portfolio_values() failed: {e}", flush=True)

    if balance is None:
        print("[Balance] Could not fetch CLOB balance — skipping report", flush=True)
        return

    print(f"[Balance] CLOB deposited: ${balance:.4f}", flush=True)
    try:
        r = req_lib.post(
            REPORT_URL,
            json={"balance": balance, "source": "gha_clob"},
            headers={"X-Signal-Secret": SIGNAL_SECRET, "Content-Type": "application/json"},
            timeout=8,
        )
        if r.ok:
            print(f"[Balance] Reported ${balance:.4f} to server ✓", flush=True)
        else:
            print(f"[Balance] Report failed: {r.status_code} {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"[Balance] Report POST failed: {e}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # ── Step 1: Confirm Swiss IP ───────────────────────────────────────────
    confirm_swiss_ip()

    # ── Step 2: Initialize Polymarket SDK ─────────────────────────────────
    from polymarket import SecureClient
    print(f"[SDK] Creating SecureClient for wallet={WALLET[:10]}...", flush=True)
    client = SecureClient.create(private_key=PRIVATE_KEY, wallet=WALLET)
    print("[SDK] SecureClient initialized ✓", flush=True)

    # ── Step 3: Pre-fetch current AND next BTC 5m markets ─────────────────
    # Cache is a dict keyed by candle_open_ms so fire-time lookup is O(1).
    # Pre-fetching the NEXT candle ensures coverage when a candle boundary
    # falls between the last refresh and the signal fire.
    market_cache: dict = {}
    refresh_market_cache(market_cache)
    if not market_cache:
        print("[Market] ERROR: Could not fetch any market on startup — exiting", flush=True)
        sys.exit(1)
    last_cache_refresh = time.time()

    # ── Step 4: Poll for signal ────────────────────────────────────────────
    last_bet_candle = ""
    polls = 0
    max_polls = int(60 * 60 / POLL_INTERVAL_S)  # 60 min at 0.3s per poll

    while polls < max_polls:
        polls += 1

        # Periodic market data refresh every 4 minutes (idle-only, no-op at fire time)
        if time.time() - last_cache_refresh >= CACHE_REFRESH_S:
            refresh_market_cache(market_cache)
            last_cache_refresh = time.time()

        try:
            value = get_signal_value()
        except Exception as e:
            print(f"[Poll {polls}] Variable fetch error: {e}", flush=True)
            time.sleep(POLL_INTERVAL_S)
            continue

        if value.startswith("fire:"):
            parts = value.split(":")
            # Format: fire:SIDE:AMOUNT:CANDLE_MS[:NONCE]
            if len(parts) < 4:
                print(f"[Signal] Malformed fire signal: {value!r}", flush=True)
                time.sleep(POLL_INTERVAL_S)
                continue

            side_str         = parts[1]
            amount           = float(parts[2])
            signal_candle_ms = int(parts[3])

            if str(signal_candle_ms) == last_bet_candle:
                print(f"[Signal] Already placed bet for candle {signal_candle_ms} — waiting for next signal...", flush=True)
                time.sleep(POLL_INTERVAL_S)
                continue

            fire_ts = time.time()
            print(f"[Signal] Received fire — side={side_str} amount=${amount} candle={signal_candle_ms}", flush=True)

            # Acknowledge synchronously BEFORE placing the order.
            # First runner to write ack wins; any other runner that polls after
            # this write sees "ack:..." (not "fire:...") and falls through to the
            # idle branch, preventing duplicate bets. Bet proceeds regardless of
            # whether this write succeeds — a failed ack must not block the order.
            set_signal_value(f"ack:{side_str}:{amount}:{signal_candle_ms}")

            # Resolve market from cache — NO network call post-fire
            try:
                market = best_cached_market(market_cache, signal_candle_ms)
            except RuntimeError as e:
                print(f"[Market] FATAL: {e}", flush=True)
                sys.exit(1)

            # Place orders using pre-warmed client + cached market data
            try:
                placed = place_fak_orders(client, side_str, amount, market)
            except Exception:
                print("[Error] FAK order loop failed — exiting", flush=True)
                sys.exit(1)

            elapsed = time.time() - fire_ts
            if placed > 0:
                print(f"[OK] Filled ${placed:.2f} of ${amount:.2f} in {elapsed:.2f}s", flush=True)
            else:
                print(f"[Error] Could not fill any amount — no liquidity", flush=True)
                sys.exit(1)

            last_bet_candle = str(signal_candle_ms)
            print("[Bet] Complete — staying warm for martingale continuation...", flush=True)

            if REPORT_URL:
                report_balance(client)

        elif value == "done":
            print("[Signal] Sequence complete — exiting", flush=True)
            sys.exit(0)

        elif value == "abort":
            print("[Signal] Signal aborted — exiting cleanly", flush=True)
            sys.exit(0)
        else:
            if polls % 100 == 0:
                print(f"[Poll {polls}] {SIGNAL_VAR}={value!r} — still waiting...", flush=True)

        time.sleep(POLL_INTERVAL_S)

    print("Timeout: runner active for 60 min without completion", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
