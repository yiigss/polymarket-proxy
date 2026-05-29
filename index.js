const http = require("http");
const https = require("https");

const PORT = process.env.PORT || 3000;
const TARGET = "https://clob.polymarket.com";

// Strip any header that could reveal the originating client's IP or country.
// Cloudflare (used by Render) adds CF-IPCountry, CF-Ray, CF-Visitor etc. using
// the *caller's* IP — which is Replit (US). We must drop all of them so
// Polymarket only sees the Frankfurt server IP.
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

  const url = new URL(req.url, TARGET);
  const options = {
    hostname: "clob.polymarket.com",
    port: 443,
    path: url.pathname + url.search,
    method: req.method,
    headers: {},
  };

  // Forward headers, stripping anything that leaks the caller's origin
  for (const [k, v] of Object.entries(req.headers)) {
    if (!STRIP_HEADERS.has(k.toLowerCase()) && k.toLowerCase() !== "host") {
      options.headers[k] = v;
    }
  }
  options.headers["host"] = "clob.polymarket.com";

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
    res.writeHead(502);
    res.end(JSON.stringify({ error: err.message }));
  });

  req.pipe(proxy);
});

server.listen(PORT, () => {
  console.log(`Polymarket proxy running on port ${PORT} → ${TARGET}`);
});
