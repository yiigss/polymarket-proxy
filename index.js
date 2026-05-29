const http = require("http");
const https = require("https");
const { SocksProxyAgent } = require("socks-proxy-agent");

const PORT = process.env.PORT || 3000;
const TARGET = "https://clob.polymarket.com";

// NordVPN Switzerland SOCKS5 servers (IP addresses — no DNS needed)
const CH_SOCKS5_SERVERS = [
  "84.39.112.20",   // ch218.nordvpn.com
  "185.9.18.84",    // ch219.nordvpn.com
  "37.120.213.131", // ch198.nordvpn.com
];

// Headers that reveal the originating client IP/country and must be stripped
// before forwarding to Polymarket.
const STRIP_HEADERS = new Set([
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-forwarded-port",
  "x-real-ip",
  "forwarded",
  // Cloudflare headers (set based on the caller's IP, not the server's)
  "cf-connecting-ip",
  "cf-ipcountry",
  "cf-ray",
  "cf-visitor",
  "cf-ew-via",
  "cf-worker",
  "true-client-ip",
  "cdn-loop",
  // Internal credential headers — strip before forwarding to Polymarket
  "x-nord-user",
  "x-nord-pass",
]);

const server = http.createServer((req, res) => {
  // Diagnostic: return this server's outbound IP
  if (req.url === "/whoami") {
    https.get("https://ipinfo.io/json", (r) => {
      let d = "";
      r.on("data", (c) => (d += c));
      r.on("end", () => {
        res.writeHead(200, { "content-type": "application/json", "access-control-allow-origin": "*" });
        res.end(d);
      });
    }).on("error", (e) => {
      res.writeHead(500);
      res.end(JSON.stringify({ error: e.message }));
    });
    return;
  }

  // Read NordVPN credentials passed by the caller (stripped before forwarding)
  const nordUser = req.headers["x-nord-user"];
  const nordPass = req.headers["x-nord-pass"];

  const url = new URL(req.url, TARGET);
  const options = {
    hostname: "clob.polymarket.com",
    port: 443,
    path: url.pathname + url.search,
    method: req.method,
    headers: {},
  };

  // Forward headers, stripping anything that leaks origin info or credentials
  for (const [k, v] of Object.entries(req.headers)) {
    if (!STRIP_HEADERS.has(k.toLowerCase()) && k.toLowerCase() !== "host") {
      options.headers[k] = v;
    }
  }
  options.headers["host"] = "clob.polymarket.com";

  // If NordVPN credentials supplied, tunnel through a Swiss SOCKS5 exit node
  if (nordUser && nordPass) {
    const serverIp = CH_SOCKS5_SERVERS[Math.floor(Math.random() * CH_SOCKS5_SERVERS.length)];
    options.agent = new SocksProxyAgent(
      `socks5h://${encodeURIComponent(nordUser)}:${encodeURIComponent(nordPass)}@${serverIp}:1080`
    );
    console.log(`[proxy] Routing via NordVPN CH: ${serverIp}`);
  } else {
    console.log("[proxy] WARNING: No NordVPN credentials — using server outbound IP");
  }

  const proxy = https.request(options, (upstream) => {
    res.writeHead(upstream.statusCode, {
      ...upstream.headers,
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "*",
      "access-control-allow-headers": "*",
    });
    upstream.pipe(res);
  });

  proxy.on("error", (err) => {
    console.error("[proxy] Error:", err.message);
    res.writeHead(502);
    res.end(JSON.stringify({ error: err.message }));
  });

  req.pipe(proxy);
});

server.listen(PORT, () => {
  console.log(`Polymarket proxy running on port ${PORT} → ${TARGET}`);
  console.log(`NordVPN CH SOCKS5 routing: ${nordUser ? "ENABLED" : "DISABLED (set X-Nord-User/Pass headers)"}`);
});
