"""arXiv API（Atom）からの論文取得。"""

from __future__ import annotations

import logging
import time

import feedparser
import httpx

from news_digest.config import ArxivConfig
from news_digest.models import Item
from news_digest.sources.base import FetchResult, FetchWindow, in_window, struct_time_to_dt
from news_digest.textutil import item_id_for, strip_html

logger = logging.getLogger(__name__)

ARXIV_API = "https://export.arxiv.org/api/query"
# arXiv の利用規約: 連続リクエストは3秒以上あける
REQUEST_INTERVAL_SEC = 3.0


def to_search_query(query: str) -> str:
    """キーワード句を arXiv の検索式にする。

    "abs:" や "cat:" などのフィールド指定を含む場合は、検索式とみなしてそのまま使う。

    Args:
        query: キーワード句、または arXiv の検索式。

    Returns:
        arXiv の検索式。
    """
    if ":" in query:
        return query
    phrase = query.replace('"', "").strip()
    return f'all:"{phrase}"'


def build_query(query: str, categories: list[str], window: FetchWindow) -> str:
    """カテゴリと投稿日時の条件を付けた検索式を作る。

    Args:
        query: キーワード句、または検索式。
        categories: 絞り込むカテゴリ。
        window: 投稿日時の範囲。

    Returns:
        search_query パラメータの値。
    """
    parts = [f"({to_search_query(query)})"]
    if categories:
        parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    since = window.since.strftime("%Y%m%d%H%M")
    until = window.until.strftime("%Y%m%d%H%M")
    parts.append(f"submittedDate:[{since} TO {until}]")
    return " AND ".join(parts)


def parse_feed(body: bytes, query: str, window: FetchWindow) -> list[Item]:
    """arXiv API の Atom 応答を Item に変換する。

    Args:
        body: 応答本文。
        query: 使ったクエリ（記録用）。
        window: 期間（範囲外は捨てる）。

    Returns:
        論文のリスト。
    """
    parsed = feedparser.parse(body)
    items: list[Item] = []
    for entry in parsed.entries:
        url = str(entry.get("id", ""))
        title = strip_html(str(entry.get("title", "")), max_chars=300)
        if not url or not title:
            continue
        published = struct_time_to_dt(entry.get("published_parsed"))
        if not in_window(published, window):
            continue
        authors = [str(a.get("name", "")) for a in entry.get("authors", [])][:8]
        items.append(
            Item(
                id=item_id_for(url),
                source="arxiv",
                source_type="paper",
                title=title,
                url=url.replace("http://", "https://"),
                published_at=published,
                content=strip_html(str(entry.get("summary", "")), max_chars=2000),
                authors=authors,
                query=query,
            )
        )
    return items


def fetch_arxiv(
    client: httpx.Client,
    queries: list[str],
    config: ArxivConfig,
    window: FetchWindow,
    sleep: float = REQUEST_INTERVAL_SEC,
) -> FetchResult:
    """複数のクエリで arXiv を検索する（レート制限のため逐次実行）。

    Args:
        client: HTTPクライアント。
        queries: キーワード句または検索式のリスト。
        config: arXiv の設定。
        window: 期間（overlap 適用前）。
        sleep: リクエスト間隔（秒）。

    Returns:
        取得結果。
    """
    result = FetchResult()
    win = window.with_overlap(config.overlap_days)
    for i, q in enumerate(queries):
        if i > 0 and sleep > 0:
            time.sleep(sleep)
        params = {
            "search_query": build_query(q, config.categories, win),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(config.max_results_per_query),
        }
        try:
            resp = client.get(ARXIV_API, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("arXiv の取得に失敗: %s (%s)", q, e)
            result.errors.append(f"arxiv: {q}: {type(e).__name__}")
            continue
        found = parse_feed(resp.content, q, win)
        logger.info("arXiv %r: %d件", q, len(found))
        result.items.extend(found)
    return result
