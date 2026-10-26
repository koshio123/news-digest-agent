"""Studio（langgraph dev）での操作を再現するテスト。

Studio で状態を編集すると値は JSON（dict・文字列）で届く。チェックポイントからの再開・フォークで
ノードが Item などのモデルを受け取れることを確かめる。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from news_digest.graph import Deps, build_graph
from tests.conftest import NOW


def test_input_datetime_string_is_coerced(make_deps: Callable[..., Deps]) -> None:
    graph = build_graph(make_deps())
    out: dict[str, Any] = graph.invoke(
        {"since_override": "2026-10-24T09:00:00+09:00", "dry_run": True, "post_to_slack": False}
    )
    assert out["since"].isoformat() == "2026-10-24T00:00:00+00:00"
    # タイムゾーンなしは JST とみなす
    out2: dict[str, Any] = graph.invoke({"since_override": "2026-10-24", "dry_run": True})
    assert out2["since"].isoformat() == "2026-10-23T15:00:00+00:00"


def test_fork_with_json_edited_state(make_deps: Callable[..., Deps]) -> None:
    graph = build_graph(make_deps(), checkpointer=InMemorySaver())
    cfg: Any = {"configurable": {"thread_id": "t1"}}
    graph.invoke({"dry_run": True}, cfg, interrupt_before=["prefilter"])
    state = graph.get_state(cfg)
    assert state.next == ("prefilter",)

    # Studio で編集したときと同じく、JSON にした値で候補を1件に絞る
    first = state.values["candidates"][0].model_dump(mode="json")
    forked = graph.update_state(cfg, {"candidates": [first]})
    out: dict[str, Any] = graph.invoke(None, forked)

    assert [s.item.id for s in out["scored"]] == [first["id"]]
    assert out["until"] == NOW


def test_breakpoint_and_resume(make_deps: Callable[..., Deps]) -> None:
    graph = build_graph(make_deps(), checkpointer=InMemorySaver())
    cfg: Any = {"configurable": {"thread_id": "t2"}}
    graph.invoke({"dry_run": True}, cfg, interrupt_before=["select"])
    assert graph.get_state(cfg).next == ("select",)
    out: dict[str, Any] = graph.invoke(None, cfg)
    assert out["digest_path"]
