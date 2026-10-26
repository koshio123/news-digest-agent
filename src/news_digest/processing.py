"""LLMを使わない処理: 重複排除・事前フィルタ・選定。"""

from __future__ import annotations

import re
from collections import Counter
from functools import cache

from news_digest.config import Profile, ScoringWeights, SelectionConfig
from news_digest.models import Item, ScoredItem
from news_digest.textutil import jaccard, tokens

# 同じ話題が複数ソースに出たとき、どれを代表に残すか（小さいほど優先）
_SOURCE_PRIORITY = {"paper": 0, "blog": 1, "news": 2, "web": 3, "sns": 4}
TITLE_DUP_THRESHOLD = 0.8


def dedupe(items: list[Item]) -> list[Item]:
    """URL（ID）とタイトルの類似度で重複を除く。

    Args:
        items: 全ソースの取得結果。

    Returns:
        重複を除いた記事。順序は代表の優先度順。
    """
    by_id: dict[str, Item] = {}
    for it in items:
        cur = by_id.get(it.id)
        if cur is None or len(it.content) > len(cur.content):
            by_id[it.id] = it
    ordered = sorted(by_id.values(), key=lambda x: _SOURCE_PRIORITY[x.source_type])
    kept: list[tuple[Item, set[str]]] = []
    for it in ordered:
        tk = tokens(it.title)
        if any(jaccard(tk, other) >= TITLE_DUP_THRESHOLD for _, other in kept):
            continue
        kept.append((it, tk))
    return [it for it, _ in kept]


@cache
def _term_pattern(term: str) -> re.Pattern[str]:
    t = re.escape(term.lower())
    if term.isascii():
        # 英語は単語境界で照合する（"EV" が "Neuron" や "review" に一致しないように）
        return re.compile(rf"(?<![a-z0-9]){t}(?![a-z0-9])")
    return re.compile(t)


def _contains(text: str, term: str) -> bool:
    return _term_pattern(term).search(text) is not None


def prefilter(items: list[Item], profile: Profile) -> list[tuple[Item, float]]:
    """キーワードによる安価な足切り。除外語を含むものを落とし、上位を残す。

    検索系ソース（arXiv・Web検索）はクエリ自体が興味から作られているので底上げする。
    RSS は興味と無関係な記事も多いので、キーワードに一致しないものは落とす。

    Args:
        items: 重複排除後の記事。
        profile: プロファイル。

    Returns:
        (記事, 事前スコア) のリスト。スコアの高い順で、最大 max_candidates 件。
    """
    terms = [t for t in [*profile.interests, *profile.keywords] if t.strip()]
    scored: list[tuple[Item, float]] = []
    for it in items:
        title = it.title.lower()
        body = it.content.lower()
        if any(_contains(title, ex) or _contains(body, ex) for ex in profile.exclude):
            continue
        score = 0.0
        for t in terms:
            if _contains(title, t):
                score += 2.0
            elif _contains(body, t):
                score += 1.0
        if it.query is not None:
            score += 1.5
        if score <= 0:
            continue
        scored.append((it, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: profile.selection.max_candidates]


def total_score(relevance: int, applicability: int, impact: int, w: ScoringWeights) -> float:
    """1〜5の3軸から0〜100の総合点を計算する。

    Args:
        relevance: 関連度。
        applicability: 実務への応用性。
        impact: インパクト。
        w: 重み（正規化済み）。

    Returns:
        総合点（0〜100）。
    """
    weighted = w.relevance * relevance + w.applicability * applicability + w.impact * impact
    return round((weighted - 1.0) / 4.0 * 100.0, 1)


def select_top(
    scored: list[ScoredItem], cfg: SelectionConfig
) -> tuple[list[ScoredItem], list[ScoredItem]]:
    """ダイジェストに載せる記事と次点を選ぶ。

    Args:
        scored: 採点済みの記事。
        cfg: 選定の設定。

    Returns:
        (掲載する記事, 次点の記事)。
    """
    ranked = sorted(scored, key=lambda s: (s.total, s.prefilter_score), reverse=True)
    selected: list[ScoredItem] = []
    rest: list[ScoredItem] = []
    per_type: Counter[str] = Counter()
    for s in ranked:
        ok = (
            len(selected) < cfg.top_k
            and s.total >= cfg.min_score
            and per_type[s.item.source_type] < cfg.max_per_source_type
        )
        if ok:
            selected.append(s)
            per_type[s.item.source_type] += 1
        else:
            rest.append(s)
    return selected, rest[: cfg.honorable_mentions]
