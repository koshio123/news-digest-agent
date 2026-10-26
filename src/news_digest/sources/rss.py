"""RSS/Atom フィードからの取得。"""

from __future__ import annotations

import logging

import feedparser
import httpx

from news_digest.config import FeedConfig
from news_digest.models import Item
from news_digest.sources.base import FetchResult, FetchWindow, in_window, struct_time_to_dt
from news_digest.textutil import item_id_for, strip_html

logger = logging.getLogger(__name__)


def parse_feed(body: bytes, feed: FeedConfig, window: FetchWindow) -> list[Item]:
    """フィード本文を Item に変換する。

    Args:
        body: フィードのXML。
        feed: フィード設定。
        window: 期間（範囲外は捨てる）。

    Returns:
        記事のリスト。
    """
    parsed = feedparser.parse(body)
    items: list[Item] = []
    for entry in parsed.entries[: feed.max_items]:
        url = str(entry.get("link", ""))
        title = strip_html(str(entry.get("title", "")), max_chars=300)
        if not url or not title:
            continue
        published = struct_time_to_dt(entry.get("published_parsed") or entry.get("updated_parsed"))
        if not in_window(published, window):
            continue
        raw = ""
        if entry.get("content"):
            raw = str(entry["content"][0].get("value", ""))
        raw = raw or str(entry.get("summary", ""))
        author = str(entry.get("author", ""))
        items.append(
            Item(
                id=item_id_for(url),
                source=f"rss:{feed.name}",
                source_type=feed.type,
                title=title,
                url=url,
                published_at=published,
                content=strip_html(raw),
                authors=[author] if author else [],
            )
        )
    return items


def fetch_feed(client: httpx.Client, feed: FeedConfig, window: FetchWindow) -> FetchResult:
    """フィードを1本取得する。

    Args:
        client: HTTPクライアント。
        feed: フィード設定。
        window: 期間（overlap 適用済み）。

    Returns:
        取得結果。
    """
    try:
        resp = client.get(feed.url)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("RSS の取得に失敗: %s (%s)", feed.name, e)
        return FetchResult(errors=[f"rss: {feed.name}: {type(e).__name__}"])
    items = parse_feed(resp.content, feed, window)
    logger.info("RSS %s: %d件", feed.name, len(items))
    return FetchResult(items=items)
