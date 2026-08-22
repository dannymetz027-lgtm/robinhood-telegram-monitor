#!/usr/bin/env python3
"""
Robinhood Chain + Twitter (X) Telegram Monitor

Runs two concurrent asyncio loops:
  - On-chain Uniswap V2/V3 pool tracker (RPC eth_getLogs, every 3s)
  - Twitter RSS tracker (RSS.app feeds, every 10s)

Deploy on Railway, Render, Fly.io, or any VPS:
  1. Set environment variables (see .env.example)
  2. pip install -r requirements.txt
  3. python bot.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests
from eth_abi import decode as abi_decode
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8914376349:AAFzpCeJCGIJ6aKW6C2t4WUrBmNv8WGlVZk",
)
TELEGRAM_CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("TELEGRAM_CHAT_ID", "7585957774,8638097560").split(",")
    if cid.strip()
]

TRUMP_RSS_URL = os.environ.get(
    "TRUMP_RSS_URL",
    "https://rss.app/feeds/psYBJMrD9XMwxbnu.xml",
)
MELANIA_RSS_URL = os.environ.get(
    "MELANIA_RSS_URL",
    "https://rss.app/feeds/rv81p1DmwkN3NvkG.xml",
)

RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
DEXSCREENER_BASE = "https://api.dexscreener.com"
DEXSCREENER_CHAIN_SLUG = "robinhood"

# Uniswap factories on Robinhood Chain (4663)
UNI_V2_FACTORY = "0x8bcEaA40B9AcdfAedF85AdF4FF01F5Ad6517937f"
UNI_V3_FACTORY = "0x1f7d7550B1b028f7571E69A784071F0205FD2EfA"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"

# keccak256("PairCreated(address,address,address,uint256)")
TOPIC_PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
# keccak256("PoolCreated(address,address,uint24,int24,address)")
TOPIC_POOL_CREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

# ERC-20 selectors
SELECTOR_NAME = "0x06fdde03"
SELECTOR_SYMBOL = "0x95d89b41"
SELECTOR_DECIMALS = "0x313ce567"

BLOCKCHAIN_POLL_SECONDS = float(os.environ.get("BLOCKCHAIN_POLL_SECONDS", "3"))
TWITTER_POLL_SECONDS = 10
REQUEST_TIMEOUT = 20
LOG_LOOKBACK_BLOCKS = int(os.environ.get("LOG_LOOKBACK_BLOCKS", "5"))

WATCH_KEYWORDS = ("trump", "wlfi", "melania", "barron")

# Quote tokens to ignore when picking the "new" token side of a pool
QUOTE_TOKENS = {
    WETH.lower(),
    "0x0000000000000000000000000000000000000000",
}

seen_pools: set[str] = set()
seen_tweets: set[str] = set()
_last_scanned_block: int | None = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {message}", flush=True)


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def matches_watch_keywords(name: str, symbol: str) -> bool:
    combined = f"{name} {symbol}".lower()
    return any(keyword in combined for keyword in WATCH_KEYWORDS)


def uniswap_buy_url(token_address: str) -> str:
    return (
        f"https://app.uniswap.org/swap?chain={DEXSCREENER_CHAIN_SLUG}"
        f"&outputCurrency={token_address}"
    )


def maestro_buy_url(token_address: str) -> str:
    return f"https://t.me/MaestroSniperBot?start={token_address}-robinhood"


def topic_to_address(topic: str) -> str:
    return "0x" + topic[-40:]


def rpc_call(method: str, params: list[Any]) -> Any:
    response = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def eth_call(to: str, data: str) -> str:
    return rpc_call("eth_call", [{"to": to, "data": data}, "latest"])


def decode_string_result(raw: str) -> str:
    if not raw or raw == "0x":
        return ""
    data = bytes.fromhex(raw[2:])
    try:
        # dynamic string
        return abi_decode(["string"], data)[0]
    except Exception:
        try:
            # bytes32 style
            return data.rstrip(b"\x00").decode("utf-8", errors="ignore")
        except Exception:
            return ""


def read_token_meta(token_address: str) -> tuple[str, str]:
    name, symbol = "Unknown", "???"
    try:
        name = decode_string_result(eth_call(token_address, SELECTOR_NAME)) or name
    except Exception as exc:
        log(f"name() failed for {token_address}: {exc}")
    try:
        symbol = decode_string_result(eth_call(token_address, SELECTOR_SYMBOL)) or symbol
    except Exception as exc:
        log(f"symbol() failed for {token_address}: {exc}")
    return name, symbol


def pick_base_token(token0: str, token1: str) -> str:
    t0, t1 = token0.lower(), token1.lower()
    if t0 in QUOTE_TOKENS and t1 not in QUOTE_TOKENS:
        return token1
    if t1 in QUOTE_TOKENS and t0 not in QUOTE_TOKENS:
        return token0
    return token0


def fetch_dex_price(token_address: str) -> str | None:
    try:
        data = requests.get(
            f"{DEXSCREENER_BASE}/tokens/v1/{DEXSCREENER_CHAIN_SLUG}/{token_address}",
            timeout=REQUEST_TIMEOUT,
        )
        data.raise_for_status()
        pairs = data.json() or []
        if not pairs:
            return None
        return pairs[0].get("priceUsd")
    except Exception:
        return None


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "RobinhoodTelegramMonitor/1.0"},
    )
    response.raise_for_status()
    return feedparser.parse(response.content)


async def send_telegram(bot: Bot, message: str) -> None:
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
                disable_notification=False,
            )
        except TelegramError as exc:
            log(f"Telegram send failed (chat {chat_id}): {exc}")
        except Exception as exc:
            log(f"Unexpected Telegram error (chat {chat_id}): {exc}")


# ---------------------------------------------------------------------------
# On-chain blockchain monitoring
# ---------------------------------------------------------------------------


def get_latest_block() -> int:
    return int(rpc_call("eth_blockNumber", []), 16)


def decode_v2_pair_created(log_entry: dict[str, Any]) -> dict[str, Any]:
    token0 = topic_to_address(log_entry["topics"][1])
    token1 = topic_to_address(log_entry["topics"][2])
    data = bytes.fromhex(log_entry["data"][2:])
    pair, _ = abi_decode(["address", "uint256"], data)
    return {
        "dex": "uniswap-v2",
        "pairAddress": pair,
        "token0": token0,
        "token1": token1,
        "blockNumber": int(log_entry["blockNumber"], 16),
        "txHash": log_entry["transactionHash"],
    }


def decode_v3_pool_created(log_entry: dict[str, Any]) -> dict[str, Any]:
    token0 = topic_to_address(log_entry["topics"][1])
    token1 = topic_to_address(log_entry["topics"][2])
    data = bytes.fromhex(log_entry["data"][2:])
    _tick_spacing, pool = abi_decode(["int24", "address"], data)
    return {
        "dex": "uniswap-v3",
        "pairAddress": pool,
        "token0": token0,
        "token1": token1,
        "blockNumber": int(log_entry["blockNumber"], 16),
        "txHash": log_entry["transactionHash"],
    }


def fetch_new_pools(from_block: int, to_block: int) -> list[dict[str, Any]]:
    if from_block > to_block:
        return []

    pools: list[dict[str, Any]] = []
    base_filter = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}

    try:
        v2_logs = rpc_call(
            "eth_getLogs",
            [{**base_filter, "address": UNI_V2_FACTORY, "topics": [TOPIC_PAIR_CREATED]}],
        )
        for entry in v2_logs or []:
            pools.append(decode_v2_pair_created(entry))
    except Exception as exc:
        log(f"V2 eth_getLogs error: {exc}")

    try:
        v3_logs = rpc_call(
            "eth_getLogs",
            [{**base_filter, "address": UNI_V3_FACTORY, "topics": [TOPIC_POOL_CREATED]}],
        )
        for entry in v3_logs or []:
            pools.append(decode_v3_pool_created(entry))
    except Exception as exc:
        log(f"V3 eth_getLogs error: {exc}")

    pools.sort(key=lambda p: p["blockNumber"])
    return pools


def enrich_pool(pool: dict[str, Any]) -> dict[str, Any]:
    token_address = pick_base_token(pool["token0"], pool["token1"])
    name, symbol = read_token_meta(token_address)
    price = fetch_dex_price(token_address)
    return {
        **pool,
        "baseToken": {
            "address": token_address,
            "name": name,
            "symbol": symbol,
        },
        "priceUsd": price,
        "url": f"https://dexscreener.com/{DEXSCREENER_CHAIN_SLUG}/{pool['pairAddress']}",
        "detectedAt": datetime.now(timezone.utc).isoformat(),
    }


def format_pool_alert(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    name = base.get("name") or "Unknown"
    symbol = base.get("symbol") or "???"
    token_address = base.get("address") or "unknown"
    price_raw = pair.get("priceUsd")
    try:
        price = f"{float(price_raw):,.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        price = str(price_raw or "indexing…")

    keyword_flag = ""
    if matches_watch_keywords(name, symbol):
        keyword_flag = " ⭐ *Keyword match*"

    dex = escape_markdown(pair.get("dex") or "uniswap")
    block = pair.get("blockNumber", "?")
    tx = pair.get("txHash", "")
    explorer_tx = f"https://robinhoodchain.blockscout.com/tx/{tx}" if tx else ""

    uniswap_url = uniswap_buy_url(token_address)
    maestro_url = maestro_buy_url(token_address)
    dex_url = pair.get("url") or (
        f"https://dexscreener.com/{DEXSCREENER_CHAIN_SLUG}/{pair.get('pairAddress') or token_address}"
    )

    lines = [
        "🚨 *NEW TOKEN FOUND ON ROBINHOOD CHAIN\\!* 🚨",
        "",
        f"*Name:* {escape_markdown(name)} \\({escape_markdown(symbol)}\\){keyword_flag}",
        f"*Contract Address:* `{token_address}`",
        f"*Initial Price:* ${price}",
        f"*Source:* on\\-chain `{dex}` @ block `{block}`",
        "",
        f"📊 [View on DexScreener]({dex_url})",
        f"🛒 [Click to Buy on Uniswap Web App]({uniswap_url})",
        f"⚡ [Instant Sniper Buy via Maestro Bot]({maestro_url})",
    ]
    if explorer_tx:
        lines.append(f"🔗 [View create tx]({explorer_tx})")
    return "\n".join(lines)


async def blockchain_tracker(bot: Bot) -> None:
    global _last_scanned_block
    log(f"On-chain tracker started (RPC {RPC_URL})")

    try:
        latest = await asyncio.to_thread(get_latest_block)
        # Start at tip — do NOT backfill old pools
        _last_scanned_block = latest
        log(f"Synced to block {_last_scanned_block} (listening for new pools only)")
    except Exception as exc:
        log(f"Failed to sync initial block: {exc}")
        _last_scanned_block = None

    while True:
        try:
            latest = await asyncio.to_thread(get_latest_block)
            if _last_scanned_block is None:
                _last_scanned_block = latest
                await asyncio.sleep(BLOCKCHAIN_POLL_SECONDS)
                continue

            from_block = _last_scanned_block + 1
            # Small overlap for reorg safety
            from_block = max(from_block - LOG_LOOKBACK_BLOCKS, _last_scanned_block - LOG_LOOKBACK_BLOCKS + 1)
            if from_block > latest:
                await asyncio.sleep(BLOCKCHAIN_POLL_SECONDS)
                continue

            pools = await asyncio.to_thread(fetch_new_pools, from_block, latest)
            _last_scanned_block = latest

            for pool in pools:
                pair_address = pool["pairAddress"]
                if not pair_address or pair_address.lower() in seen_pools:
                    continue
                seen_pools.add(pair_address.lower())

                enriched = await asyncio.to_thread(enrich_pool, pool)
                alert = format_pool_alert(enriched)
                await send_telegram(bot, alert)
                base = enriched.get("baseToken") or {}
                log(
                    f"Alert sent for on-chain pool {pair_address} "
                    f"({base.get('symbol')}) block={pool['blockNumber']}"
                )
        except Exception as exc:
            log(f"Blockchain tracker loop error: {exc}")

        await asyncio.sleep(BLOCKCHAIN_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Twitter / X RSS monitoring
# ---------------------------------------------------------------------------


def extract_tweet_id(entry: Any) -> str:
    tweet_id = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if tweet_id:
        return str(tweet_id)

    link = getattr(entry, "link", "")
    match = re.search(r"/status/(\d+)", link or "")
    if match:
        return match.group(1)

    title = getattr(entry, "title", "")
    published = getattr(entry, "published", "")
    return f"{link}|{title}|{published}"


def format_tweet_alert(account_name: str, tweet_text: str, link: str) -> str:
    return (
        "🐦 *NEW TWITTER ANNOUNCEMENT\\!*\n"
        f"*Account:* {escape_markdown(account_name)}\n"
        f"*Post:* {escape_markdown(tweet_text)}\n"
        f"👉 [Read Post on X]({link})"
    )


async def process_rss_feed(bot: Bot, feed_url: str, account_name: str) -> None:
    if not feed_url:
        return

    try:
        feed = await asyncio.to_thread(fetch_rss, feed_url)
    except requests.RequestException as exc:
        log(f"RSS fetch error ({account_name}): {exc}")
        return
    except Exception as exc:
        log(f"RSS parse error ({account_name}): {exc}")
        return

    for entry in reversed(feed.entries or []):
        tweet_id = extract_tweet_id(entry)
        if tweet_id in seen_tweets:
            continue

        seen_tweets.add(tweet_id)
        tweet_text = (
            getattr(entry, "summary", None)
            or getattr(entry, "description", None)
            or getattr(entry, "title", None)
            or ""
        )
        tweet_text = re.sub(r"<[^>]+>", "", tweet_text).strip()
        link = getattr(entry, "link", "") or feed_url

        alert = format_tweet_alert(account_name, tweet_text, link)
        await send_telegram(bot, alert)
        log(f"Alert sent for new tweet from {account_name}: {tweet_id}")


async def warmup_seen_tweets() -> None:
    feeds = [
        (TRUMP_RSS_URL, "Donald Trump"),
        (MELANIA_RSS_URL, "Melania Trump"),
    ]
    for feed_url, account_name in feeds:
        if not feed_url:
            continue
        try:
            feed = await asyncio.to_thread(fetch_rss, feed_url)
            for entry in feed.entries or []:
                seen_tweets.add(extract_tweet_id(entry))
        except Exception as exc:
            log(f"Tweet warmup error ({account_name}): {exc}")
    log(f"Warmup complete: {len(seen_tweets)} existing tweets tracked.")


async def twitter_tracker(bot: Bot) -> None:
    log("Twitter tracker started (RSS.app feeds)")
    feeds = [
        (TRUMP_RSS_URL, "Donald Trump"),
        (MELANIA_RSS_URL, "Melania Trump"),
    ]

    missing = [name for url, name in feeds if not url]
    if missing:
        log(
            "WARNING: Missing RSS feed URLs for: "
            + ", ".join(missing)
            + ". Set TRUMP_RSS_URL and MELANIA_RSS_URL env vars."
        )

    while True:
        for feed_url, account_name in feeds:
            await process_rss_feed(bot, feed_url, account_name)
        await asyncio.sleep(TWITTER_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log("ERROR: TELEGRAM_BOT_TOKEN is not set.")
        sys.exit(1)
    if not TELEGRAM_CHAT_IDS:
        log("ERROR: TELEGRAM_CHAT_ID is not set.")
        sys.exit(1)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)

    try:
        me = await bot.get_me()
        log(f"Bot connected: @{me.username}")
    except TelegramError as exc:
        log(f"ERROR: Could not connect to Telegram: {exc}")
        sys.exit(1)

    await warmup_seen_tweets()

    await asyncio.gather(
        blockchain_tracker(bot),
        twitter_tracker(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Shutting down.")
