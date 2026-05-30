#!/usr/bin/env python3
"""
balance_fetch.py — fetch CLOB cash balance via fresh L2 key derivation and report back.

Runs inside a GHA runner that already has NordVPN CH connected at OS level,
so all outbound traffic is already Swiss — no SOCKS5 needed.

Usage (env vars required):
  POLYMARKET_PRIVATE_KEY — hex private key (with or without 0x)
  REPORT_URL             — https://<domain>/api/btc/balance-report
  SIGNAL_SECRET          — shared secret for X-Signal-Secret header
"""
import os, sys, json, time, hmac, hashlib, base64
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

def main() -> None:
    private_key   = os.environ["POLYMARKET_PRIVATE_KEY"]
    report_url    = os.environ["REPORT_URL"]
    signal_secret = os.environ.get("SIGNAL_SECRET", "")

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    # ── L1 auth: sign timestamp with private key ────────────────────────────
    acct   = Account.from_key(private_key)
    wallet = acct.address
    ts_l1  = str(int(time.time()))
    signed = acct.sign_message(encode_defunct(text=ts_l1))
    sig_l1 = "0x" + signed.signature.hex().lstrip("0x")
    print(f"[Balance] L1 auth for wallet={wallet[:10]}...", flush=True)

    # ── Derive fresh L2 API key ─────────────────────────────────────────────
    r_key = requests.post(
        "https://clob.polymarket.com/auth/api-key",
        headers={
            "POLY_ADDRESS":   wallet,
            "POLY_SIGNATURE": sig_l1,
            "POLY_TIMESTAMP": ts_l1,
            "POLY_NONCE":     "0",
            "Content-Type":   "application/json",
        },
        json={},
        timeout=20,
    )
    if not r_key.ok:
        print(f"[Balance] L2 key derivation failed {r_key.status_code}: {r_key.text}", flush=True)
        sys.exit(1)
    key_data   = r_key.json()
    api_key    = key_data["apiKey"]
    secret_b64 = key_data["secret"]
    passphrase = key_data["passphrase"]
    print(f"[Balance] Fresh L2 key: {api_key[:8]}...", flush=True)

    # ── L2 HMAC signature ───────────────────────────────────────────────────
    ts_l2  = str(int(time.time()))
    msg_l2 = ts_l2 + "GET" + "/balance-allowance?asset_type=COLLATERAL"
    secret_stripped = secret_b64.rstrip("=")
    secret_padded   = secret_stripped + "=" * (-len(secret_stripped) % 4)
    sig_l2 = base64.b64encode(
        hmac.new(base64.urlsafe_b64decode(secret_padded), msg_l2.encode(), hashlib.sha256).digest()
    ).decode()

    # ── Fetch balance ───────────────────────────────────────────────────────
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

    # ── Report back to API server ───────────────────────────────────────────
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
