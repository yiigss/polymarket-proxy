#!/usr/bin/env python3
"""
server.py — Fly.io persistent bet server
Accepts POST /bet, routes bet.py through NordVPN SOCKS5, returns result.
No GHA queue, no VPN install — ~3-5s per bet.
"""
import os, json, subprocess, random, signal, sys, hashlib, hmac, time, base64
from http.server import HTTPServer, BaseHTTPRequestHandler

CH_SOCKS5_SERVERS = [
    "84.39.112.20",    # ch218.nordvpn.com
    "185.9.18.84",     # ch219.nordvpn.com
    "37.120.213.131",  # ch198.nordvpn.com
]

BET_SECRET = os.environ.get("BET_SECRET", "")
BET_PY     = os.path.join(os.path.dirname(__file__), "bet", "bet.py")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[HTTP] {fmt % args}", flush=True)

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        elif self.path == "/balance":
            self._handle_balance()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_balance(self):
        """Fetch CLOB cash balance by deriving fresh L2 keys.
        Render Frankfurt is EU — no SOCKS5 needed (same as bet.py).
        Derives a fresh L2 API key on every call (no stale credentials)."""
        import requests as req_lib
        from eth_account import Account
        from eth_account.messages import encode_defunct

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

        if not private_key:
            self._send_json(503, {"error": "POLYMARKET_PRIVATE_KEY not set"})
            return
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        try:
            # ── Step 1: L1 auth — sign timestamp with private key ──────────
            acct   = Account.from_key(private_key)
            wallet = acct.address
            ts_l1  = str(int(time.time()))
            msg    = encode_defunct(text=ts_l1)
            signed = acct.sign_message(msg)
            sig_l1 = signed.signature.hex()
            if not sig_l1.startswith("0x"):
                sig_l1 = "0x" + sig_l1
            print(f"[Balance] L1 auth for wallet={wallet[:10]}...", flush=True)

            # ── Step 2: Derive fresh L2 API key (no proxy — Render is EU) ──
            r_key = req_lib.post(
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
                self._send_json(200, {"error": f"L2 key derivation failed {r_key.status_code}: {r_key.text}", "balance": None})
                return
            key_data   = r_key.json()
            api_key    = key_data["apiKey"]
            secret_b64 = key_data["secret"]
            passphrase = key_data["passphrase"]
            print(f"[Balance] Fresh L2 key derived: {api_key[:8]}...", flush=True)

            # ── Step 3: L2 HMAC signature for balance request ──────────────
            ts_l2  = str(int(time.time()))
            msg_l2 = ts_l2 + "GET" + "/balance-allowance?asset_type=COLLATERAL"
            secret_stripped = secret_b64.rstrip("=")
            secret_padded   = secret_stripped + "=" * (-len(secret_stripped) % 4)
            sig_l2 = base64.b64encode(
                hmac.new(base64.urlsafe_b64decode(secret_padded), msg_l2.encode(), hashlib.sha256).digest()  # type: ignore[attr-defined]
            ).decode()

            # ── Step 4: Fetch balance (no proxy) ───────────────────────────
            r_bal = req_lib.get(
                "https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL",
                headers={
                    "POLY-API-KEY":    api_key,
                    "POLY-SIGNATURE":  sig_l2,
                    "POLY-TIMESTAMP":  ts_l2,
                    "POLY-PASSPHRASE": passphrase,
                },
                timeout=20,
            )
            body = r_bal.json()
            if not r_bal.ok:
                self._send_json(200, {"error": f"CLOB balance {r_bal.status_code}: {body}", "balance": None})
                return
            raw     = body.get("balance", "0")
            balance = float(raw) / 1_000_000
            print(f"[Balance] CLOB cash: ${balance:.2f}", flush=True)
            self._send_json(200, {"balance": balance})
        except Exception as e:
            print(f"[Balance] Error: {e}", flush=True)
            self._send_json(200, {"error": str(e), "balance": None})

    def do_POST(self):
        if self.path != "/bet":
            self.send_response(404)
            self.end_headers()
            return

        # Auth
        if BET_SECRET and self.headers.get("X-Secret", "") != BET_SECRET:
            self._send_json(401, {"error": "Unauthorized"})
            return

        # Parse body
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            side      = body["side"]
            amount    = str(body["amount"])
            candle_ms = str(body["candle_ms"])
        except Exception as e:
            self._send_json(400, {"error": f"Bad request: {e}"})
            return

        # Pick a random NordVPN Switzerland SOCKS5 server
        nordvpn_user = os.environ["NORDVPN_SERVICE_USERNAME"]
        nordvpn_pass = os.environ["NORDVPN_SERVICE_PASSWORD"]
        server_ip    = random.choice(CH_SOCKS5_SERVERS)
        print(f"[Bet] {side} ${amount} (no proxy — Render Frankfurt is EU)", flush=True)

        env = {**os.environ}

        try:
            result = subprocess.run(
                ["python3", BET_PY, side, amount, candle_ms],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            print(f"[Bet] Done rc={result.returncode}", flush=True)
            if result.stdout:
                print(result.stdout, flush=True)
            if result.stderr and result.stderr.strip():
                print(result.stderr, flush=True)
            self._send_json(200, {
                "success": result.returncode == 0,
                "stdout":  result.stdout,
                "stderr":  result.stderr,
            })
        except subprocess.TimeoutExpired:
            self._send_json(200, {"success": False, "stdout": "", "stderr": "Timeout after 60s"})
        except Exception as e:
            self._send_json(200, {"success": False, "stdout": "", "stderr": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"[Server] Listening on port {port}", flush=True)
    srv = HTTPServer(("0.0.0.0", port), Handler)
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    srv.serve_forever()
