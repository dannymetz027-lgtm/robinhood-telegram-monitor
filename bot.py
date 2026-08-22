#!/usr/bin/env python3
"""
Robinhood Chain + Twitter (X) Telegram Monitor

Runs two concurrent asyncio loops:
  - Blockchain tracker (DexScreener, every 5s)
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
# Comma-separated chat IDs supported
TELEGRAM_CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("TELEGRAM_CHAT_ID", "7585957774,8638097560").split(",")
    if cid.strip()
]

# RSS.app feed URLs
TRUMP_RSS_URL = os.environ.get(
    "TRUMP_RSS_URL",
    "https://rss.app/feeds/psYBJMrD9XMwxbnu.xml",
)
MELANIA_RSS_URL = os.environ.get(
    "MELANIA_RSS_URL",
    "https://rss.app/feeds/rv81p1DmwkN3NvkG.xml",
)

DEXSCREENER_BASE = "https://api.dexscreener.com"
ROBINHOOD_CHAIN_ID = "4663"  # EIP-155 chain ID; DexScreener also uses slug "robinhood"
DEXSCREENER_CHAIN_SLUG = "robinhood"

BLOCKCHAIN_POLL_SECONDS = 5
TWITTER_POLL_SECONDS = 10
REQUEST_TIMEOUT = 15

SEARCH_QUERIES = ["robinhood", "trump", "WLFI", "melania", "barron"]
WATCH_KEYWORDS = ("trump", "wlfi", "melania", "barron")

# In-memory deduplication sets
seen_pools: set[str] = set()
seen_tweets: set[str] = set()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {message}", flush=True)


def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    if not text:
        return ""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def is_robinhood_chain(chain_id: Any) -> bool:
    """Strict Robinhood Chain filter (chain ID 4663 / DexScreener slug)."""
    value = str(chain_id or "").strip().lower()
    return value == ROBINHOOD_CHAIN_ID or value == DEXSCREENER_CHAIN_SLUG


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


def fetch_json(url: str, params: dict | None = None) -> Any:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


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
# Blockchain monitoring
# ---------------------------------------------------------------------------


def collect_robinhood_pairs() -> list[dict[str, Any]]:
    """Poll DexScreener search API and return unique Robinhood Chain pairs."""
    pairs_by_address: dict[str, dict[str, Any]] = {}

    for query in SEARCH_QUERIES:
        try:
            data = fetch_json(f"{DEXSCREENER_BASE}/latest/dex/search", params={"q": query})
        except requests.RequestException as exc:
            log(f"DexScreener search error (q={query!r}): {exc}")
            continue
        except ValueError as exc:
            log(f"DexScreener JSON decode error (q={query!r}): {exc}")
            continue

        for pair in data.get("pairs") or []:
            if not is_robinhood_chain(pair.get("chainId")):
                continue
            pair_address = pair.get("pairAddress")
            if pair_address:
                pairs_by_address[pair_address] = pair

    # Also check latest token profiles for brand-new listings
    try:
        profiles = fetch_json(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
        for profile in profiles or []:
            if not is_robinhood_chain(profile.get("chainId")):
                continue
            token_address = profile.get("tokenAddress")
            if not token_address:
                continue
            try:
                token_data = fetch_json(
                    f"{DEXSCREENER_BASE}/token-pairs/v1/{DEXSCREENER_CHAIN_SLUG}/{token_address}"
                )
            except requests.RequestException as exc:
                log(f"Token-pairs error ({token_address}): {exc}")
                continue

            for pair in token_data or []:
                if not is_robinhood_chain(pair.get("chainId")):
                    continue
                pair_address = pair.get("pairAddress")
                if pair_address:
                    pairs_by_address[pair_address] = pair
    except requests.RequestException as exc:
        log(f"DexScreener token-profiles error: {exc}")
    except ValueError as exc:
        log(f"DexScreener token-profiles JSON error: {exc}")

    return list(pairs_by_address.values())


def format_pool_alert(pair: dict[str, Any]) -> str:
    base = pair.get("baseToken") or {}
    name = base.get("name") or "Unknown"
    symbol = base.get("symbol") or "???"
    token_address = base.get("address") or "unknown"
    price_raw = pair.get("priceUsd")
    try:
        price = f"{float(price_raw):,.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        price = str(price_raw or "N/A")

    keyword_flag = ""
    if matches_watch_keywords(name, symbol):
        keyword_flag = " ⭐ *Keyword match*"

    uniswap_url = uniswap_buy_url(token_address)
    maestro_url = maestro_buy_url(token_address)

    return (
        "🚨 *NEW TOKEN FOUND ON ROBINHOOD CHAIN\\!* 🚨\n\n"
        f"*Name:* {escape_markdown(name)} \\({escape_markdown(symbol)}\\){keyword_flag}\n"
        f"*Contract Address:* `{token_address}`\n"
        f"*Initial Price:* ${price}\n\n"
        f"🛒 [Click to Buy on Uniswap Web App]({uniswap_url})\n"
        f"⚡ [Instant Sniper Buy via Maestro Bot]({maestro_url})"
    )


async def warmup_seen_pools() -> None:
    """Seed seen_pools on startup so only future launches trigger alerts."""
    try:
        pairs = await asyncio.to_thread(collect_robinhood_pairs)
        for pair in pairs:
            pair_address = pair.get("pairAddress")
            if pair_address:
                seen_pools.add(pair_address)
        log(f"Warmup complete: {len(seen_pools)} existing pools tracked.")
    except Exception as exc:
        log(f"Warmup error (continuing anyway): {exc}")


async def warmup_seen_tweets() -> None:
    """Seed seen_tweets on startup so only future posts trigger alerts."""
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


async def blockchain_tracker(bot: Bot) -> None:
    log("Blockchain tracker started (DexScreener / Robinhood Chain)")
    while True:
        try:
            pairs = await asyncio.to_thread(collect_robinhood_pairs)
            for pair in pairs:
                pair_address = pair.get("pairAddress")
                if not pair_address or pair_address in seen_pools:
                    continue

                seen_pools.add(pair_address)
                alert = format_pool_alert(pair)
                await send_telegram(bot, alert)
                log(f"Alert sent for new pool: {pair_address}")
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

    await asyncio.gather(
        warmup_seen_pools(),
        warmup_seen_tweets(),
    )

    await asyncio.gather(
        blockchain_tracker(bot),
        twitter_tracker(bot),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Shutting down.")
