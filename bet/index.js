#!/usr/bin/env node
/**
 * index.js — thin launcher: installs py-clob-client and delegates to bet.py
 * The JS @polymarket/clob-client generates orders with a mismatched version
 * field; the Python reference client is what Polymarket actually supports.
 */
const { execSync } = require("child_process");
const path = require("path");

const [,, side, amount, candleMs] = process.argv;
if (!side || !amount || !candleMs) {
  console.error("Usage: node index.js <up|down> <amount> <candle_time_ms>");
  process.exit(1);
}

console.log("[Launcher] Installing Python dependencies...");
execSync(
  "pip3 install --quiet --break-system-packages py-clob-client requests",
  { stdio: "inherit" }
);

console.log("[Launcher] Running bet.py...");
execSync(
  `python3 ${path.join(__dirname, "bet.py")} ${side} ${amount} ${candleMs}`,
  { stdio: "inherit" }
);
