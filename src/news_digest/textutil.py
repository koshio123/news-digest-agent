"""URL正規化・HTML除去・タイトル類似度などの小さな関数群。"""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "mc_", "fbclid", "gclid", "ref", "source", "cmpid")
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[぀-ヿ一-鿿]")


def normalize_url(url: str) -> str:
    """同じ記事を同じ文字列に揃えるためのURL正規化。

    arXiv はバージョンと abs/pdf の違いを無視して "arxiv:ID" にする。
    それ以外はスキーム・ホストの小文字化、トラッキング用クエリと末尾スラッシュ・フラグメントの除去を行う。

    Args:
        url: 元のURL。

    Returns:
        正規化した文字列。
    """
    m = _ARXIV_RE.search(url)
    if m:
        return f"arxiv:{m.group(1)}"
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def item_id_for(url: str) -> str:
    """正規化URLから安定したIDを作る。

    Args:
        url: 元のURL。

    Returns:
        SHA-1 の16進文字列。
    """
    return hashlib.sha1(normalize_url(url).encode("utf-8"), usedforsecurity=False).hexdigest()


def strip_html(text: str, max_chars: int = 1500) -> str:
    """HTMLタグを除去し、空白を詰めて切り詰める。

    Args:
        text: HTMLを含みうる文字列。
        max_chars: 最大文字数。

    Returns:
        プレーンテキスト。
    """
    plain = html.unescape(_TAG_RE.sub(" ", text))
    plain = _WS_RE.sub(" ", plain).strip()
    return plain[:max_chars]


def tokens(text: str) -> set[str]:
    """類似度計算用のトークン集合（英数字の単語＋日本語は1文字単位）。

    Args:
        text: 対象の文字列。

    Returns:
        トークンの集合。
    """
    return set(_TOKEN_RE.findall(text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard係数。

    Args:
        a: 集合A。
        b: 集合B。

    Returns:
        0〜1 の類似度。どちらも空なら 0。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
