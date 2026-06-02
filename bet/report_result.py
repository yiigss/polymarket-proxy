#!/usr/bin/env python3
"""
report_result.py — wait for a BTC 5m candle to close and report the bet result to the server.
Run in background immediately after bet.py places an order.
Usage: python3 bet/report_result.py <up|down> <amount_usdc> <candle_open_time_ms> <result_url>
"""
import os, sys, time, json
import requests

GAMMA_API = "https://gamma-api.polymarket.com"

def main():
    if len(sys.argv) < 5:
        print("Usage: report_result.py <side> <amount> <candle_ms> <result_url>", file=sys.stderr)
        sys.exit(1)

    side       = sys.argv[1]          # "up" or "down"
    amount     = float(sys.argv[2])
    candle_ms  = int(sys.argv[3])
    result_url = sys.argv[4]
    secret     = os.environ.get("SIGNAL_SECRET", "")

    # Wait until the candle closes (5 min after open) + 15s buffer
    close_at = candle_ms / 1000 + 300 + 15
    now = time.time()
    wait = close_at - now
    if wait > 0:
        print(f"[Result] Waiting {wait:.0f}s for candle to close...", flush=True)
        time.sleep(wait)

    # Fetch market with retries
    sec  = candle_ms // 1000
    slug = f"btc-updown-5m-{sec}"
    events = None
    for attempt in range(6):
        try:
            r = requests.get(f"{GAMMA_API}/events?slug={slug}", timeout=15)
            r.raise_for_status()
            events = r.json()
            break
        except Exception as e:
            print(f"[Result] Gamma API attempt {attempt+1} failed: {e}", flush=True)
            time.sleep(10)

    if not events:
        print("[Result] Could not fetch market after retries — skipping report", flush=True)
        sys.exit(0)

    mkt      = events[0]["markets"][0]
    outcomes = json.loads(mkt.get("outcomes",      "[]"))
    prices   = json.loads(mkt.get("outcomePrices", "[]"))

    up_idx   = next((i for i, o in enumerate(outcomes) if o.lower() == "up"),   None)
    down_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "down"), None)
    if up_idx is None or down_idx is None:
        print(f"[Result] Could not find up/down outcomes in {outcomes} — skipping", flush=True)
        sys.exit(0)

    up_price   = float(prices[up_idx])
    down_price = float(prices[down_idx])
    print(f"[Result] Market prices: up={up_price} down={down_price}", flush=True)

    # Determine result from resolved prices (winner -> ~1.0, loser -> ~0.0)
    if side == "up":
        if up_price > 0.8:
            result, payout = "win",  round(amount / up_price,   2)
        elif up_price < 0.2:
            result, payout = "loss", 0.0
        else:
            print(f"[Result] Market not yet resolved (up={up_price}) — skipping", flush=True)
            sys.exit(0)
    else:
        if down_price > 0.8:
            result, payout = "win",  round(amount / down_price, 2)
        elif down_price < 0.2:
            result, payout = "loss", 0.0
        else:
            print(f"[Result] Market not yet resolved (down={down_price}) — skipping", flush=True)
            sys.exit(0)

    print(f"[Result] {slug} -> {result.upper()} side={side} stake={amount} payout={payout}", flush=True)

    try:
        resp = requests.post(
            result_url,
            json={"side": side, "amount": amount, "candle_ms": candle_ms, "result": result, "payout": payout},
            headers={"Content-Type": "application/json", "X-Signal-Secret": secret},
            timeout=10,
        )
        if resp.ok:
            print(f"[Result] Reported to server: {resp.json()}", flush=True)
        else:
            print(f"[Result] Server rejected: {resp.status_code} {resp.text[:120]}", flush=True)
    except Exception as e:
        print(f"[Result] POST failed: {e}", flush=True)

if __name__ == "__main__":
    main()
