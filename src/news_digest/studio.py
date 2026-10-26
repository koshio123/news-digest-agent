"""LangGraph Studio（`langgraph dev`）用のエントリーポイント。

`make_graph` はグラフのファクトリで、サーバーは実行（run）のたびに呼ぶ。そのため
`profile.yaml` や `.env` を書き換えると、サーバーを再起動しなくても次の実行に反映される
（`langgraph dev` の自動リロードは .py の変更しか見ない）。

履歴DB・出力先は本番と同じものを使う。試すときは入力に
{"dry_run": true, "post_to_slack": false} を指定する。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from news_digest.app import make_deps
from news_digest.config import load_settings
from news_digest.graph import Deps, build_graph

logger = logging.getLogger(__name__)


def _load() -> Deps:
    return make_deps(load_settings())


def _close(deps: Deps) -> None:
    deps.http.close()
    deps.store.close()


@asynccontextmanager
async def make_graph(
    config: RunnableConfig,
) -> AsyncIterator[CompiledStateGraph[Any, Any, Any, Any]]:
    """実行ごとに設定とプロファイルを読み直してグラフを作る。

    サーバーはイベントループ上でファクトリを呼び、ブロッキングI/Oを検出するとエラーにするので、
    ファイル・DBを開く処理は別スレッドで行う。

    Args:
        config: サーバーから渡される実行設定（未使用）。

    Yields:
        コンパイル済みのグラフ。実行が終わるとHTTPクライアントとDBを閉じる。
    """
    deps = await asyncio.to_thread(_load)
    logger.info("プロファイルを読み込みました: %s", deps.settings.profile_path)
    try:
        yield build_graph(deps)
    finally:
        await asyncio.to_thread(_close, deps)
