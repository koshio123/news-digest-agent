"""LLMを使わない部分の単体テスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_digest.config import FeedConfig, Profile, ScoringWeights, SelectionConfig
from news_digest.delivery.slack import escape, rating_from_reactions
from news_digest.models import Item, ScoredItem
from news_digest.processing import dedupe, prefilter, select_top, total_score
from news_digest.sources import arxiv, rss
from news_digest.sources.base import FetchWindow
from news_digest.store import HistoryStore
from news_digest.textutil import item_id_for, normalize_url

FIX = Path(__file__).parent / "fixtures"
WINDOW = FetchWindow(
    since=datetime(2026, 10, 23, tzinfo=UTC), until=datetime(2026, 10, 25, tzinfo=UTC)
)


def _item(title: str, url: str, stype: str = "news", content: str = "", query: str | None = None):
    return Item(
        id=item_id_for(url),
        source="test",
        source_type=stype,  # type: ignore[arg-type]
        title=title,
        url=url,
        content=content,
        query=query,
    )


def test_normalize_url_arxiv_versions_are_same() -> None:
    assert normalize_url("http://arxiv.org/abs/2610.01234v2") == "arxiv:2610.01234"
    assert normalize_url("https://arxiv.org/pdf/2610.01234v1") == "arxiv:2610.01234"


def test_normalize_url_drops_tracking_and_trailing_slash() -> None:
    a = normalize_url("https://www.Example.com/a/?utm_source=x&id=1#top")
    assert a == normalize_url("http://example.com/a?id=1")


def test_arxiv_build_query_has_categories_and_window() -> None:
    q = arxiv.build_query("vehicle-to-grid", ["eess.SY", "cs.LG"], WINDOW)
    assert q == (
        '(all:"vehicle-to-grid") AND (cat:eess.SY OR cat:cs.LG)'
        " AND submittedDate:[202610230000 TO 202610250000]"
    )
    assert arxiv.to_search_query('abs:"V2G" AND cat:eess.SY') == 'abs:"V2G" AND cat:eess.SY'


def test_arxiv_parse_feed_filters_window() -> None:
    items = arxiv.parse_feed((FIX / "arxiv.xml").read_bytes(), "v2g", WINDOW)
    assert len(items) == 1
    it = items[0]
    assert it.title == "Learning-Based V2G Scheduling for Residential Virtual Power Plants"
    assert it.source_type == "paper"
    assert it.url.startswith("https://")
    assert it.authors == ["Alice Example", "Bob Example"]


def test_rss_parse_feed_strips_html_and_filters_old() -> None:
    feed = FeedConfig(name="T", url="https://x", type="news")
    items = rss.parse_feed((FIX / "feed.xml").read_bytes(), feed, WINDOW)
    titles = [i.title for i in items]
    assert "Old EV charging story" not in titles
    v2g = next(i for i in items if "V2G" in i.title)
    assert v2g.content == "A utility started a vehicle-to-grid program for demand response."


def test_dedupe_by_url_and_title() -> None:
    a = _item("Big V2G launch announced", "https://a.com/x?utm_source=1")
    b = _item("Big V2G launch announced", "https://a.com/x")
    c = _item("Big V2G Launch Announced!", "https://b.com/y", stype="sns")
    d = _item("Unrelated", "https://c.com/z")
    out = dedupe([a, b, c, d])
    assert len(out) == 2
    assert {o.source_type for o in out} == {"news"}


def test_prefilter_excludes_and_requires_keyword() -> None:
    profile = Profile(role="r", interests=["V2G"], exclude=["NFT"])
    items = [
        _item("V2G pilot", "https://a/1"),
        _item("V2G NFT", "https://a/2"),
        _item("Phone review", "https://a/3"),
        _item("Search hit", "https://a/4", query="q"),
    ]
    kept = [i.title for i, _ in prefilter(items, profile)]
    assert kept == ["V2G pilot", "Search hit"]


def test_prefilter_matches_ascii_terms_on_word_boundary() -> None:
    profile = Profile(role="r", interests=["EV", "充電"])
    items = [
        _item("AWS Neuron review", "https://a/1"),
        _item("New EV launched", "https://a/2"),
        _item("EV-ready homes", "https://a/3"),
        _item("急速充電器の新製品", "https://a/4"),
    ]
    kept = {i.title for i, _ in prefilter(items, profile)}
    assert kept == {"New EV launched", "EV-ready homes", "急速充電器の新製品"}


def test_total_score_range() -> None:
    w = ScoringWeights()
    assert total_score(5, 5, 5, w) == 100.0
    assert total_score(1, 1, 1, w) == 0.0


def test_select_top_respects_min_and_type_cap() -> None:
    def s(title: str, total: float, stype: str = "news") -> ScoredItem:
        return ScoredItem(
            item=_item(title, f"https://x/{title}", stype=stype),
            relevance=3,
            applicability=3,
            impact=3,
            total=total,
            reason="",
        )

    cfg = SelectionConfig(top_k=3, min_score=50, max_per_source_type=1, honorable_mentions=5)
    selected, mentions = select_top(
        [s("a", 90), s("b", 80), s("c", 70, "paper"), s("d", 40, "blog")], cfg
    )
    assert [x.item.title for x in selected] == ["a", "c"]
    assert [x.item.title for x in mentions] == ["b", "d"]


def test_store_window_seen_and_feedback(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite3")
    assert store.last_success_until() is None
    until = datetime(2026, 10, 25, tzinfo=UTC)
    run_id = store.start_run(until - timedelta(days=1), until)
    it = _item("V2G", "https://a/1")
    store.save_items(run_id, [it], [], {it.id})
    assert store.last_success_until() is None  # 成功するまでは次回の開始時刻にしない
    store.finish_run(run_id, "success", None, {})
    assert store.last_success_until() == until
    assert store.seen_ids([it.id, "other"]) == {it.id}
    assert store.resolve_item_id(it.short_id) == it.id
    store.upsert_feedback(it.id, 1, "良い", "cli")
    store.upsert_feedback(it.id, -1, "", "slack")  # メモは残る
    ex = store.feedback_examples()
    assert (ex[0].rating, ex[0].note) == (-1, "良い")


def test_slack_rating_and_escape() -> None:
    assert rating_from_reactions([{"name": "+1", "count": 2}, {"name": "-1", "count": 1}]) == 1
    assert rating_from_reactions([{"name": "thumbsdown", "count": 1}]) == -1
    assert rating_from_reactions([{"name": "eyes", "count": 3}]) == 0
    assert escape("a<b>&c") == "a&lt;b&gt;&amp;c"
