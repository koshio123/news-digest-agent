"""実行履歴・既読・配信・フィードバックを保存する SQLite ストア。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from news_digest.models import FeedbackExample, Item, ScoredItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    window_since TEXT NOT NULL,
    window_until TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | success | failed
    finished_at TEXT,
    digest_path TEXT,
    stats_json TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    first_seen_run_id INTEGER NOT NULL REFERENCES runs(id),
    total_score REAL,
    selected INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    item_id TEXT NOT NULL REFERENCES items(id),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (channel, ts)
);
CREATE TABLE IF NOT EXISTS feedback (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    rating INTEGER NOT NULL,         -- +1 / -1
    note TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,            -- slack | cli
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Delivery:
    """Slack に投稿した記事。

    Attributes:
        item_id: 記事ID。
        channel: チャンネルID。
        ts: メッセージのタイムスタンプ。
    """

    item_id: str
    channel: str
    ts: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class HistoryStore:
    """SQLite に履歴を保存する。"""

    def __init__(self, path: Path) -> None:
        """DBを開き、スキーマを作る。

        Args:
            path: SQLiteファイルのパス（":memory:" も可）。
        """
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        # LangGraph は同じ superstep のノードを別スレッドで実行するので、スレッド間で共有する
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        """接続を閉じる。"""
        self._conn.close()

    # ---- 実行 ----

    def last_success_until(self) -> datetime | None:
        """最後に成功した実行の期間終了時刻（＝次回の開始時刻）。

        Returns:
            時刻。成功した実行が無ければ None。
        """
        row = self._conn.execute(
            "SELECT window_until FROM runs WHERE status = 'success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return datetime.fromisoformat(row["window_until"]) if row else None

    def start_run(self, since: datetime, until: datetime) -> int:
        """実行を記録する。

        Args:
            since: 期間の開始。
            until: 期間の終了。

        Returns:
            実行ID。
        """
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, window_since, window_until, status)"
                " VALUES (?, ?, ?, 'running')",
                (_now(), since.isoformat(), until.isoformat()),
            )
        assert cur.lastrowid is not None  # noqa: S101
        return cur.lastrowid

    def finish_run(
        self, run_id: int, status: str, digest_path: str | None, stats: dict[str, object]
    ) -> None:
        """実行の結果を記録する。

        Args:
            run_id: 実行ID。
            status: success / failed。
            digest_path: 出力したMarkdownのパス。
            stats: 件数などの統計。
        """
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, digest_path = ?, stats_json = ?"
                " WHERE id = ?",
                (status, _now(), digest_path, json.dumps(stats, ensure_ascii=False), run_id),
            )

    def fail_running_runs(self) -> int:
        """途中で止まった実行を failed にする（次回の期間計算から外すため）。

        Returns:
            更新した件数。
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'",
                (_now(),),
            )
        return cur.rowcount

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        """最近の実行。

        Args:
            limit: 最大件数。

        Returns:
            runs の行（新しい順）。
        """
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- 既読 ----

    def seen_ids(self, ids: Iterable[str]) -> set[str]:
        """過去の実行で処理済みのIDを返す。

        Args:
            ids: 調べるID。

        Returns:
            処理済みのIDの集合。
        """
        id_list = list(ids)
        seen: set[str] = set()
        for i in range(0, len(id_list), 500):
            chunk = id_list[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._conn.execute(f"SELECT id FROM items WHERE id IN ({marks})", chunk)  # noqa: S608
            seen.update(r["id"] for r in rows)
        return seen

    def save_items(
        self,
        run_id: int,
        items: list[Item],
        scored: list[ScoredItem],
        selected_ids: set[str],
    ) -> None:
        """今回処理した記事を既読として保存する。

        Args:
            run_id: 実行ID。
            items: 重複排除後の全候補（採点されなかったものも含む）。
            scored: 採点結果。
            selected_ids: ダイジェストに載せたID。
        """
        scores = {s.item.id: s.total for s in scored}
        rows = [
            (
                it.id,
                it.source,
                it.source_type,
                it.title,
                it.url,
                it.published_at.isoformat() if it.published_at else None,
                run_id,
                scores.get(it.id),
                1 if it.id in selected_ids else 0,
            )
            for it in items
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO items (id, source, source_type, title, url, published_at,"
                " first_seen_run_id, total_score, selected) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    # ---- 配信 ----

    def record_delivery(self, item_id: str, run_id: int, channel: str, ts: str) -> None:
        """Slack への投稿を記録する（後でリアクションを読むため）。

        Args:
            item_id: 記事ID。
            run_id: 実行ID。
            channel: チャンネルID。
            ts: メッセージのタイムスタンプ。
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO deliveries (item_id, run_id, channel, ts, delivered_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (item_id, run_id, channel, ts, _now()),
            )

    def recent_deliveries(self, days: float) -> list[Delivery]:
        """直近に投稿した記事。

        Args:
            days: 何日前まで遡るか。

        Returns:
            投稿のリスト。
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT item_id, channel, ts FROM deliveries WHERE delivered_at >= ?", (since,)
        )
        return [Delivery(r["item_id"], r["channel"], r["ts"]) for r in rows]

    # ---- フィードバック ----

    def upsert_feedback(self, item_id: str, rating: int, note: str, origin: str) -> None:
        """フィードバックを保存する（同じ記事は上書き）。

        Args:
            item_id: 記事ID。
            rating: +1 / -1。0 なら削除する。
            note: コメント。
            origin: slack / cli。
        """
        with self._conn:
            if rating == 0:
                self._conn.execute("DELETE FROM feedback WHERE item_id = ?", (item_id,))
                return
            self._conn.execute(
                "INSERT INTO feedback (item_id, rating, note, origin, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(item_id) DO UPDATE SET rating = excluded.rating,"
                " note = CASE WHEN excluded.note != '' THEN excluded.note ELSE feedback.note END,"
                " origin = excluded.origin, updated_at = excluded.updated_at",
                (item_id, 1 if rating > 0 else -1, note, origin, _now()),
            )

    def feedback_examples(self, limit: int = 20) -> list[FeedbackExample]:
        """採点プロンプトに渡す最近のフィードバック。

        Args:
            limit: 最大件数。

        Returns:
            フィードバックのリスト（新しい順）。
        """
        rows = self._conn.execute(
            "SELECT i.title, i.source, f.rating, f.note FROM feedback f"
            " JOIN items i ON i.id = f.item_id ORDER BY f.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [
            FeedbackExample(
                title=r["title"], source=r["source"], rating=r["rating"], note=r["note"]
            )
            for r in rows
        ]

    def resolve_item_id(self, prefix: str) -> str | None:
        """短縮IDから記事IDを引く。

        Args:
            prefix: IDの先頭部分（ダイジェストに表示した8文字）。

        Returns:
            一意に決まれば記事ID、そうでなければ None。
        """
        rows = self._conn.execute(
            "SELECT id FROM items WHERE id LIKE ? LIMIT 2", (prefix.lower() + "%",)
        ).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None
