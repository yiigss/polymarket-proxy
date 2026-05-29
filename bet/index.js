#!/usr/bin/env node
/**
 * place-bet.js — runs inside GitHub Actions (Ubuntu, VPN connected to Switzerland)
 * Usage: node index.js <up|down> <amount_usdc> <candle_open_time_ms>
 *
 * All Polymarket credentials come from environment variables (GHA secrets).
 * No proxy needed — we have a Swiss IP via NordVPN.
 */
const { ClobClient, Side, OrderType, AssetType } = require("@polymarket/clob-client");
const { ethers } = require("ethers");

const CLOB_HOST = "https://clob.polymarket.com";
const CHAIN_ID  = 137; // Polygon mainnet
const GAMMA_API = "https://gamma-api.polymarket.com";

async function getBtc5mMarket(candleOpenTimeMs) {
  const sec  = Math.floor(candleOpenTimeMs / 1000);
  const slug = `btc-updown-5m-${sec}`;
  const url  = `${GAMMA_API}/events?slug=${slug}`;
  console.log(`[Market] Looking up ${slug}`);

  const r = await fetch(url);
  if (!r.ok) throw new Error(`Gamma API ${r.status}`);
  const events = await r.json();
  if (!events?.length) throw new Error(`No market for slug ${slug}`);

  const mkt = events[0]?.markets?.[0];
  if (!mkt) throw new Error(`No market data in ${slug}`);

  const outcomes  = JSON.parse(mkt.outcomes  ?? "[]");
  const tokenIds  = JSON.parse(mkt.clobTokenIds ?? "[]");
  const prices    = JSON.parse(mkt.outcomePrices ?? "[]");

  const upIdx   = outcomes.findIndex(o => o.toLowerCase() === "up");
  const downIdx = outcomes.findIndex(o => o.toLowerCase() === "down");
  if (upIdx === -1 || downIdx === -1) throw new Error(`Bad outcomes: ${JSON.stringify(outcomes)}`);

  return {
    slug,
    upTokenId:   tokenIds[upIdx],
    downTokenId: tokenIds[downIdx],
    upPrice:     parseFloat(prices[upIdx]   ?? "0.5"),
    downPrice:   parseFloat(prices[downIdx] ?? "0.5"),
  };
}

async function main() {
  const [,, side, amountStr, candleTimeMsStr] = process.argv;
  if (!side || !amountStr || !candleTimeMsStr) {
    console.error("Usage: node index.js <up|down> <amount> <candle_time_ms>");
    process.exit(1);
  }

  const amount         = parseFloat(amountStr);
  const candleOpenTime = parseInt(candleTimeMsStr, 10);

  console.log(`[Bet] side=${side} amount=$${amount} candle=${new Date(candleOpenTime).toISOString()}`);

  // ── Credentials from GHA secrets ────────────────────────────────────────────
  const key         = process.env.POLYMARKET_API_KEY;
  const secret      = process.env.POLYMARKET_SECRET;
  const passphrase  = process.env.POLYMARKET_PASSPHRASE;
  const privateKey  = process.env.POLYMARKET_PRIVATE_KEY;
  const funder      = process.env.POLYMARKET_WALLET_ADDRESS;

  if (!key || !secret || !passphrase || !privateKey) {
    throw new Error("Missing Polymarket credentials in environment");
  }

  const pk     = privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`;
  const wallet = new ethers.Wallet(pk);
  const client = new ClobClient(CLOB_HOST, CHAIN_ID, wallet, { key, secret, passphrase }, 0, funder);

  // ── Verify we are in Switzerland ────────────────────────────────────────────
  const ipResp = await fetch("https://ipinfo.io/json");
  const ipData = await ipResp.json();
  console.log(`[IP] ${ipData.ip} — ${ipData.city}, ${ipData.country}`);
  if (ipData.country !== "CH") {
    throw new Error(`Not connected to Switzerland (country=${ipData.country}). VPN issue.`);
  }

  // ── Find market ─────────────────────────────────────────────────────────────
  const market  = await getBtc5mMarket(candleOpenTime);
  const tokenId = side === "up" ? market.upTokenId : market.downTokenId;
  const price   = side === "up" ? market.upPrice   : market.downPrice;

  console.log(`[Market] ${market.slug} | token=${tokenId.slice(0,12)}... | price=${price}`);

  // Polymarket minimum order is $15; respect it silently
  const effectiveAmount = Math.max(amount, 15);
  if (effectiveAmount !== amount) {
    console.log(`[Bet] Amount $${amount} below $15 minimum — using $${effectiveAmount}`);
  }

  // ── Place order ─────────────────────────────────────────────────────────────
  const result = await client.createAndPostMarketOrder(
    { tokenID: tokenId, amount: effectiveAmount, side: Side.BUY },
    undefined,
    OrderType.FOK
  );

  console.log(`[Order] Raw response: ${JSON.stringify(result)}`);

  const errMsg = result?.errorMsg ?? result?.error ?? result?.message;
  if (!result?.success && errMsg) {
    throw new Error(`CLOB rejected: ${errMsg}`);
  }

  const orderId = result?.orderID ?? result?.order_id ?? result?.id ?? "unknown";
  console.log(`[OK] Order placed! id=${orderId} amount=$${effectiveAmount} price=${price}`);
  console.log(`::set-output name=order_id::${orderId}`);
  console.log(`::set-output name=amount::${effectiveAmount}`);
}

main().catch(err => {
  console.error(`[FATAL] ${err.message}`);
  process.exit(1);
});
