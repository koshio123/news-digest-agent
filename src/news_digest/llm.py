"""LLM の生成とLLMの出力スキーマ。

モデルは "provider:model" 形式で .env から切り替える（例: "anthropic:claude-haiku-5-5",
"ollama:qwen3:8b"）。ノードはここで作ったモデルだけを使い、SDKを直接呼ばない。
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field


def make_chat_model(spec: str) -> BaseChatModel:
    """ "provider:model" からチャットモデルを作る。

    Args:
        spec: 例 "anthropic:claude-haiku-5-5"、"ollama:qwen3:8b"。

    Returns:
        チャットモデル。
    """
    provider, _, model = spec.partition(":")
    if provider == "anthropic":
        # 新しい Claude モデルは temperature を指定すると 400 になる
        return init_chat_model(model, model_provider=provider)
    return init_chat_model(model, model_provider=provider, temperature=0)


class QueryPlan(BaseModel):
    """プロファイルから作る検索クエリ。"""

    arxiv_queries: list[str] = Field(
        description="arXiv用の英語キーワード句（2〜4語、引用符なし）。最大6個。", max_length=8
    )
    web_queries: list[str] = Field(
        description="ニュース・ブログ検索用のクエリ（英語または日本語）。最大6個。", max_length=8
    )


class ItemScore(BaseModel):
    """1件の採点結果。"""

    id: str = Field(description="入力で与えた記事のID（8文字）")
    relevance: int = Field(ge=1, le=5, description="関連度 1〜5")
    applicability: int = Field(ge=1, le=5, description="実務への応用性 1〜5")
    impact: int = Field(ge=1, le=5, description="インパクト 1〜5")
    reason: str = Field(description="採点理由（日本語1文、60字以内）")


class ScoreBatch(BaseModel):
    """採点結果のまとまり。"""

    scores: list[ItemScore]


class SummaryOut(BaseModel):
    """1件の要約。"""

    headline: str = Field(description="内容が一目で分かる見出し（40字以内）")
    summary: str = Field(description="何が新しいのか・何をしたのかを2〜3文で")
    why_it_matters: str = Field(description="ユーザーのロールにとってなぜ重要か（1〜2文）")
    action: str = Field(default="", description="試すこと・追うべきこと（無ければ空文字）")
    tags: list[str] = Field(default_factory=list, description="短いタグ（最大4個）", max_length=6)
