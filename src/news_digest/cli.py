"""コマンドラインのエントリーポイント（`uv run digest ...`）。"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

import typer

from news_digest.app import run_digest
from news_digest.config import load_settings
from news_digest.delivery.markdown import JST
from news_digest.store import HistoryStore

app = typer.Typer(help="ロールと興味に合わせた最新情報ダイジェスト", no_args_is_help=True)
logger = logging.getLogger("news_digest")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx は INFO でURLを出す。キーをクエリに載せるAPIを足したときの漏えいを防ぐ
    # 新しい版は httpx2 / httpcore2 の名前で出すので両方を抑える
    for name in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(UTC)


@app.command()
def run(
    since: Annotated[
        str | None,
        typer.Option(help="期間の開始（例 2026-10-20 / 2026-10-20T09:00）。省略時は前回実行から"),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="履歴を更新しない（既読にしない）")
    ] = False,
    no_slack: Annotated[bool, typer.Option("--no-slack", help="Slackに投稿しない")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """ダイジェストを生成して、Markdown 出力と Slack 投稿を行う。

    Args:
        since: 期間の開始。
        dry_run: 履歴を更新しない。
        no_slack: Slack に投稿しない。
        verbose: 詳細ログ。
    """
    _setup_logging(verbose)
    state = run_digest(
        load_settings(), since=_parse_since(since), dry_run=dry_run, post_to_slack=not no_slack
    )
    stats = state.get("stats", {})
    logger.info("完了: %s (%s)", state.get("digest_path"), stats)
    typer.echo(state.get("digest_path", ""))


class Rating(StrEnum):
    """フィードバックの種類。"""

    up = "up"
    down = "down"
    clear = "clear"


@app.command()
def feedback(
    item_id: Annotated[str, typer.Argument(help="ダイジェストに表示された8文字のID")],
    rating: Annotated[Rating, typer.Argument(help="up / down / clear")],
    note: Annotated[str, typer.Option(help="理由などのメモ（採点時の参考になる）")] = "",
) -> None:
    """記事にフィードバックを付ける（Slack のリアクションと同じ扱い）。

    Args:
        item_id: 記事の短縮ID。
        rating: 評価。
        note: メモ。

    Raises:
        typer.Exit: IDが見つからない場合。
    """
    _setup_logging(False)
    store = HistoryStore(load_settings().db_path)
    full = store.resolve_item_id(item_id)
    if full is None:
        typer.echo(f"ID {item_id} の記事が見つからないか、一意に決まりません", err=True)
        raise typer.Exit(code=1)
    value = {Rating.up: 1, Rating.down: -1, Rating.clear: 0}[rating]
    store.upsert_feedback(full, value, note, origin="cli")
    typer.echo(f"{item_id}: {rating.value}")


@app.command()
def history(limit: Annotated[int, typer.Option(help="表示件数")] = 10) -> None:
    """最近の実行履歴を表示する。

    Args:
        limit: 表示件数。
    """
    store = HistoryStore(load_settings().db_path)
    for r in store.recent_runs(limit):
        typer.echo(
            f"#{r['id']} {r['status']:<8} {r['window_since'][:16]} → {r['window_until'][:16]}"
            f"  {r['digest_path'] or ''}"
        )
