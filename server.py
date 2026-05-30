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
        """Fetch CLOB cash balance using stored API key credentials.
        Credentials can be passed via X-Poly-Key/Secret/Pass headers (fallback from env)."""
        api_key    = self.headers.get("X-Poly-Key")    or os.environ.get("POLYMARKET_API_KEY", "")
        secret_b64 = self.headers.get("X-Poly-Secret") or os.environ.get("POLYMARKET_SECRET", "")
        passphrase = self.headers.get("X-Poly-Pass")   or os.environ.get("POLYMARKET_PASSPHRASE", "")
        if not api_key or not secret_b64 or not passphrase:
            self._send_json(503, {"error": "CLOB credentials not configured"})
            return
        try:
            import urllib.request as _urllib
            ts  = str(int(time.time()))
            msg = ts + "GET" + "/balance-allowance?asset_type=COLLATERAL"
            sig = base64.b64encode(
                hmac.new(base64.b64decode(secret_b64), msg.encode(), hashlib.sha256).digest()  # type: ignore[attr-defined]
            ).decode()
            req = _urllib.Request(
                "https://clob.polymarket.com/balance-allowance?asset_type=COLLATERAL",
                headers={
                    "POLY-API-KEY":    api_key,
                    "POLY-SIGNATURE":  sig,
                    "POLY-TIMESTAMP":  ts,
                    "POLY-PASSPHRASE": passphrase,
                },
            )
            with _urllib.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode())
            raw = body.get("balance", "0")
            balance = float(raw) / 1_000_000
            self._send_json(200, {"balance": balance, "raw": body})
        except Exception as e:
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
