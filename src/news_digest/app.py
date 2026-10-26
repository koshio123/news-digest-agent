"""設定から依存を組み立てて、グラフを実行する。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from news_digest.config import Settings, load_profile
from news_digest.delivery.slack import Notifier, SlackBotNotifier, SlackWebhookNotifier
from news_digest.graph import Deps, FeedbackSource, RunOptions, build_graph
from news_digest.llm import make_chat_model
from news_digest.sources.base import make_client
from news_digest.store import HistoryStore

logger = logging.getLogger(__name__)

# 並列ノード（取得・採点・要約）の同時実行数。LLMのレート制限に合わせて調整する
MAX_CONCURRENCY = 8


def make_deps(settings: Settings) -> Deps:
    """設定から依存を作る。

    Args:
        settings: 設定。

    Returns:
        グラフの依存。
    """
    profile = load_profile(settings.profile_path)
    notifier: Notifier | None = None
    feedback_source: FeedbackSource | None = None
    if settings.slack_bot_token is not None and settings.slack_channel_id:
        bot = SlackBotNotifier(
            settings.slack_bot_token.get_secret_value(), settings.slack_channel_id
        )
        notifier, feedback_source = bot, bot
    elif settings.slack_webhook_url is not None:
        notifier = SlackWebhookNotifier(settings.slack_webhook_url.get_secret_value())
    else:
        logger.info("Slack が未設定のため、Markdown のみ出力します")
    if settings.tavily_api_key is None:
        logger.info("TAVILY_API_KEY が未設定のため、Web検索はスキップします")
    return Deps(
        profile=profile,
        settings=settings,
        store=HistoryStore(settings.db_path),
        llm_fast=make_chat_model(settings.llm_fast),
        llm_standard=make_chat_model(settings.llm_standard),
        http=make_client(),
        notifier=notifier,
        feedback_source=feedback_source,
    )


def run_digest(
    settings: Settings,
    since: datetime | None = None,
    dry_run: bool = False,
    post_to_slack: bool = True,
) -> dict[str, Any]:
    """ダイジェストを1回生成する。

    Args:
        settings: 設定。
        since: 期間の開始を手動指定する場合の日時。
        dry_run: True なら履歴DBを更新しない。
        post_to_slack: False なら Slack に投稿しない。

    Returns:
        グラフの最終状態。

    Raises:
        Exception: グラフの実行に失敗した場合（実行は failed として記録する）。
    """
    deps = make_deps(settings)
    graph = build_graph(deps)
    inputs = RunOptions(since_override=since, dry_run=dry_run, post_to_slack=post_to_slack)
    try:
        return graph.invoke(inputs.model_dump(), config={"max_concurrency": MAX_CONCURRENCY})
    except Exception:
        n = deps.store.fail_running_runs()
        logger.exception("実行に失敗しました（failed として記録: %d件）", n)
        raise
    finally:
        deps.http.close()
        deps.store.close()
