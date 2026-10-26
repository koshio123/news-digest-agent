"""グラフ全体の結合テスト（偽LLM・偽HTTP）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from news_digest import graph as graph_mod
from news_digest.config import Profile
from news_digest.delivery.slack import PostedItem
from news_digest.graph import Deps, ScoringFailedError, build_graph
from news_digest.llm import ScoreBatch
from news_digest.models import Item, ScoredItem, Summary
from news_digest.store import Delivery
from news_digest.textutil import item_id_for
from tests.conftest import NOW, FakeLLM


class FakeSlack:
    """投稿を記録し、指定の評価をリアクションとして返す。"""

    supports_feedback = True

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.ratings: dict[str, int] = {}
        self.asked: list[Delivery] = []

    def post(
        self,
        since: datetime,
        until: datetime,
        selected: list[ScoredItem],
        summaries: Mapping[str, Summary],
        markdown_path: str,
    ) -> list[PostedItem]:
        self.posted += [s.item.title for s in selected]
        return [PostedItem(s.item.id, "C1", f"{i}.0") for i, s in enumerate(selected)]

    def collect_reactions(self, deliveries: list[Delivery]) -> dict[str, int]:
        self.asked += deliveries
        return {d.item_id: self.ratings[d.item_id] for d in deliveries if d.item_id in self.ratings}


def test_first_run_builds_digest_and_marks_seen(make_deps: Callable[..., Deps]) -> None:
    slack = FakeSlack()
    deps = make_deps(notifier=slack, feedback_source=slack)
    out: dict[str, Any] = build_graph(deps).invoke({})

    # 初回は first_run_lookback_days 遡る
    assert out["since"] == NOW - timedelta(days=3)
    titles = [s.item.title for s in out["selected"]]
    assert "Learning-Based V2G Scheduling for Residential Virtual Power Plants" in titles
    assert "Utility launches V2G program with 500 EVs" in titles
    assert all("NFT" not in t and "smartphone" not in t for t in titles)
    md = out["digest_markdown"]
    assert "## 📄 論文" in md and "要約見出し" in md
    assert slack.posted == titles
    assert deps.store.last_success_until() == NOW


def test_second_run_excludes_previous_items_and_uses_feedback(
    make_deps: Callable[..., Deps],
) -> None:
    slack = FakeSlack()
    first = make_deps(notifier=slack, feedback_source=slack)
    out1: dict[str, Any] = build_graph(first).invoke({})
    liked = out1["selected"][0].item
    slack.ratings[liked.id] = 1

    later = NOW + timedelta(days=1)
    llm = FakeLLM()
    second = make_deps(
        store=first.store,
        notifier=slack,
        feedback_source=slack,
        clock=lambda: later,
        llm_fast=llm,
        llm_standard=llm,
    )
    out2: dict[str, Any] = build_graph(second).invoke({})

    # 期間は前回の終了時刻から
    assert out2["since"] == NOW
    # 同じ記事が返ってきても、前回処理済みなので候補に入らない
    assert out2["candidates"] == []
    assert out2["selected"] == []
    assert "基準を超える記事はありませんでした" in out2["digest_markdown"]
    # Slack のリアクションが保存され、プロファイル外の好みとして次回のプロンプトに入る
    ex = second.store.feedback_examples()
    assert [(e.title, e.rating) for e in ex] == [(liked.title, 1)]


def test_feedback_reaches_score_prompt(make_deps: Callable[..., Deps]) -> None:
    llm = FakeLLM()
    deps = make_deps(llm_fast=llm, llm_standard=llm)
    # 過去の実行で「不要」と評価した記事を用意する
    past = Item(
        id=item_id_for("https://old.example.com/a"),
        source="rss:Test",
        source_type="news",
        title="抽象的なV2Gの未来予想",
        url="https://old.example.com/a",
    )
    run_id = deps.store.start_run(NOW - timedelta(days=5), NOW - timedelta(days=3))
    deps.store.save_items(run_id, [past], [], {past.id})
    deps.store.finish_run(run_id, "success", None, {})
    deps.store.upsert_feedback(past.id, -1, "抽象的すぎる", "cli")

    build_graph(deps).invoke({"dry_run": True})
    prompt = llm.prompts["ScoreBatch"][0]
    assert "不要だった:" in prompt
    assert "抽象的なV2Gの未来予想（抽象的すぎる）" in prompt


def test_dry_run_does_not_touch_history(make_deps: Callable[..., Deps]) -> None:
    deps = make_deps()
    out: dict[str, Any] = build_graph(deps).invoke({"dry_run": True, "post_to_slack": False})
    assert out["selected"]
    assert deps.store.last_success_until() is None
    assert deps.store.seen_ids(s.item.id for s in out["selected"]) == set()


def test_since_override(make_deps: Callable[..., Deps]) -> None:
    deps = make_deps()
    since = NOW - timedelta(hours=6)
    out: dict[str, Any] = build_graph(deps).invoke({"since_override": since, "dry_run": True})
    assert out["since"] == since


class FlakyLLM(FakeLLM):
    """採点を最初の fail_times 回だけ失敗させる偽LLM。"""

    def __init__(self, fail_times: int) -> None:
        super().__init__()
        self.fail_times = fail_times

    def with_structured_output(self, schema: type[BaseModel]) -> Any:
        inner = super().with_structured_output(schema)
        if schema is not ScoreBatch:
            return inner
        outer = self

        class _R:
            def invoke(self, prompt: str) -> BaseModel:
                if outer.fail_times > 0:
                    outer.fail_times -= 1
                    raise RuntimeError("LLM down")
                return inner.invoke(prompt)

        return _R()


def test_all_scoring_failed_fails_run_without_marking_seen(
    make_deps: Callable[..., Deps],
) -> None:
    llm = FlakyLLM(fail_times=100)
    deps = make_deps(llm_fast=llm, llm_standard=llm)
    with pytest.raises(ScoringFailedError):
        build_graph(deps).invoke({})
    assert deps.store.fail_running_runs() == 1
    assert deps.store.last_success_until() is None
    assert deps.store.recent_runs()[0]["status"] == "failed"


def test_partially_failed_batch_is_not_marked_seen(
    make_deps: Callable[..., Deps], profile: Profile
) -> None:
    graph_mod.SCORE_BATCH_SIZE = 1  # 1件ずつのバッチにして、1つだけ失敗させる
    try:
        llm = FlakyLLM(fail_times=1)
        deps = make_deps(llm_fast=llm, llm_standard=llm)
        out: dict[str, Any] = build_graph(deps).invoke({})
    finally:
        graph_mod.SCORE_BATCH_SIZE = 15
    sent = set(out["prefilter_scores"])
    scored = {s.item.id for s in out["scored"]}
    unscored = sent - scored
    assert len(unscored) == 1
    assert deps.store.seen_ids(sent) == scored  # 未採点の1件は既読にしない
    assert any(e.startswith("score") for e in out["errors"])
