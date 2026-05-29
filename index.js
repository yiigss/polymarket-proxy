const http = require("http");
const https = require("https");

const PORT = process.env.PORT || 3000;
const TARGET = "https://clob.polymarket.com";

const STRIP_HEADERS = new Set([
  "x-forwarded-for",
  "x-real-ip",
  "cf-connecting-ip",
  "true-client-ip",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-forwarded-port",
  "forwarded",
]);

const server = http.createServer((req, res) => {
  const url = new URL(req.url, TARGET);
  const options = {
    hostname: "clob.polymarket.com",
    port: 443,
    path: url.pathname + url.search,
    method: req.method,
    headers: {},
  };

  // Forward headers, stripping IP leakers
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
