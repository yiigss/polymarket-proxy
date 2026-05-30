#!/usr/bin/env python3
"""
balance_fetch.py — fetch CLOB cash balance via fresh L2 key derivation and report back.

Runs inside a GHA runner with NordVPN CH connected at OS level.
Uses the polymarket-client SDK's own EIP-712 L1 auth (same as bet.py).

Env vars:
  POLYMARKET_PRIVATE_KEY — hex private key (with or without 0x)
  REPORT_URL             — https://<domain>/api/btc/balance-report
  SIGNAL_SECRET          — shared secret for X-Signal-Secret header
"""
import os, sys, time, hmac, hashlib, base64
import requests
from eth_account import Account
from polymarket._internal.l1_auth import sign_api_key_auth
from polymarket._internal.actions.auth import build_l1_auth_headers

CHAIN_ID = 137  # Polygon mainnet

def main() -> None:
    private_key   = os.environ["POLYMARKET_PRIVATE_KEY"]
    report_url    = os.environ["REPORT_URL"]
    signal_secret = os.environ.get("SIGNAL_SECRET", "")

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    acct = Account.from_key(private_key)
    wallet = acct.address
    print(f"[Balance] Wallet: {wallet[:10]}...", flush=True)

    # ── Step 1: EIP-712 L1 auth signature ──────────────────────────────────
    ts = int(time.time())
    sig = sign_api_key_auth(acct, chain_id=CHAIN_ID, timestamp=ts, nonce=0)
    headers_l1 = build_l1_auth_headers(sig)
    print(f"[Balance] L1 signed (EIP-712) ts={ts}", flush=True)

    # ── Step 2: Derive fresh L2 API key ────────────────────────────────────
    r_key = requests.post(
        "https://clob.polymarket.com/auth/api-key",
        headers=headers_l1,
        json={},
        timeout=20,
    )
    if not r_key.ok:
        # Try derive-api-key if key already exists (400 = already created)
        if r_key.status_code == 400:
            r_key = requests.get(
                "https://clob.polymarket.com/auth/derive-api-key",
                headers=headers_l1,
                timeout=20,
            )
        if not r_key.ok:
            print(f"[Balance] L2 key failed {r_key.status_code}: {r_key.text}", flush=True)
            sys.exit(1)
    key_data   = r_key.json()
    api_key    = key_data["apiKey"]
    secret_b64 = key_data["secret"]
    passphrase = key_data["passphrase"]
    print(f"[Balance] Fresh L2 key: {api_key[:8]}...", flush=True)

    # ── Step 3: L2 HMAC signature ───────────────────────────────────────────
    ts_l2  = str(int(time.time()))
    msg_l2 = ts_l2 + "GET" + "/balance-allowance?asset_type=COLLATERAL"
    secret_stripped = secret_b64.rstrip("=")
    secret_padded   = secret_stripped + "=" * (-len(secret_stripped) % 4)
    sig_l2 = base64.b64encode(
        hmac.new(base64.urlsafe_b64decode(secret_padded), msg_l2.encode(), hashlib.sha256).digest()
    ).decode()

    # ── Step 4: Fetch CLOB balance ─────────────────────────────────────────
    r_bal = requests.get(
        "https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL",
        headers={
            "POLY-API-KEY":    api_key,
            "POLY-SIGNATURE":  sig_l2,
            "POLY-TIMESTAMP":  ts_l2,
            "POLY-PASSPHRASE": passphrase,
        },
        timeout=20,
    )
    if not r_bal.ok:
        print(f"[Balance] CLOB balance failed {r_bal.status_code}: {r_bal.text}", flush=True)
        sys.exit(1)

    body    = r_bal.json()
    raw     = body.get("balance", "0")
    balance = float(raw) / 1_000_000
    print(f"[Balance] CLOB cash: ${balance:.2f}", flush=True)

    # ── Step 5: Report to API server ───────────────────────────────────────
    r_rep = requests.post(
        report_url,
        json={"balance": balance, "source": "clob_gha"},
        headers={"X-Signal-Secret": signal_secret, "Content-Type": "application/json"},
        timeout=10,
    )
    if r_rep.ok:
        print(f"[Balance] Reported ${balance:.2f} to server ✓", flush=True)
    else:
        print(f"[Balance] Report failed {r_rep.status_code}: {r_rep.text}", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
