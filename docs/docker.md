# Docker実行系(docker executor)ガイド

selfim の docker実行系は、候補ハーネスを**そのままビルド可能なDockerイメージ**として
扱い、「ビルド → コンテナ起動 → HTTPでタスク投入 → 評価 → 採択 → 再ビルド」の
ループを回す。コンテナは AgentCore Runtime 互換IF(`POST /invocations` / `GET /ping`、
ポート8080)で待ち受けるため、出荷イメージは AgentCore Runtime や ECS へ
そのまま持ち出せる(ローカル最適化 → イメージ出荷)。

## クイックスタート

```sh
# キー不要(mock)。Dockerデーモンが動いていること。
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --executor docker --rounds 2 --candidates 2
```

成果物(`output/runs/<run_id>/`):

| パス | 内容 |
|---|---|
| `ship/agent-image.tar` | 勝者ハーネスの最終イメージ(docker save) |
| `ship/ship.json` | タグ・digest・モジュールハッシュ・スコア・SIM概要 |
| `harness/` | 昇格済みハーネス(そのままビルドコンテキスト) |
| `logs/<container>.log` | 候補ごとのコンテナログ |
| `sim.json`, `evaluations/`, `report.json` | 従来どおり |

出荷イメージの確認:

```sh
docker load -i output/runs/<run_id>/ship/agent-image.tar
docker run -d -p 127.0.0.1:18080:8080 selfim-<run_id>-final
curl http://127.0.0.1:18080/ping
curl -X POST http://127.0.0.1:18080/invocations \
  -H "Content-Type: application/json" \
  -d '{"task_id":"t1","prompt":"判断せよ","context":"事実: ...","reasoning_steps_required":["確認"]}'
```

## アーキテクチャ

```
harness/(ビルドコンテキスト = 候補ディレクトリ)
├── modules/        編集可能: system_prompt / skill / tools / verify
├── scaffold/       編集可能: entrypoint.py / Dockerfile / requirements.txt
└── runtime/        固定(Proposer編集外): HTTP契約とプロバイダ選択を保護
```

- ビルドは `docker build -f scaffold/Dockerfile <候補dir>` 相当。
  Dockerfile のレイヤ順(requirements → pip → コード)により、モジュール編集だけの
  候補は依存インストール層がキャッシュ命中し高速。
- 1候補 = 1コンテナ。スイート全体(tasks × rollouts)で再利用し、評価後に破棄。
- ホストポートはエフェメラル(衝突回避)、公開先は 127.0.0.1 限定。
- コンテナenvは `SELFIM_MODEL_PROVIDER` のみ(bedrock時のみAWS系をパススルー)。
- タスクは1件ずつJSONペイロードとして渡る。ルーブリック・全タスク一覧・
  `data/tasks/`・`papers/` はイメージにもコンテナにも渡らない(論文B Table III)。
  これらを COPY するDockerfile編集はスクリーニングで自動拒否される。

## 実行プロトコル(InvocationEnvelope)

`POST /invocations` のリクエストは TaskSpec の JSON、応答は次のenvelope:

```json
{
  "ok": true,
  "output_text": "...",
  "composed_prompt": "<実際に組成されたプロンプト>",
  "call_log": [{"module_file": "tools.py", "function_name": "outline"}],
  "token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
  "error": ""
}
```

`composed_prompt` と `call_log` はホスト側の実行証拠判定(プロンプト包含・
実呼出回数)に使われる。local実行系と同一セマンティクス。

## 安全上の注意と残存リスク

- **実行時の外向き通信は完全遮断できない**: `--network none` や internal ネットワークは
  ポート公開と両立しない(2026-11-22 検証済み)。このためコンテナは通常ブリッジで
  起動する。ビルド時の `RUN curl/wget/aws s3` はSelectorが拒否するが、実行時に
  候補コード自身が外へ通信する可能性は残る。bedrock プロバイダ使用時はそもそも
  外向き通信が必要。**信頼できないProposer出力を扱う場合は隔離ネットワーク上で
  実行すること**を推奨。
- **AWS資格情報**: bedrock モードでは `AWS_ACCESS_KEY_ID` 等がコンテナへ渡る。
  可能ならタスクロール等の临时資格情報を使うこと。
- **コンテナ内 bedrock 経路は自動テスト対象外**: 実クレデンシャルが必要なため、
  テストは mock プロバイダで網羅する。bedrock 接続性は実環境でのスモーク確認による。

## WSL2 での性能メモ

プロジェクトが Windows 側マウント(/mnt/c)にあると、ビルドコンテキストの読み取りが
9P 経由になり遅い。コンテキストは約10ファイルと小さいため通常は問題にならないが、
遅い場合はプロジェクト一式を WSL ファイルシステム側(~/)へコピーして実行すると
大幅に速くなる。

## プラットフォーム(arch)に関する注意

AgentCore Runtime / ECS へデプロイする際はターゲットのCPUアーキテクチャに合わせて
ビルドすること(例: `docker build --platform linux/amd64 ...`)。出荷タグの再ビルドは
`output/runs/<run_id>/harness/` をコンテキストに `scaffold/Dockerfile` で行えばよい。
