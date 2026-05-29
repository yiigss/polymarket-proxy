#!/usr/bin/env node
/**
 * place-bet.js — runs inside GitHub Actions (Ubuntu, VPN connected to Switzerland)
 * Usage: node index.js <up|down> <amount_usdc> <candle_open_time_ms>
 */
const { ClobClient, Side, OrderType, SignatureType } = require("@polymarket/clob-client");
const { ethers } = require("ethers");

const CLOB_HOST = "https://clob.polymarket.com";
const CHAIN_ID  = 137;
const GAMMA_API = "https://gamma-api.polymarket.com";

// Intercept fetch to log outgoing CLOB order payloads
const _origFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {}) => {
  if (typeof url === "string" && url.includes("clob.polymarket.com")) {
    console.log(`[HTTP] ${opts.method ?? "GET"} ${url}`);
    if (opts.body) {
      try { console.log("[Body]", JSON.stringify(JSON.parse(opts.body), null, 2).slice(0, 2000)); }
      catch { console.log("[Body raw]", String(opts.body).slice(0, 500)); }
    }
    const resp = await _origFetch(url, opts);
    const clone = resp.clone();
    const text  = await clone.text();
    console.log(`[Resp] ${resp.status}`, text.slice(0, 500));
    return resp;
  }
  return _origFetch(url, opts);
};

async function getBtc5mMarket(candleOpenTimeMs) {
  const sec  = Math.floor(candleOpenTimeMs / 1000);
  const slug = `btc-updown-5m-${sec}`;
  console.log(`[Market] Looking up ${slug}`);

  const r = await fetch(`${GAMMA_API}/events?slug=${slug}`);
  if (!r.ok) throw new Error(`Gamma API ${r.status}`);
  const events = await r.json();
  if (!events?.length) throw new Error(`No market for slug ${slug}`);

  const mkt     = events[0]?.markets?.[0];
  const outcomes = JSON.parse(mkt.outcomes       ?? "[]");
  const tokenIds = JSON.parse(mkt.clobTokenIds   ?? "[]");
  const prices   = JSON.parse(mkt.outcomePrices  ?? "[]");
  const negRisk  = mkt.negRisk ?? false;

  const upIdx   = outcomes.findIndex(o => o.toLowerCase() === "up");
  const downIdx = outcomes.findIndex(o => o.toLowerCase() === "down");
  if (upIdx === -1 || downIdx === -1) throw new Error(`Bad outcomes: ${JSON.stringify(outcomes)}`);

  return {
    slug,
    upTokenId:   tokenIds[upIdx],
    downTokenId: tokenIds[downIdx],
    upPrice:     parseFloat(prices[upIdx]   ?? "0.5"),
    downPrice:   parseFloat(prices[downIdx] ?? "0.5"),
    negRisk,
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

  const key        = process.env.POLYMARKET_API_KEY;
  const secret     = process.env.POLYMARKET_SECRET;
  const passphrase = process.env.POLYMARKET_PASSPHRASE;
  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;
  const funder     = process.env.POLYMARKET_WALLET_ADDRESS;
  if (!key || !secret || !passphrase || !privateKey) throw new Error("Missing credentials");

  const pk     = privateKey.startsWith("0x") ? privateKey : `0x${privateKey}`;
  const wallet = new ethers.Wallet(pk);

  const ipData = await (await fetch("https://ipinfo.io/json")).json();
  console.log(`[IP] ${ipData.ip} — ${ipData.city}, ${ipData.country}`);
  if (ipData.country !== "CH") throw new Error(`Not CH — got ${ipData.country}`);

  const market  = await getBtc5mMarket(candleOpenTime);
  const tokenId = side === "up" ? market.upTokenId : market.downTokenId;
  const price   = side === "up" ? market.upPrice   : market.downPrice;
  const effectiveAmount = Math.max(amount, 15);

  console.log(`[Market] ${market.slug} | negRisk=${market.negRisk} | token=${tokenId.slice(0,12)}... | price=${price} | amount=$${effectiveAmount}`);

  // Try POLY_PROXY then EOA, and log the order object before posting
  for (const sigType of [SignatureType.POLY_PROXY, SignatureType.EOA]) {
    const label  = sigType === SignatureType.POLY_PROXY ? "POLY_PROXY" : "EOA";
    const client = new ClobClient(CLOB_HOST, CHAIN_ID, wallet, { key, secret, passphrase }, sigType, funder);

    // Build order without posting to inspect it first
    try {
      const orderArgs = { tokenID: tokenId, amount: effectiveAmount, side: Side.BUY, ...(market.negRisk ? { negRisk: true } : {}) };
      const signed = await client.createMarketOrder(orderArgs);
      console.log(`[${label}] Signed order:`, JSON.stringify(signed, null, 2).slice(0, 800));

      const result = await client.postOrder(signed, OrderType.FOK);
      console.log(`[${label}] Post result:`, JSON.stringify(result));

      const errMsg = result?.errorMsg ?? result?.error ?? result?.message;
      if (result?.success) {
        const orderId = result?.orderID ?? result?.order_id ?? result?.id ?? "?";
        console.log(`[OK] Order placed via ${label}! id=${orderId}`);
        process.exit(0);
      }
      console.log(`[${label}] failed: ${errMsg}`);
    } catch (err) {
      console.log(`[${label}] exception: ${err.message}`);
    }
  }

  throw new Error("All signature types exhausted — order not placed");
}

main().catch(err => {
  console.error(`[FATAL] ${err.message}`);
  process.exit(1);
});
