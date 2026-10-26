"""プロンプトの組み立て。"""

from __future__ import annotations

from news_digest.config import Profile
from news_digest.models import FeedbackExample, Item

RUBRIC = """\
## 採点基準（各1〜5の整数）
- relevance（関連度）
  5: ロール・興味の中心テーマそのもの / 3: 隣接分野で間接的に関係 / 1: 無関係
- applicability（実務への応用性）
  5: 今の業務ですぐ試せる、または設計・技術選定の判断を変える
  3: 中長期的に参考になる / 1: 実務に活かす道筋がない
- impact（インパクト）
  5: 分野の前提を変える（主要な製品・規格・規制の発表、大幅なSOTA更新、広く使われるOSSの大型リリース）
  3: 注目に値する改善・事例 / 1: 些末、宣伝、既知の話の焼き直し
迷ったら低めに付ける。タイトルだけで判断できない場合は本文抜粋を根拠にする。"""


def profile_block(profile: Profile) -> str:
    """プロファイルをプロンプト用の文字列にする。

    Args:
        profile: プロファイル。

    Returns:
        Markdown形式の文字列。
    """
    lines = [f"- ロール: {profile.role}"]
    if profile.context:
        lines.append(f"- 業務内容: {profile.context.strip()}")
    lines.append(f"- 興味: {', '.join(profile.interests)}")
    if profile.exclude:
        lines.append(f"- 興味がないもの: {', '.join(profile.exclude)}")
    return "\n".join(lines)


def query_plan_prompt(profile: Profile) -> str:
    """検索クエリを作るプロンプト。

    Args:
        profile: プロファイル。

    Returns:
        プロンプト。
    """
    return f"""あなたはエンジニア向けの技術情報キュレーターです。
次のユーザーが最新情報を追うための検索クエリを作ってください。

# ユーザー
{profile_block(profile)}

# 指示
- arxiv_queries: arXivの論文検索用。英語の具体的なキーワード句（2〜4語）。興味の各テーマを漏れなくカバーする。
- web_queries: ニュース・技術ブログ検索用。製品発表・規制・OSSリリース・事例が拾えるクエリ。日本の動向を拾う日本語クエリも1〜2個入れる。
- 広すぎる語（"AI" 単体など）は避ける。"""


def _feedback_block(examples: list[FeedbackExample]) -> str:
    if not examples:
        return ""
    good = [e for e in examples if e.rating > 0]
    bad = [e for e in examples if e.rating < 0]
    lines = ["# ユーザーの過去の評価（好みの傾向として参考にする）"]
    if good:
        lines.append("役に立った:")
        lines += [f"- {e.title}" + (f"（{e.note}）" if e.note else "") for e in good]
    if bad:
        lines.append("不要だった:")
        lines += [f"- {e.title}" + (f"（{e.note}）" if e.note else "") for e in bad]
    return "\n".join(lines) + "\n"


def score_prompt(profile: Profile, items: list[Item], feedback: list[FeedbackExample]) -> str:
    """採点のプロンプト（複数記事をまとめて採点する）。

    Args:
        profile: プロファイル。
        items: 採点する記事。
        feedback: 過去のフィードバック。

    Returns:
        プロンプト。
    """
    docs = []
    for it in items:
        date = it.published_at.strftime("%Y-%m-%d") if it.published_at else "不明"
        docs.append(
            f'<item id="{it.short_id}" source="{it.source}" date="{date}">\n'
            f"title: {it.title}\ncontent: {it.content[:800]}\n</item>"
        )
    return f"""あなたはエンジニア向けの技術情報キュレーターです。
次のユーザーにとっての重要度を、記事ごとに採点してください。

# ユーザー
{profile_block(profile)}

{RUBRIC}

{_feedback_block(feedback)}
# 記事（全{len(items)}件。すべての id について1件ずつ採点を返す）
{chr(10).join(docs)}"""


def summary_prompt(profile: Profile, item: Item, reason: str) -> str:
    """要約のプロンプト。

    Args:
        profile: プロファイル。
        item: 要約する記事。
        reason: 採点時の理由（観点の手がかり）。

    Returns:
        プロンプト。
    """
    authors = ", ".join(item.authors[:5])
    return f"""次の記事を、下記ユーザー向けに{profile.output_language}で要約してください。

# ユーザー
{profile_block(profile)}

# 記事
- タイトル: {item.title}
- 取得元: {item.source}
- 著者: {authors or "不明"}
- 選定理由: {reason}
- 本文抜粋:
{item.content}

# 指示
- 本文抜粋に書かれていないことを推測で足さない。分からない点は「抜粋からは不明」とする。
- why_it_matters はユーザーの業務と具体的に結び付ける（一般論にしない）。
- 固有名詞・製品名・数値は原文のまま残す。"""
