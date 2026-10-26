"""情報源で共通の型とヘルパー。"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from news_digest.models import Item

USER_AGENT = "news-digest-agent/0.1 (+personal use)"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(frozen=True)
class FetchWindow:
    """取得対象の期間（前回実行時刻〜今）。

    Attributes:
        since: 前回の成功した実行の終了時刻（初回は now - lookback）。
        until: 今回の実行開始時刻。
    """

    since: datetime
    until: datetime

    def with_overlap(self, days: float) -> FetchWindow:
        """公開の遅れを吸収するため開始を前にずらした期間を返す。

        前回分の重複は既読DBで除くので、ここは広めに取ってよい。

        Args:
            days: ずらす日数。

        Returns:
            開始時刻をずらした期間。
        """
        return FetchWindow(since=self.since - timedelta(days=days), until=self.until)


@dataclass
class FetchResult:
    """1つの取得タスクの結果。

    Attributes:
        items: 取得した記事。
        errors: 失敗したクエリ・フィードの説明（処理は続行する）。
    """

    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def make_client() -> httpx.Client:
    """共通設定のHTTPクライアントを作る。

    Returns:
        httpx.Client。
    """
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )


def struct_time_to_dt(value: time.struct_time | None) -> datetime | None:
    """feedparser の struct_time（UTC）を datetime に変換する。

    Args:
        value: feedparser の *_parsed。

    Returns:
        UTC の datetime。None なら None。
    """
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)


def parse_date(value: str | None) -> datetime | None:
    """ISO 8601 / RFC 2822 の日付文字列を UTC の datetime にする。

    Args:
        value: 日付文字列。

    Returns:
        UTC の datetime。解釈できなければ None。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def in_window(published_at: datetime | None, window: FetchWindow) -> bool:
    """期間内か判定する。日付不明は既読DBに任せて通す。

    Args:
        published_at: 公開日時。
        window: 期間。

    Returns:
        期間内（または日付不明）なら True。
    """
    if published_at is None:
        return True
    return window.since <= published_at <= window.until + timedelta(hours=1)
