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
[round 1] Stage 1: evaluate -> score=62.5
[round 1] Stage 2: propose -> 2 candidates (screened: 1)
[round 1] Stage 4: accepted candidate #1 -> new score=71.0
[round 2] ...
[done] held-out before=58.0 after=74.0
```

スコアの数字は実行ごとに変わります(MockModelは決定的ですが、初期化の乱数シードが変わるためです)。行の並び順が違えば、それはループの構造が変わったことを意味します。

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

タスクごとに次の形式の記録が入っています。

```json
{
  "task_id": "legal-001",
  "score": 70.0,
  "reason": "結論は妥当だが、判例適用の根拠を示す思考ステップが欠落",
  "missing_points": ["適用法令の条文確認ステップ", "反対意見の検討"],
  "attribution": "harness"
}
```

- `score` がルーブリック採点の数値(0–100)、`reason` と `missing_points` が「何が不足していたか」のトレースです
- `attribution` は失敗の切り分けです。`harness` ならハーネス欠陥、`model` ならモデルの能力限界を示します。ハーネス改善は `harness` のクラスタだけが対象になります

## 5. 別のスイートで試す

```sh
uv run python scripts/run_loop.py --suite hr_evaluation --provider mock --rounds 1
```

同梱のもう一つのサンプル(人事評価タスク)でも同じ構造の成果物が得られます。自分のタスクに差し替える手順は [how-to-swap-tasks.md](how-to-swap-tasks.md) です。

## つまずいたとき

- `scripts/run_loop.py` が見つからない → プロジェクトルートで実行しているか確認してください
- `output/` が空 → コマンドが異常終了していないか、直前のログ行を確認してください
- スコアが低いままで変化しない → 採択候補がなかった可能性があります。ログに `kept Ht (no accepted candidate)` と出ます。SIMはこの場合変わりません
