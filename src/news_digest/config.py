"""環境変数（.env）とプロファイル（profile.yaml）の読み込み。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from news_digest.models import SourceType


class Settings(BaseSettings):
    """秘密情報と実行環境の設定。値は .env から読む。

    Attributes:
        llm_fast: 安価なモデル（クエリ生成・採点）。"provider:model" 形式。
        llm_standard: 要約用のモデル。
        tavily_api_key: Tavily のAPIキー。未設定ならWeb検索をスキップする。
        slack_bot_token: Slack Bot Token（xoxb-）。フィードバック収集にも使う。
        slack_channel_id: 投稿先チャンネルID。
        slack_webhook_url: Bot Token が無い場合の簡易投稿先（フィードバック不可）。
        data_dir: SQLite などの保存先。
        output_dir: Markdownダイジェストの出力先。
        profile_path: プロファイルYAMLのパス。
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_fast: str = "anthropic:claude-haiku-5-5"
    llm_standard: str = "anthropic:claude-sonnet-5-5"
    tavily_api_key: SecretStr | None = None
    slack_bot_token: SecretStr | None = None
    slack_channel_id: str | None = None
    slack_webhook_url: SecretStr | None = None
    data_dir: Path = Path("data")
    output_dir: Path = Path("output/digests")
    profile_path: Path = Path("profile.yaml")

    @property
    def db_path(self) -> Path:
        """履歴DBのパス。

        Returns:
            SQLiteファイルのパス。
        """
        return self.data_dir / "history.sqlite3"


def load_settings(env_file: Path = Path(".env")) -> Settings:
    """.env を環境変数に読み込んでから設定を作る。

    ANTHROPIC_API_KEY など、LLMのSDKが環境変数から直接読むキーも .env に置けるようにするため。
    既に環境変数にある値は上書きしない。

    Args:
        env_file: .env のパス。

    Returns:
        設定。
    """
    load_dotenv(env_file, override=False)
    return Settings()


class FeedConfig(BaseModel):
    """RSS/Atomフィード1本の設定。

    Attributes:
        name: 表示名。
        url: フィードURL。
        type: 種別。
        max_items: 1回の取得で見る最大件数。
    """

    name: str
    url: str
    type: SourceType = "blog"
    max_items: int = 30


class ArxivConfig(BaseModel):
    """arXiv の設定。

    Attributes:
        enabled: 有効か。
        categories: 絞り込むカテゴリ（空なら全分野）。
        fixed_queries: LLMが作るクエリに加えて常に投げるクエリ。
        max_results_per_query: クエリごとの最大件数。
        overlap_days: 公開の遅れを吸収するため、前回実行時刻より何日前から取り直すか。
    """

    enabled: bool = True
    categories: list[str] = Field(default_factory=list)
    fixed_queries: list[str] = Field(default_factory=list)
    max_results_per_query: int = 30
    overlap_days: float = 3.0


class WebSearchConfig(BaseModel):
    """Web検索（Tavily）の設定。

    Attributes:
        enabled: 有効か。
        topic: Tavily の topic（news / general）。
        fixed_queries: 常に投げるクエリ。
        max_results_per_query: クエリごとの最大件数。
        overlap_days: 前回実行時刻より何日前から取り直すか。
    """

    enabled: bool = True
    topic: Literal["news", "general"] = "news"
    fixed_queries: list[str] = Field(default_factory=list)
    max_results_per_query: int = 8
    overlap_days: float = 1.0


class RssConfig(BaseModel):
    """RSSの設定。

    Attributes:
        enabled: 有効か。
        feeds: フィード一覧。
        overlap_days: 前回実行時刻より何日前から取り直すか。
    """

    enabled: bool = True
    feeds: list[FeedConfig] = Field(default_factory=list)
    overlap_days: float = 1.0


class SourcesConfig(BaseModel):
    """情報源の設定のまとめ。"""

    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    web: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ScoringWeights(BaseModel):
    """「重要」の定義（総合点の重み）。合計が1になるよう正規化する。

    Attributes:
        relevance: 関連度。
        applicability: 実務への応用性。
        impact: インパクト。
    """

    relevance: float = 0.4
    applicability: float = 0.35
    impact: float = 0.25

    @model_validator(mode="after")
    def _normalize(self) -> ScoringWeights:
        total = self.relevance + self.applicability + self.impact
        if total <= 0:
            raise ValueError("重みの合計は正である必要があります")
        self.relevance /= total
        self.applicability /= total
        self.impact /= total
        return self


class SelectionConfig(BaseModel):
    """選定の設定。

    Attributes:
        max_candidates: 事前フィルタ後にLLM採点へ回す最大件数。
        top_k: ダイジェストに載せる最大件数。
        min_score: 掲載に必要な総合点の下限（0〜100）。
        max_per_source_type: 種別ごとの上限（偏り防止）。
        honorable_mentions: 要約なしで載せる次点の件数。
    """

    max_candidates: int = 80
    top_k: int = 10
    min_score: float = 55.0
    max_per_source_type: int = 5
    honorable_mentions: int = 10


class Profile(BaseModel):
    """ユーザーのロールと興味。

    Attributes:
        role: エンジニアとしてのロール。
        context: 業務内容の補足（採点の判断材料）。
        interests: 興味のあるトピック。
        keywords: 事前フィルタに使う追加キーワード。
        exclude: 興味のないトピック（含まれる記事は除外）。
        languages: 出力言語など。
        first_run_lookback_days: 初回実行時に遡る日数。
        weights: 採点の重み。
        selection: 選定の設定。
        sources: 情報源の設定。
    """

    role: str
    context: str = ""
    interests: list[str]
    keywords: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    output_language: str = "日本語"
    first_run_lookback_days: float = 3.0
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)


def load_profile(path: Path) -> Profile:
    """プロファイルYAMLを読み込む。

    Args:
        path: YAMLファイルのパス。

    Returns:
        検証済みのプロファイル。

    Raises:
        FileNotFoundError: ファイルが無い場合。
        pydantic.ValidationError: 内容が不正な場合。
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Profile.model_validate(data)
