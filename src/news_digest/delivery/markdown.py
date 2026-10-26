"""Markdownダイジェストの組み立てと保存。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_digest.models import ScoredItem, Summary

JST = ZoneInfo("Asia/Tokyo")
SECTION_TITLES = {
    "paper": "📄 論文",
    "news": "📰 ニュース",
    "blog": "✍️ ブログ",
    "sns": "💬 SNS・コミュニティ",
    "web": "🌐 Web",
}


def fmt_jst(dt: datetime) -> str:
    """日本時間で表示する。

    Args:
        dt: タイムゾーン付きの日時。

    Returns:
        "YYYY-MM-DD HH:MM" 形式の文字列。
    """
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def score_line(s: ScoredItem) -> str:
    """採点の内訳を1行で表す。

    Args:
        s: 採点済みの記事。

    Returns:
        例 "総合 78（関連 5 / 応用 4 / 影響 3）"。
    """
    return f"総合 {s.total:.0f}（関連 {s.relevance} / 応用 {s.applicability} / 影響 {s.impact}）"


def render_digest(
    since: datetime,
    until: datetime,
    selected: list[ScoredItem],
    summaries: Mapping[str, Summary],
    mentions: list[ScoredItem],
    stats: Mapping[str, int],
    errors: list[str],
) -> str:
    """ダイジェストのMarkdownを作る。

    Args:
        since: 期間の開始。
        until: 期間の終了。
        selected: 掲載する記事（総合点の高い順）。
        summaries: 記事ID→要約。
        mentions: 次点の記事。
        stats: 件数の統計。
        errors: 取得に失敗したソース。

    Returns:
        Markdown文字列。
    """
    out: list[str] = [
        f"# Tech Digest {fmt_jst(until)[:10]}",
        "",
        f"- 対象期間: {fmt_jst(since)} 〜 {fmt_jst(until)}（JST）",
        f"- 取得 {stats.get('fetched', 0)} 件 → 新着 {stats.get('new', 0)} 件"
        f" → 採点 {stats.get('scored', 0)} 件 → 掲載 {len(selected)} 件",
        "",
    ]
    if not selected:
        out += ["今回は基準を超える記事はありませんでした。", ""]
    else:
        out += ["## TL;DR", ""]
        for s in selected[:3]:
            sm = summaries.get(s.item.id)
            out.append(f"- **{sm.headline if sm else s.item.title}** — {s.reason}")
        out.append("")
        for stype, title in SECTION_TITLES.items():
            group = [s for s in selected if s.item.source_type == stype]
            if not group:
                continue
            out += [f"## {title}", ""]
            for s in group:
                out += _render_item(s, summaries.get(s.item.id))
    if mentions:
        out += ["## 次点", ""]
        out += [
            f"- [{m.item.title}]({m.item.url}) — {m.total:.0f}点 · {m.item.source}"
            f" · `{m.item.short_id}`"
            for m in mentions
        ]
        out.append("")
    out += [
        "---",
        "",
        "フィードバック: Slackで 👍 / 👎 を付けるか、"
        "`uv run digest feedback <ID> up|down [--note ...]` を実行してください。",
    ]
    if errors:
        out += ["", "<details><summary>取得エラー</summary>", ""]
        out += [f"- {e}" for e in errors]
        out += ["", "</details>"]
    return "\n".join(out) + "\n"


def _render_item(s: ScoredItem, sm: Summary | None) -> list[str]:
    it = s.item
    date = fmt_jst(it.published_at)[:10] if it.published_at else "日付不明"
    lines = [f"### [{sm.headline if sm else it.title}]({it.url})", ""]
    if sm and sm.headline != it.title:
        lines += [f"*{it.title}*", ""]
    lines += [f"`{it.short_id}` · {it.source} · {date} · {score_line(s)}", ""]
    if sm:
        lines += [sm.summary, "", f"**なぜ重要か**: {sm.why_it_matters}", ""]
        if sm.action:
            lines += [f"**次の一手**: {sm.action}", ""]
        if sm.tags:
            lines += [" ".join(f"#{t}" for t in sm.tags), ""]
    else:
        lines += [s.reason, ""]
    return lines


def write_digest(markdown: str, output_dir: Path, until: datetime) -> Path:
    """Markdownをファイルに保存する。

    Args:
        markdown: 本文。
        output_dir: 出力先ディレクトリ。
        until: 期間の終了（ファイル名に使う）。

    Returns:
        保存したファイルのパス。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{until.astimezone(JST).strftime('%Y-%m-%d_%H%M')}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
