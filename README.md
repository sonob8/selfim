# selfim — ハーネス自動改善ループシステム

LLMエージェントのハーネス(System Prompt / Skill / Tool定義)を、評価→改善提案→検証→採択のループで自動的に改善するPythonシステムです。改善履歴は「根原因ごとに試行をグループ化したデータベース(SIM)」に蓄積され、次回以降の提案と選択に使われます。

設計は2本の論文に基づきます。詳細は [docs/explanation.md](docs/explanation.md) を参照してください。

- 論文A: *Self-Improvement Memory Enables Continual Recursive Self-Improvement* — 改善履歴DB(SIM)の設計
- 論文B: *Harnesses That Improve Themselves* — ハーネスのモジュール分解と検証ゲート付き改善ループ

## やること・やらないこと

**やること**

- strands-agents エージェントの7モジュール(system prompt / skill / tool / verify に加え、
  Docker足場の entrypoint / Dockerfile / requirements)を1ラウンド1モジュールで改善
- ユーザー提供のルーブリックに基づくLLM-as-a-Judge評価(数値スコア + 不足点トレース)
- 改善の予測と実際の成果をSIMに記録し、以降の提案に反映
- docker実行系では候補ハーネスを**そのままビルド可能なイメージ**として扱い、
  AgentCore互換IF(POST /invocations)で評価。勝者ハーネスは tar イメージとして出荷
  (AgentCore Runtime / ECS 等へ持ち出し可能)

**やらないこと**

- モデル重みの更新(ポリシーモデルは凍結)
- 評価ルーブリックの自動生成(ルーブリックはユーザーが提供)
- 改善オプティマイザ自身(Proposer/Selector)のコード改善(再帰的な自己改善は対象外)

## ドキュメント

| 目的 | ドキュメント |
|---|---|
| MockModelでループを初めて回す | [docs/tutorial.md](docs/tutorial.md) |
| Dockerでループを回してイメージを出荷する | [docs/docker.md](docs/docker.md) |
| 自分のタスクとルーブリックに差し替える | [docs/how-to-swap-tasks.md](docs/how-to-swap-tasks.md) |
| コンポーネントのAPIを調べる | [docs/reference.md](docs/reference.md) |
| なぜこの設計か(両論文の統合) | [docs/explanation.md](docs/explanation.md) |

## セットアップ

```sh
uv sync
cp .env.example .env
```

APIキーは不要です。プロバイダを `bedrock` に切り替えるまで `.env` は触りません。Bedrockへの切り替え手順は [docs/how-to-swap-tasks.md](docs/how-to-swap-tasks.md) の「Bedrockで動かす」を参照してください。

## 最小の実行例

```sh
# local実行系(in-process・高速・キー不要)
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --rounds 2 --candidates 2

# docker実行系(候補をイメージ化してAgentCore互換コンテナで評価・出荷)
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --executor docker --rounds 2 --candidates 2
```

このコマンドは、同梱サンプルスイート(裁判の判断タスク)を2ラウンド改善し、`output/runs/<run_id>/` に改善後のハーネス、`sim.json`、評価ログを書き出します。docker実行系ではさらに `ship/agent-image.tar`(出荷イメージ)と `ship/ship.json` が生成されます。何が起きているかは [docs/tutorial.md](docs/tutorial.md) と [docs/docker.md](docs/docker.md) で一歩ずつ追えます。

## テスト

```sh
uv run pytest              # docker不要の全テスト(既定。docker系はスキップ)
uv run pytest -m docker    # 実Dockerを使うテストのみ
uv run pytest -m docker_e2e  # 全体ループのdocker E2E(遅い)
```
