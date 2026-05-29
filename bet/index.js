#!/usr/bin/env node
/**
 * index.js — thin launcher: installs polymarket-client (V2 SDK) and delegates to bet.py
 * py-clob-client was archived in April 2026 after the Polymarket CLOB v2 migration.
 * All orders via the old client fail with order_version_mismatch.
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
  "pip3 install --quiet --pre --break-system-packages 'polymarket-client>=0.1.0b3' requests",
  { stdio: "inherit" }
);

console.log("[Launcher] Running bet.py...");
execSync(
  `python3 ${path.join(__dirname, "bet.py")} ${side} ${amount} ${candleMs}`,
  { stdio: "inherit" }
);
