"""テスト共通の偽物（LLM・HTTP）とフィクスチャ。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from news_digest.config import FeedConfig, Profile, Settings
from news_digest.graph import Deps
from news_digest.llm import QueryPlan, ScoreBatch, SummaryOut
from news_digest.store import HistoryStore

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)


class _Runnable:
    def __init__(self, fn: Callable[[str], BaseModel]) -> None:
        self._fn = fn

    def invoke(self, prompt: str) -> BaseModel:
        return self._fn(prompt)


class FakeLLM:
    """with_structured_output だけを実装した偽LLM。受け取ったプロンプトを記録する。"""

    def __init__(self) -> None:
        self.prompts: dict[str, list[str]] = {}

    def with_structured_output(self, schema: type[BaseModel]) -> _Runnable:
        def respond(prompt: str) -> BaseModel:
            self.prompts.setdefault(schema.__name__, []).append(prompt)
            if schema is QueryPlan:
                return QueryPlan(arxiv_queries=["vehicle-to-grid"], web_queries=["V2G news"])
            if schema is ScoreBatch:
                scores = []
                for m in re.finditer(r'<item id="(\w+)"[^>]*>\ntitle: (.*)', prompt):
                    hot = "V2G" in m.group(2)
                    scores.append(
                        {
                            "id": m.group(1),
                            "relevance": 5 if hot else 2,
                            "applicability": 5 if hot else 2,
                            "impact": 4 if hot else 1,
                            "reason": "V2Gの実装に直結" if hot else "関連が薄い",
                        }
                    )
                return ScoreBatch.model_validate({"scores": scores})
            if schema is SummaryOut:
                return SummaryOut(
                    headline="要約見出し",
                    summary="何が新しいか。",
                    why_it_matters="業務に効く。",
                    tags=["V2G"],
                )
            raise AssertionError(schema)

        return _Runnable(respond)


def mock_transport() -> httpx.MockTransport:
    """arXiv・RSS・Tavily を返す偽のHTTP。"""
    arxiv_body = (FIXTURES / "arxiv.xml").read_bytes()
    feed_body = (FIXTURES / "feed.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "export.arxiv.org":
            return httpx.Response(200, content=arxiv_body)
        if host == "feeds.example.com":
            return httpx.Response(200, content=feed_body)
        if host == "api.tavily.com":
            body = json.loads(request.content)
            assert request.headers["Authorization"] == "Bearer tvly-test"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": f"Web result for {body['query']}: V2G pilot in Japan",
                            "url": "https://news.example.jp/v2g-pilot",
                            "content": "国内でV2Gの実証が始まった。",
                            "published_date": "2026-10-24T12:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def profile() -> Profile:
    """テスト用のプロファイル。"""
    p = Profile(
        role="EVエネマネのバックエンドエンジニア",
        interests=["V2G", "EV charging"],
        keywords=["vehicle-to-grid"],
        exclude=["NFT"],
        first_run_lookback_days=3,
    )
    p.sources.rss.feeds = [
        FeedConfig(name="Test", url="https://feeds.example.com/rss", type="news")
    ]
    p.selection.min_score = 50
    return p


@pytest.fixture
def make_deps(tmp_path: Path, profile: Profile) -> Callable[..., Deps]:
    """Deps を作る関数（clock や feedback_source を差し替えられる）。"""

    def _make(**overrides: Any) -> Deps:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            tavily_api_key="tvly-test",  # type: ignore[arg-type]
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "out",
        )
        llm = FakeLLM()
        base: dict[str, Any] = {
            "profile": profile,
            "settings": settings,
            "store": HistoryStore(settings.db_path),
            "llm_fast": cast(BaseChatModel, llm),
            "llm_standard": cast(BaseChatModel, llm),
            "http": httpx.Client(transport=mock_transport()),
            "clock": lambda: NOW,
            "arxiv_sleep": 0.0,
        }
        base.update(overrides)
        return Deps(**base)

    return _make
