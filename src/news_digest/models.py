"""アプリ全体で使うデータモデル。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["paper", "news", "blog", "sns", "web"]


class Item(BaseModel):
    """情報源から取得した1件の記事・論文・投稿。

    Attributes:
        id: 正規化URLから作る安定ID（既読管理のキー）。
        source: 取得元の名前（例: "arxiv", "rss:Hacker News", "web:tavily"）。
        source_type: 種別（論文/ニュース/ブログ/SNS/Web）。
        title: タイトル。
        url: 元のURL。
        published_at: 公開日時（UTC）。取得できない場合は None。
        content: 本文の抜粋・アブストラクト。
        authors: 著者。
        query: 検索系ソースで使ったクエリ。
    """

    id: str
    source: str
    source_type: SourceType
    title: str
    url: str
    published_at: datetime | None = None
    content: str = ""
    authors: list[str] = Field(default_factory=list)
    query: str | None = None

    @property
    def short_id(self) -> str:
        """フィードバック用の短いID（先頭8文字）。

        Returns:
            短縮ID。
        """
        return self.id[:8]


class ScoredItem(BaseModel):
    """ルーブリックで採点された記事。

    Attributes:
        item: 対象の記事。
        relevance: 関連度（1〜5）。
        applicability: 実務への応用性（1〜5）。
        impact: インパクト（1〜5）。
        total: 重み付き総合点（0〜100）。
        reason: 採点理由（日本語1文）。
        prefilter_score: 事前フィルタの点数。
    """

    item: Item
    relevance: int
    applicability: int
    impact: int
    total: float
    reason: str
    prefilter_score: float = 0.0


class Summary(BaseModel):
    """選ばれた記事の要約。

    Attributes:
        item_id: 対象記事のID。
        headline: 日本語の見出し。
        summary: 何が新しいか（2〜3文）。
        why_it_matters: 自分のロールにとってなぜ重要か。
        action: 試すこと・追うべきこと（任意）。
        tags: タグ。
    """

    item_id: str
    headline: str
    summary: str
    why_it_matters: str
    action: str = ""
    tags: list[str] = Field(default_factory=list)


class FeedbackExample(BaseModel):
    """採点プロンプトに渡す過去のフィードバック。

    Attributes:
        title: 記事タイトル。
        source: 取得元。
        rating: +1（良い）/ -1（不要）。
        note: 任意のコメント。
    """

    title: str
    source: str
    rating: int
    note: str = ""
