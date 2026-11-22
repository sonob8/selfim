# selfim — 自己改善ループでエージェントハーネスを最適化し、Dockerイメージとして出荷する

selfim は、LLMエージェントのハーネス(システムプロンプト / スキル / ツール定義 / 検証コード / Docker足場)を、**「評価 → 改善提案 → 検証 → 採択」のループで自動改善**するシステムです。改善対象のエージェントは Docker イメージとしてビルド・実行され、最終的に AgentCore Runtime や ECS へ持ち出せる形で出荷されます。

- ベースモデルの重みは更新しません(ポリシーモデルは凍結)。改善するのはハーネスのみです
- 改善履歴は「根原因ごとに試行をグループ化したDB(SIM)」に蓄積され、次回以降の提案と選択に使われます
- キー不要の決定的 MockModel により、AWSクレデンシャルなしでループ全体を動かせます

## コンセプト

2本の研究に基づいて設計されています。

**論文A: Self-Improvement Memory Enables Continual Recursive Self-Improvement**
改善履歴を根原因(root cause)ごとにグループ化したDB(SIM)として管理します。
各改善提案は「既存根原因の引用 or 新規根原因の命名」を必須とし、予測は評価前に記録・
評価後に検証されます。提案者(Proposer)と選択者(Selector)はSIM全文を参照するため、
同じ試行の繰り返しや目的不明の編集を防げます。

**論文B: Harnesses That Improve Themselves**
ハーネスをモジュール(System Prompt / Skill / Tool / Verify など)に分解し、
「小さく・一般的な編集」だけを検証ゲート付きで受け入れます。
単発の失敗への反射的修正(Phantom Guardrails)や、評価データへの過学習を
安全装置(Safe Guards)で防ぎます。

selfim はこの2つを統合し、さらに**候補ハーネスをそのままビルド可能なDockerイメージ**として扱います:

```
        ┌─────────────────── ホスト(ループプロセス) ───────────────────┐
        │  評価(LLM-as-a-Judge) → 提案(Proposer) → スクリーニング       │
        │  → docker build → コンテナ実行 → 検証ゲート → 採択(Selector)    │
        │  → SIM更新 → 次ラウンド                                       │
        └──────────────────────────┬───────────────────────────────┘
                                   │ HTTP POST /invocations(AgentCore互換)
        ┌──────────────────────────▼───────────────────────────────┐
        │  候補ハーネスのコンテナ(ポート8080, /invocations + /ping)     │
        │  strands Agent(Bedrock)または決定的Mock                     │
        └────────────────────────────────────────────────────────────┘
        勝者ハーネス → selfim-<run_id>-final イメージ → docker save → 出荷
```

## 何が改善されるか(7モジュール)

| モジュール | パス | 論文Bコンポーネント |
|---|---|---|
| system_prompt | `modules/system_prompt.txt` | Context & State |
| skill | `modules/skill.md` | Agent Control(手順知識) |
| tool | `modules/tools.py` | Action Interface |
| verify | `modules/verify.py` | Verification & Recovery |
| entrypoint | `scaffold/entrypoint.py` | Agent Control(実行時コード) |
| dockerfile | `scaffold/Dockerfile` | デプロイ境界 |
| requirements | `scaffold/requirements.txt` | 依存境界 |

1候補 = 1モジュールの編集(全ファイル置換)。1ラウンドで複数候補を生成・評価し、
最良の1件だけが現行ハーネスへ昇格されます。不採択の場合、ハーネスとSIMは不変です。

## 安全機構

- **検証ゲート**: 構文確認(compile)/ docker build成功 / スモークテスト(新コードパス実行確認)/ 非退行 / diff行数予算 / トークン予算
- **Table III(評価系の隔離)**: タスク・ルーブリック・SIM・評価器は候補の書込スコープ外。`data/` `papers/` をイメージへ COPY する編集やホストパスのマウントはスクリーニングで自動拒否
- **SIMによる同一方向の再訪防止**: 過去に失敗した方向は「新エビデンス」の宣言がない限り再提案を拒否
- **evolve / held-out 分離**: 最適化に使うタスクセットと最終確認用セットは厳密に分離(過学習の検出)

## セットアップ

