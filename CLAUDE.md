# news-digest-agent 開発規約

ロールと興味（`profile.yaml`）に合わせて最新情報を集め、重要なものを要約して Markdown と Slack に届ける LangGraph アプリ。要件は [docs/requirements.md](docs/requirements.md)、設計判断は [docs/decisions.md](docs/decisions.md)。

## コマンド

```bash
uv sync
uv run pytest                       # 実APIは呼ばない（偽LLM・MockTransport）
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run digest run --dry-run --no-slack -v   # 履歴を更新せずに試す
uv run langgraph dev                        # Studio。入力に {"dry_run": true, "post_to_slack": false}
```

## 規約

- **グラフ**: ノードは `graph.py` の `build_graph` 内に置き、外部依存は `Deps` から使う。並列ノードが書くキーには reducer を付ける。
- **状態の型**: 状態と Send の引数は Pydantic モデルにする（Studio で編集した JSON をノードの前で型に戻すため）。Send で受け取るノードは最初に `model_validate` する。
- **LLM**: `llm.make_chat_model` で作ったモデルの `with_structured_output` だけを使う。数値（総合点など）はLLMに計算させずコードで計算する。
- **テスト**: 実API・実LLMを呼ばない。外部APIを叩くテストを書く場合は `@pytest.mark.network` を付ける。
- **秘密情報**: キーは `.env` にだけ置く。ログにキーやURLのクエリを出さない（`httpx` のロガーは WARNING）。
- **ログ**: `print` ではなく `logging`。
- **日本語**: コメント・docstring・ドキュメントは日本語、識別子は英語。docstring には `Args:` / `Returns:` を書く。
- **設計判断**は `docs/decisions.md` に追記する。
