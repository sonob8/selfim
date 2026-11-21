# Tutorial: MockModelで改善ループを回す

このチュートリアルでは、APIキーなしで selfim の改善ループを2ラウンド実行し、SIMデータベースとハーネスの変化を確認します。所要時間は5分程度です。完成物は `output/runs/<run_id>/` 配下の sim.json・評価ログ・ハーネス差分です。

## 前提

- `uv sync` 済みであること
- `.env` の編集は不要(プロバイダに `mock` を使うためキーなしで動く)

## 1. ループを実行する

```sh
uv run python scripts/run_loop.py --suite legal_judgment --provider mock --rounds 2 --candidates 2
```

ターミナルにラウンドごとのログが流れます。次の行を探してください。

```
failure clusters: 2 (recurring only)
screening: accepted (required reasoning steps are omitted from the output)
screening: rejected (required reasoning steps are omitted from the output): タスク固有ヒント: ...
candidate_s1 evaluation (round 1): score=100.00 over 6 rollouts
selection: accepted s1 (56.00 -> 100.00)
SIM: predicted recorded -> P1 (round 1)
SIM: outcome recorded -> P1 (fixed=2 missed=0)
=== round 2 (base 100.00) ===
round 2: kept Ht (ゲート通過候補なし)
loop done: evolve 56.00 -> 100.00, held-out 56.00 -> 100.00
[done] held-out before=56.00 after=100.00
```

スコアの数字はスイート内容に依存します。不採択ラウンドでは `kept Ht` の行が出ます。`screening: rejected` はスクリーニングで落ちた候補(評価されず、SIMにも記録されません)です。

## 2. SIMデータベースを読む

```sh
cat output/runs/<run_id>/sim.json
```

JSONの形は次のとおりです。

```json
{
  "next_id": 2,
  "carried_from": null,
  "aliases": {},
  "problems": [
    {
      "id": "P1",
      "name": "reasoning steps are omitted before the final verdict",
      "opened_round": 1,
      "attempts": [
        {
          "round": 1, "sample": "s1", "accepted": true, "backfilled": false,
          "what": "system prompt に思考ステップの出力を必須化する指示を追加",
          "files": "system_prompt.txt",
          "predicted": ["legal-001"],
          "fixed": ["legal-001"], "missed": [],
          "passed": 7, "total": 10,
          "domain": "legal_judgment"
        }
      ]
    }
  ]
}
```

読み方:

- `problems[].name` が根原因の仮説です。試行はこの下に追記されていきます
- `predicted` は評価の前に書かれた予測です。`fixed` と `missed` が評価後の結果です
- この例では予測どおり `legal-001` が改善されました。外れたタスクは `missed` に入ります
- 不採択ラウンドではこのファイルは変わりません(試行は採択された候補だけが記録されます)

## 3. ハーネスの変化を見る

```sh
diff -u src/selfim/harness/modules/system_prompt.txt output/runs/<run_id>/harness/modules/system_prompt.txt
```

採択されたモジュールにだけ差分が出ます。1ラウンドの候補は1モジュールに限定されるため、それ以外のモジュールファイルに差分はありません。シード(`src/selfim/harness/`)はループ実行中も書き換わりません。

## 4. 評価ログを読む

```sh
cat output/runs/<run_id>/evaluations/round_1.json
```

タスク×rolloutごとの記録が `per_task` 配列に入っています。1要素は次の形式です。

```json
{
  "task_id": "legal-001",
  "rollout": 0,
  "score": 70.0,
  "reason": "ルーブリック基準 4 項を決定的採点した。",
  "missing_points": ["思考ステップ欠落: 反対解釈の検討"],
  "attribution": "harness"
}
```

- `score` がルーブリック採点の数値(0–100)、`missing_points` が「何が不足していたか」のトレースです
- `attribution` は失敗の切り分けです。`harness` ならハーネス欠陥、`model` ならモデルの能力限界を示します。ハーネス改善は `harness` のクラスタだけが対象になります
- 同ディレクトリの `manifest_round_N.json` には、評価前に固定された候補の予測(`predicted`)が記録されます

## 5. 別のスイートで試す

```sh
uv run python scripts/run_loop.py --suite hr_evaluation --provider mock --rounds 1
```

同梱のもう一つのサンプル(人事評価タスク)でも同じ構造の成果物が得られます。自分のタスクに差し替える手順は [how-to-swap-tasks.md](how-to-swap-tasks.md) です。

## つまずいたとき

- `scripts/run_loop.py` が見つからない → プロジェクトルートで実行しているか確認してください
- `output/` が空 → コマンドが異常終了していないか、直前のログ行を確認してください
- スコアが低いままで変化しない → 採択候補がなかった可能性があります。ログに `kept Ht` と出ます。SIMはこの場合変わりません
