"""Tavily Search API によるWeb検索。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from news_digest.config import WebSearchConfig
from news_digest.models import Item
from news_digest.sources.base import FetchResult, FetchWindow, in_window, parse_date
from news_digest.textutil import item_id_for, strip_html

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def parse_response(data: dict[str, Any], query: str, topic: str, window: FetchWindow) -> list[Item]:
    """Tavily の応答JSONを Item に変換する。

    Args:
        data: 応答JSON。
        query: 使ったクエリ。
        topic: news / general。
        window: 期間（範囲外は捨てる）。

    Returns:
        記事のリスト。
    """
    items: list[Item] = []
    for r in data.get("results", []):
        url = str(r.get("url", ""))
        title = strip_html(str(r.get("title", "")), max_chars=300)
        if not url or not title:
            continue
        published = parse_date(r.get("published_date"))
        if not in_window(published, window):
            continue
        items.append(
            Item(
                id=item_id_for(url),
                source="web:tavily",
                source_type="news" if topic == "news" else "web",
                title=title,
                url=url,
                published_at=published,
                content=strip_html(str(r.get("content", ""))),
                query=query,
            )
        )
    return items


def search(
    client: httpx.Client,
    api_key: str,
    query: str,
    config: WebSearchConfig,
    window: FetchWindow,
) -> FetchResult:
    """1つのクエリでWeb検索する。

    Args:
        client: HTTPクライアント。
        api_key: Tavily のAPIキー。
        query: 検索クエリ。
        config: Web検索の設定。
        window: 期間（overlap 適用前）。

    Returns:
        取得結果。
    """
    win = window.with_overlap(config.overlap_days)
    payload = {
        "query": query,
        "topic": config.topic,
        "search_depth": "basic",
        "max_results": config.max_results_per_query,
        "start_date": win.since.date().isoformat(),
        "include_published_date": True,
    }
    try:
        resp = client.post(
            TAVILY_SEARCH_URL, json=payload, headers={"Authorization": f"Bearer {api_key}"}
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # 例外メッセージにヘッダは含まれないが、念のため型名だけを残す
        logger.warning("Tavily の検索に失敗: %s (%s)", query, type(e).__name__)
        return FetchResult(errors=[f"web: {query}: {type(e).__name__}"])
    items = parse_response(resp.json(), query, config.topic, win)
    logger.info("Tavily %r: %d件", query, len(items))
    return FetchResult(items=items)