要件: Python 3.12+ / [uv](https://docs.astral.sh/uv/) / (docker実行系のみ)Docker

```sh
uv sync
cp .env.example .env
```

APIキーは不要です。プロバイダを `bedrock` に切り替えるまで `.env` は触りません。

## 使い方

### local実行系(in-process・高速・キー不要)

```sh
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --rounds 2 --candidates 2
```

### docker実行系(本命: イメージをビルドして評価・出荷)

```sh
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --executor docker --rounds 2 --candidates 2
```

各候補は「docker build → コンテナ起動 → HTTPでタスク投入 → 評価 → ゲート」を経て、
採択されたハーネスが次ラウンドのベースになります。ループ完了後、勝者ハーネスは
`selfim-<run_id>-final` イメージとしてビルドされ、tar 形式で出荷されます。

成果物(`output/runs/<run_id>/`):

| パス | 内容 |
|---|---|
| `ship/agent-image.tar` | 出荷イメージ(docker save) |
| `ship/ship.json` | タグ・digest・モジュールハッシュ・スコア・SIM概要 |
| `harness/` | 昇格済みハーネス(そのままビルドコンテキスト) |
| `sim.json` | 改善履歴DB(根原因×試行) |
| `evaluations/`, `report.json` | 評価ログとレポート |
| `logs/` | 候補ごとのコンテナログ |

出荷イメージの確認と持ち出し:

```sh
docker load -i output/runs/<run_id>/ship/agent-image.tar
docker run -d -p 127.0.0.1:18080:8080 selfim-<run_id>-final
curl http://127.0.0.1:18080/ping
curl -X POST http://127.0.0.1:18080/invocations \
  -H "Content-Type: application/json" \
  -d '{"task_id":"t1","prompt":"判断せよ","context":"事実: ...","reasoning_steps_required":["確認"]}'
```

イメージは AgentCore Runtime 互換IF(`POST /invocations` / `GET /ping`、ポート8080)で
待ち受けるため、ECR へ push して AgentCore Runtime / ECS へそのままデプロイできます
(デプロイ時はターゲットのCPUアーキテクチャに合わせて `--platform` で再ビルドしてください)。

### Bedrock で実行する

`.env` で `SELFIM_MODEL_PROVIDER=bedrock` を設定し、AWS資格情報(`AWS_ACCESS_KEY_ID` 等)
を環境変数に用意します。タスクエージェントはコンテナ内から Bedrock を呼び出します
(ジャッジ/提案者/選択者はホスト側から呼び出します)。

### 自分のタスクに差し替える

タスクスイートとルーブリックは `data/tasks/<suite>/` に YAML で置くだけです。
詳細は [docs/how-to-swap-tasks.md](docs/how-to-swap-tasks.md) を参照。

## テスト

```sh
uv run pytest                # docker不要の全テスト(既定。docker E2Eは除外)
uv run pytest -m docker      # 実Dockerを使うテストのみ
uv run pytest -m docker_e2e  # 全体ループのdocker E2E(イメージビルドを含む・遅い)
```

## ドキュメント

| 目的 | ドキュメント |
|---|---|
| MockModelでループを初めて回す | [docs/tutorial.md](docs/tutorial.md) |
| Dockerでループを回してイメージを出荷する | [docs/docker.md](docs/docker.md) |
| 自分のタスクとルーブリックに差し替える | [docs/how-to-swap-tasks.md](docs/how-to-swap-tasks.md) |
| コンポーネントのAPIを調べる | [docs/reference.md](docs/reference.md) |
| なぜこの設計か(両論文の統合) | [docs/explanation.md](docs/explanation.md) |

## プロジェクト構成

```
src/selfim/
  main_loop.py        改善ループ本体(評価→提案→スクリーニング→適用→ゲート→採択→SIM更新)
  agent_runner.py     local実行系 + TaskRunner プロトコル + 実行証拠生成
  docker_executor.py  docker実行系(イメージビルド + コンテナ実行)
  container_session.py コンテナライフサイクル(起動/ping待ち/invocation/ログ保存)
  evaluator.py        LLM-as-a-Judge評価と失敗クラスタリング
  proposer.py         改善提案(根原因の引用 or 新規命名を必須化)
  selector.py         実行前スクリーニング(Table III強制を含む)と評価後選択
  validation_gate.py  受け入れゲート(構文/ビルド/スモーク/非退行/予算)
  sim_manager.py      SIM(改善履歴DB)の読み書き
  module_registry.py  7モジュールの管理・候補隔離適用・昇格
  shipper.py          最終イメージの出荷(docker save + ship.json)
  harness/            シードハーネス(modules/ + scaffold/ + 固定runtime/)
scripts/run_loop.py   CLIエントリ
data/tasks/           タスクスイート+ルーブリック(差し替え可能)
```

## 制限事項

- モデル重みの更新は行いません(ハーネスのみが改善対象)
- ルーブリックの自動生成は行いません(ユーザーが提供)
- 改善オプティマイザ自身(Proposer/Selector)のコード改善は対象外
- コンテナ内の Bedrock 経路は実クレデンシャルが必要なため自動テスト対象外
  (mockプロバイダでロジックを網羅。詳細は [docs/docker.md](docs/docker.md))
- `papers/`(本リポジトリの設計根拠とした論文)は著作権の関係で公開リポジトリには
  含めていません。ローカルで `papers/` にPDFを置けば `CLAUDE.md` の要約と突き合わせできます

## ライセンス

未設定(追加予定)。
