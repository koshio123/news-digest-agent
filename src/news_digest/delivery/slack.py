"""Slack への投稿と、リアクションによるフィードバック収集。

- Bot Token がある場合: 見出しメッセージ＋記事ごとに1メッセージを投稿する。
  記事ごとに分けるのは、👍 / 👎 リアクションを記事単位のフィードバックとして読むため。
- Webhook だけの場合: 1メッセージにまとめて投稿する（フィードバックは読めない）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.webhook import WebhookClient

from news_digest.delivery.markdown import SECTION_TITLES, fmt_jst, score_line
from news_digest.models import ScoredItem, Summary
from news_digest.store import Delivery

logger = logging.getLogger(__name__)

UP_REACTIONS = {"+1", "thumbsup"}
DOWN_REACTIONS = {"-1", "thumbsdown"}
POST_INTERVAL_SEC = 1.1  # chat.postMessage はチャンネルあたり約1件/秒


@dataclass(frozen=True)
class PostedItem:
    """投稿した記事のメッセージ。

    Attributes:
        item_id: 記事ID。
        channel: チャンネルID。
        ts: メッセージのタイムスタンプ。
    """

    item_id: str
    channel: str
    ts: str


class Notifier(Protocol):
    """ダイジェストの通知先。"""

    supports_feedback: bool

    def post(
        self,
        since: datetime,
        until: datetime,
        selected: list[ScoredItem],
        summaries: Mapping[str, Summary],
        markdown_path: str,
    ) -> list[PostedItem]:
        """ダイジェストを投稿する。

        Args:
            since: 期間の開始。
            until: 期間の終了。
            selected: 掲載する記事。
            summaries: 記事ID→要約。
            markdown_path: 保存したMarkdownのパス。

        Returns:
            投稿した記事のメッセージ（フィードバック収集用）。
        """
        ...


def escape(text: str) -> str:
    """Slack mrkdwn の制御文字をエスケープする。

    Args:
        text: 元の文字列。

    Returns:
        エスケープ済みの文字列。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def item_blocks(s: ScoredItem, sm: Summary | None) -> list[dict[str, Any]]:
    """記事1件分の Block Kit。

    Args:
        s: 採点済みの記事。
        sm: 要約。

    Returns:
        ブロックのリスト。
    """
    it = s.item
    headline = escape(sm.headline if sm else it.title)
    body = f"*<{it.url}|{headline}>*\n"
    if sm:
        body += f"{escape(sm.summary)}\n*なぜ重要か*: {escape(sm.why_it_matters)}"
        if sm.action:
            body += f"\n*次の一手*: {escape(sm.action)}"
    else:
        body += escape(s.reason)
    kind = SECTION_TITLES[it.source_type]
    meta = f"{kind} · {escape(it.source)} · {score_line(s)} · ID `{it.short_id}`"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": meta}]},
    ]


def header_text(since: datetime, until: datetime, n: int) -> str:
    """見出しメッセージの本文。

    Args:
        since: 期間の開始。
        until: 期間の終了。
        n: 掲載件数。

    Returns:
        mrkdwn 文字列。
    """
    return (
        f":newspaper: *Tech Digest {fmt_jst(until)[:10]}* — {n}件\n"
        f"対象期間: {fmt_jst(since)} 〜 {fmt_jst(until)}（JST）\n"
        "役に立った記事には :+1:、不要な記事には :-1: を付けてください（次回の採点に反映されます）"
    )


class SlackBotNotifier:
    """Bot Token で記事ごとに投稿する。"""

    supports_feedback = True

    def __init__(self, token: str, channel: str, interval: float = POST_INTERVAL_SEC) -> None:
        """初期化する。

        Args:
            token: Bot Token（xoxb-）。
            channel: チャンネルID。
            interval: 投稿間隔（秒）。
        """
        self._client = WebClient(token=token)
        self._channel = channel
        self._interval = interval

    def post(
        self,
        since: datetime,
        until: datetime,
        selected: list[ScoredItem],
        summaries: Mapping[str, Summary],
        markdown_path: str,
    ) -> list[PostedItem]:
        """見出し＋記事ごとのメッセージを投稿する。

        Args:
            since: 期間の開始。
            until: 期間の終了。
            selected: 掲載する記事。
            summaries: 記事ID→要約。
            markdown_path: 保存したMarkdownのパス。

        Returns:
            投稿した記事のメッセージ。
        """
        text = header_text(since, until, len(selected))
        self._client.chat_postMessage(channel=self._channel, text=text, unfurl_links=False)
        posted: list[PostedItem] = []
        for s in selected:
            time.sleep(self._interval)
            resp = self._client.chat_postMessage(
                channel=self._channel,
                text=summaries[s.item.id].headline if s.item.id in summaries else s.item.title,
                blocks=item_blocks(s, summaries.get(s.item.id)),
                unfurl_links=False,
                unfurl_media=False,
            )
            posted.append(PostedItem(s.item.id, str(resp["channel"]), str(resp["ts"])))
        return posted

    def collect_reactions(self, deliveries: list[Delivery]) -> dict[str, int]:
        """投稿へのリアクションを読み、記事ごとの評価にする。

        Args:
            deliveries: 直近に投稿した記事。

        Returns:
            記事ID→評価（+1 / -1）。リアクションが無い記事は含めない。
        """
        ratings: dict[str, int] = {}
        for d in deliveries:
            try:
                resp = self._client.reactions_get(channel=d.channel, timestamp=d.ts)
            except SlackApiError as e:
                logger.warning("リアクションの取得に失敗: %s", e.response.get("error"))
                continue
            message = resp.get("message") or {}
            rating = rating_from_reactions(message.get("reactions", []))
            if rating != 0:
                ratings[d.item_id] = rating
        return ratings


def rating_from_reactions(reactions: list[dict[str, Any]]) -> int:
    """リアクション一覧から評価を決める。

    Args:
        reactions: Slack の reactions 配列。

    Returns:
        +1 / -1。どちらも無い、または同数なら 0。
    """
    up = sum(int(r.get("count", 0)) for r in reactions if r.get("name") in UP_REACTIONS)
    down = sum(int(r.get("count", 0)) for r in reactions if r.get("name") in DOWN_REACTIONS)
    if up > down:
        return 1
    if down > up:
        return -1
    return 0


class SlackWebhookNotifier:
    """Incoming Webhook で1メッセージにまとめて投稿する（フィードバックは読めない）。"""

    supports_feedback = False

    def __init__(self, url: str) -> None:
        """初期化する。

        Args:
            url: Incoming Webhook のURL。
        """
        self._client = WebhookClient(url)

    def post(
        self,
        since: datetime,
        until: datetime,
        selected: list[ScoredItem],
        summaries: Mapping[str, Summary],
        markdown_path: str,
    ) -> list[PostedItem]:
        """1メッセージにまとめて投稿する。

        Args:
            since: 期間の開始。
            until: 期間の終了。
            selected: 掲載する記事。
            summaries: 記事ID→要約。
            markdown_path: 保存したMarkdownのパス。

        Returns:
            空リスト（リアクションを読めないため）。
        """
        blocks: list[dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text(since, until, len(selected))},
            }
        ]
        for s in selected[:20]:  # Block Kit は1メッセージ50ブロックまで
            blocks += [*item_blocks(s, summaries.get(s.item.id)), {"type": "divider"}]
        resp = self._client.send(text=f"Tech Digest {fmt_jst(until)[:10]}", blocks=blocks)
        if resp.status_code != 200:
            logger.warning("Webhook への投稿に失敗: %s", resp.status_code)
        return []
