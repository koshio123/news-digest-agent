"""ダイジェスト生成の LangGraph グラフ。

    init_run → collect_feedback → plan_queries
        → (Send) fetch ×N  … arXiv / RSSフィードごと / Web検索クエリごとに並列
        → dedupe_new → prefilter
        → (Send) score_batch ×M  … 採点をバッチ単位で並列
        → select
        → (Send) summarize ×K  … 掲載記事ごとに並列
        → compose → deliver → save_history

依存（LLM・HTTP・DB・通知先）は Deps で注入し、テストでは偽物に差し替える。
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

import httpx
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field, field_validator

from news_digest import prompts
from news_digest.config import FeedConfig, Profile, Settings
from news_digest.delivery.markdown import JST, render_digest, write_digest
from news_digest.delivery.slack import Notifier, PostedItem
from news_digest.llm import QueryPlan, ScoreBatch, SummaryOut
from news_digest.models import FeedbackExample, Item, ScoredItem, Summary
from news_digest.processing import dedupe, prefilter, select_top, total_score
from news_digest.sources import FetchResult, FetchWindow, arxiv, rss, tavily
from news_digest.store import Delivery, HistoryStore

logger = logging.getLogger(__name__)

SCORE_BATCH_SIZE = 15
MAX_QUERIES_PER_SOURCE = 8
FEEDBACK_WINDOW_DAYS = 7.0


class ScoringFailedError(RuntimeError):
    """採点がすべて失敗した（LLMの認証エラー・障害など）。"""


class FeedbackSource(Protocol):
    """投稿へのリアクションを読む相手（Slack Bot）。"""

    def collect_reactions(self, deliveries: list[Delivery]) -> dict[str, int]:
        """リアクションを評価に変換する。

        Args:
            deliveries: 直近に投稿した記事。

        Returns:
            記事ID→評価。
        """
        ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    """グラフが使う外部依存。

    Attributes:
        profile: プロファイル。
        settings: 設定。
        store: 履歴DB。
        llm_fast: クエリ生成・採点用のモデル。
        llm_standard: 要約用のモデル。
        http: HTTPクライアント。
        notifier: Slack の通知先（無ければ投稿しない）。
        feedback_source: リアクションの読み取り元（無ければ読まない）。
        clock: 現在時刻（テスト用に差し替え可能）。
        arxiv_sleep: arXiv のリクエスト間隔。
    """

    profile: Profile
    settings: Settings
    store: HistoryStore
    llm_fast: BaseChatModel
    llm_standard: BaseChatModel
    http: httpx.Client
    notifier: Notifier | None = None
    feedback_source: FeedbackSource | None = None
    clock: Callable[[], datetime] = field(default=_utcnow)
    arxiv_sleep: float = arxiv.REQUEST_INTERVAL_SEC


class RunOptions(BaseModel):
    """実行時の指定（グラフの入力）。

    Attributes:
        since_override: 期間の開始を手動で指定する（オンデマンド実行用）。
        dry_run: True なら履歴DBを更新しない（既読にしない・実行を記録しない）。
        post_to_slack: False なら Slack に投稿しない。
    """

    since_override: datetime | None = None
    dry_run: bool = False
    post_to_slack: bool = True

    @field_validator("since_override")
    @classmethod
    def _to_utc(cls, v: datetime | None) -> datetime | None:
        # Studio から "2026-10-24" のようにタイムゾーンなしで渡されたら JST とみなす
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=JST)
        return v.astimezone(UTC)


class DigestState(RunOptions):
    """グラフの状態。

    Pydantic モデルにしているのは、LangGraph がノードに渡す前に検証・変換するため。
    Studio で状態を編集したり入力に日時文字列を渡したりすると値は JSON（dict・文字列）で届くが、
    ここで Item や datetime に戻る。並列ノードが書き込むキーには reducer（operator.add）を付ける。
    """

    run_id: int = 0
    since: datetime | None = None
    until: datetime | None = None
    feedback: list[FeedbackExample] = Field(default_factory=list)
    arxiv_queries: list[str] = Field(default_factory=list)
    web_queries: list[str] = Field(default_factory=list)
    raw_items: Annotated[list[Item], operator.add] = Field(default_factory=list)
    errors: Annotated[list[str], operator.add] = Field(default_factory=list)
    candidates: list[Item] = Field(default_factory=list)
    prefilter_scores: dict[str, float] = Field(default_factory=dict)
    scored: Annotated[list[ScoredItem], operator.add] = Field(default_factory=list)
    selected: list[ScoredItem] = Field(default_factory=list)
    mentions: list[ScoredItem] = Field(default_factory=list)
    summaries: Annotated[list[Summary], operator.add] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    digest_markdown: str = ""
    digest_path: str = ""
    posted: list[PostedItem] = Field(default_factory=list)

    def window(self) -> FetchWindow:
        """init_run で決めた期間。

        Returns:
            期間。

        Raises:
            RuntimeError: init_run より前に呼んだ場合。
        """
        if self.since is None or self.until is None:
            raise RuntimeError("期間が未設定です（init_run の前に呼ばれた）")
        return FetchWindow(since=self.since, until=self.until)


class FetchTask(BaseModel):
    """fetch ノードへの入力（Send で渡す）。"""

    kind: Literal["arxiv", "rss", "web"]
    queries: list[str] = Field(default_factory=list)
    feed: FeedConfig | None = None
    since: datetime
    until: datetime


class ScoreTask(BaseModel):
    """score_batch ノードへの入力。"""

    batch: list[Item]
    prefilter_scores: dict[str, float]
    feedback: list[FeedbackExample]


class SummarizeTask(BaseModel):
    """summarize ノードへの入力。"""

    target: ScoredItem


def _unique(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v.strip())
    return out[:limit]


def build_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """依存を束ねてグラフを組み立てる。

    Args:
        deps: 外部依存。
        checkpointer: テストでフォークや中断を試すときに渡す。
            Studio（langgraph dev）はサーバーが自前のものを使うので渡さない。

    Returns:
        コンパイル済みのグラフ。
    """
    profile = deps.profile
    store = deps.store

    # ---- 1. 実行の準備 ----

    def init_run(state: DigestState) -> dict[str, Any]:
        until = deps.clock()
        since = (
            state.since_override
            or store.last_success_until()
            or until - timedelta(days=profile.first_run_lookback_days)
        )
        run_id = 0 if state.dry_run else store.start_run(since, until)
        logger.info("対象期間: %s 〜 %s (run_id=%d)", since, until, run_id)
        return {"run_id": run_id, "since": since, "until": until}

    def collect_feedback(state: DigestState) -> dict[str, Any]:
        if deps.feedback_source is not None and not state.dry_run:
            deliveries = store.recent_deliveries(FEEDBACK_WINDOW_DAYS)
            ratings = deps.feedback_source.collect_reactions(deliveries)
            for item_id, rating in ratings.items():
                store.upsert_feedback(item_id, rating, note="", origin="slack")
            logger.info("Slack のフィードバック: %d件", len(ratings))
        return {"feedback": store.feedback_examples(limit=20)}

    def plan_queries(state: DigestState) -> dict[str, Any]:
        src = profile.sources
        try:
            planner = deps.llm_fast.with_structured_output(QueryPlan)
            plan = planner.invoke(prompts.query_plan_prompt(profile))
            assert isinstance(plan, QueryPlan)  # noqa: S101
            arxiv_q, web_q = plan.arxiv_queries, plan.web_queries
        except Exception as e:
            # クエリ生成に失敗しても、興味をそのままクエリにして続ける
            logger.warning("クエリ生成に失敗したため興味をそのまま使う: %s", e)
            arxiv_q, web_q = list(profile.interests), list(profile.interests)
        return {
            "arxiv_queries": _unique([*src.arxiv.fixed_queries, *arxiv_q], MAX_QUERIES_PER_SOURCE),
            "web_queries": _unique([*src.web.fixed_queries, *web_q], MAX_QUERIES_PER_SOURCE),
        }

    def fan_out_fetch(state: DigestState) -> list[Send] | str:
        src = profile.sources
        win = state.window()
        since, until = win.since, win.until
        tasks: list[FetchTask] = []
        if src.arxiv.enabled and state.arxiv_queries:
            tasks.append(
                FetchTask(
                    kind="arxiv",
                    queries=state.arxiv_queries,
                    feed=None,
                    since=since,
                    until=until,
                )
            )
        if src.rss.enabled:
            tasks += [
                FetchTask(kind="rss", queries=[], feed=f, since=since, until=until)
                for f in src.rss.feeds
            ]
        if src.web.enabled and deps.settings.tavily_api_key is not None:
            tasks += [
                FetchTask(kind="web", queries=[q], feed=None, since=since, until=until)
                for q in state.web_queries
            ]
        if not tasks:
            return "dedupe_new"
        return [Send("fetch", t) for t in tasks]

    # ---- 2. 取得（並列） ----

    def fetch(state: FetchTask) -> dict[str, Any]:
        # Send の引数はチェックポイントから戻ると dict のことがあるので、検証してから使う
        task = FetchTask.model_validate(state)
        window = FetchWindow(since=task.since, until=task.until)
        src = profile.sources
        try:
            if task.kind == "arxiv":
                res = arxiv.fetch_arxiv(
                    deps.http, task.queries, src.arxiv, window, sleep=deps.arxiv_sleep
                )
            elif task.kind == "rss":
                assert task.feed is not None  # noqa: S101
                res = rss.fetch_feed(
                    deps.http, task.feed, window.with_overlap(src.rss.overlap_days)
                )
            else:
                key = deps.settings.tavily_api_key
                assert key is not None  # noqa: S101
                res = tavily.search(
                    deps.http, key.get_secret_value(), task.queries[0], src.web, window
                )
        except Exception as e:
            # 1つのソースの失敗で全体を止めない
            logger.exception("取得タスクが失敗: %s", task.kind)
            res = FetchResult(errors=[f"{task.kind}: {type(e).__name__}"])
        return {"raw_items": res.items, "errors": res.errors}

    # ---- 3. 重複排除・事前フィルタ ----

    def dedupe_new(state: DigestState) -> dict[str, Any]:
        raw = state.raw_items
        unique = dedupe(raw)
        seen = store.seen_ids(it.id for it in unique)
        new = [it for it in unique if it.id not in seen]
        logger.info("取得 %d → 重複排除 %d → 新着 %d", len(raw), len(unique), len(new))
        return {"candidates": new, "stats": {"fetched": len(raw), "new": len(new)}}

    def prefilter_node(state: DigestState) -> dict[str, Any]:
        kept = prefilter(state.candidates, profile)
        scores = {it.id: s for it, s in kept}
        stats = {**state.stats, "prefiltered": len(kept)}
        return {"prefilter_scores": scores, "stats": stats}

    def fan_out_score(state: DigestState) -> list[Send] | str:
        scores = state.prefilter_scores
        items = [it for it in state.candidates if it.id in scores]
        items.sort(key=lambda it: scores[it.id], reverse=True)
        if not items:
            return "select"
        return [
            Send(
                "score_batch",
                ScoreTask(
                    batch=items[i : i + SCORE_BATCH_SIZE],
                    prefilter_scores=scores,
                    feedback=state.feedback,
                ),
            )
            for i in range(0, len(items), SCORE_BATCH_SIZE)
        ]

    # ---- 4. 採点（並列） ----

    def score_batch(state: ScoreTask) -> dict[str, Any]:
        task = ScoreTask.model_validate(state)
        batch = task.batch
        by_short = {it.short_id: it for it in batch}
        try:
            scorer = deps.llm_fast.with_structured_output(ScoreBatch)
            out = scorer.invoke(prompts.score_prompt(profile, batch, task.feedback))
            assert isinstance(out, ScoreBatch)  # noqa: S101
        except Exception as e:
            logger.exception("採点に失敗（%d件をスキップ）", len(batch))
            return {"errors": [f"score: {len(batch)}件: {type(e).__name__}"]}
        results: list[ScoredItem] = []
        for sc in out.scores:
            it = by_short.get(sc.id.strip())
            if it is None:
                continue
            results.append(
                ScoredItem(
                    item=it,
                    relevance=sc.relevance,
                    applicability=sc.applicability,
                    impact=sc.impact,
                    total=total_score(sc.relevance, sc.applicability, sc.impact, profile.weights),
                    reason=sc.reason,
                    prefilter_score=task.prefilter_scores.get(it.id, 0.0),
                )
            )
        missing = len(batch) - len(results)
        if missing:
            logger.warning("採点結果が欠けた記事: %d件", missing)
        return {"scored": results}

    # ---- 5. 選定・要約（並列） ----

    def select(state: DigestState) -> dict[str, Any]:
        scored = state.scored
        if state.prefilter_scores and not scored:
            # 全バッチ失敗のまま成功扱いにすると、未採点の記事が既読になり二度と出てこない。
            # 実行を失敗させて期間を進めず、次回に取り直す
            errs = [e for e in state.errors if e.startswith("score")]
            raise ScoringFailedError(f"採点がすべて失敗しました: {errs}")
        selected, mentions = select_top(scored, profile.selection)
        stats = {**state.stats, "scored": len(scored), "selected": len(selected)}
        return {"selected": selected, "mentions": mentions, "stats": stats}

    def fan_out_summarize(state: DigestState) -> list[Send] | str:
        if not state.selected:
            return "compose"
        return [Send("summarize", SummarizeTask(target=s)) for s in state.selected]

    def summarize(state: SummarizeTask) -> dict[str, Any]:
        s = SummarizeTask.model_validate(state).target
        try:
            writer = deps.llm_standard.with_structured_output(SummaryOut)
            out = writer.invoke(prompts.summary_prompt(profile, s.item, s.reason))
            assert isinstance(out, SummaryOut)  # noqa: S101
        except Exception as e:
            logger.exception("要約に失敗: %s", s.item.title)
            return {"errors": [f"summarize: {s.item.short_id}: {type(e).__name__}"]}
        return {"summaries": [Summary(item_id=s.item.id, **out.model_dump())]}

    # ---- 6. 出力 ----

    def compose(state: DigestState) -> dict[str, Any]:
        win = state.window()
        summaries = {sm.item_id: sm for sm in state.summaries}
        md = render_digest(
            since=win.since,
            until=win.until,
            selected=state.selected,
            summaries=summaries,
            mentions=state.mentions,
            stats=state.stats,
            errors=state.errors,
        )
        path = write_digest(md, deps.settings.output_dir, win.until)
        logger.info("ダイジェストを保存: %s", path)
        return {"digest_markdown": md, "digest_path": str(path)}

    def deliver(state: DigestState) -> dict[str, Any]:
        if deps.notifier is None or not state.post_to_slack:
            return {"posted": []}
        summaries = {sm.item_id: sm for sm in state.summaries}
        try:
            win = state.window()
            posted = deps.notifier.post(
                win.since, win.until, state.selected, summaries, state.digest_path
            )
        except Exception as e:
            # Slack が落ちていても Markdown は残っているので、実行は成功扱いにする
            logger.exception("Slack への投稿に失敗")
            return {"posted": [], "errors": [f"slack: {type(e).__name__}"]}
        return {"posted": posted}

    def save_history(state: DigestState) -> dict[str, Any]:
        if state.dry_run:
            return {}
        run_id = state.run_id
        selected_ids = {s.item.id for s in state.selected}
        scored = state.scored
        scored_ids = {s.item.id for s in scored}
        # 採点に回したのに一部のバッチ失敗で採点されなかった記事は既読にしない（次回に取り直す）
        sent = state.prefilter_scores
        to_save = [it for it in state.candidates if it.id not in sent or it.id in scored_ids]
        store.save_items(run_id, to_save, scored, selected_ids)
        for p in state.posted:
            store.record_delivery(p.item_id, run_id, p.channel, p.ts)
        store.finish_run(run_id, "success", state.digest_path, dict(state.stats))
        return {}

    g = StateGraph(DigestState)
    g.add_node("init_run", init_run)
    g.add_node("collect_feedback", collect_feedback)
    g.add_node("plan_queries", plan_queries)
    g.add_node("fetch", fetch)  # Send で FetchTask を受け取る
    g.add_node("dedupe_new", dedupe_new)
    g.add_node("prefilter", prefilter_node)
    g.add_node("score_batch", score_batch)
    g.add_node("select", select)
    g.add_node("summarize", summarize)
    g.add_node("compose", compose)
    g.add_node("deliver", deliver)
    g.add_node("save_history", save_history)

    g.add_edge(START, "init_run")
    g.add_edge("init_run", "collect_feedback")
    g.add_edge("collect_feedback", "plan_queries")
    g.add_conditional_edges("plan_queries", fan_out_fetch, ["fetch", "dedupe_new"])
    g.add_edge("fetch", "dedupe_new")
    g.add_edge("dedupe_new", "prefilter")
    g.add_conditional_edges("prefilter", fan_out_score, ["score_batch", "select"])
    g.add_edge("score_batch", "select")
    g.add_conditional_edges("select", fan_out_summarize, ["summarize", "compose"])
    g.add_edge("summarize", "compose")
    g.add_edge("compose", "deliver")
    g.add_edge("deliver", "save_history")
    g.add_edge("save_history", END)
    return g.compile(checkpointer=checkpointer)
