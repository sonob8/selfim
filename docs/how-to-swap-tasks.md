# How-to: 自分のタスクとルーブリックに差し替える

`data/tasks/` にスイートディレクトリを1つ作れば、独自の推論タスクとルーブリックでループを回せます。既存のスイートは変更しません。

## タスクスイートを作る

1. サンプルを複製します。

	```sh
	cp -r data/tasks/legal_judgment data/tasks/my_suite
	```

2. `tasks.yaml` を編集します。1タスクは次の形です。

	```yaml
	- task_id: my-001
	  prompt: "あなたは審査員です。次の申請を評価し、結論と根拠を述べてください。"
	  context: "申請書: ...(タスクに渡す資料)"
	  reasoning_steps_required:
	    - "評価基準の確認"
	    - "証拠の突合"
	    - "結論への整合チェック"
	```

	- `task_id` はスイート内で一意にします。SIMの `predicted`/`fixed`/`missed` はこのIDで記録されます
	- `reasoning_steps_required` は、エージェントに途中の思考ステップを出させる場合に指定します。不要なら省略できます
	- ルーブリックジャッジに渡す資料は `context` に入れます。評価データ本体はハーネスから書き込めない場所に置かれるため、候補ハーネスがデータを書き換えることはありません

3. `rubric.yaml` を編集します。

	```yaml
	scoring_scale:
	  min: 0
	  max: 100
	criteria:
	  - name: "結論の妥当性"
	    weight: 0.4
	    description: "結論が与えられた基準から論理的に導けているか"
	  - name: "思考ステップの完全性"
	    weight: 0.4
	    description: "reasoning_steps_required の各ステップが明示されているか"
	  - name: "根拠の具体性"
	    weight: 0.2
	    description: "根拠が資料に即して具体的か"
	judge_instructions: "各基準を独立に採点し、不足があれば missing_points に列挙してください。"
	```

	- `weight` の合計は1.0にします。総合スコアは重み付き和です
	- criteria はスイート共通のみです(タスクごとの上書きには未対応)。タスクごとに採点基準を変えたい場合は、基準を一般化するかスイートを分割してください

4. 実行します。

	```sh
	uv run python scripts/run_loop.py --suite my_suite --provider mock --rounds 2
	```

## Bedrockで動かす

1. `.env` を開き、次の3つを設定します。

	```
	SELFIM_MODEL_PROVIDER=bedrock
	BEDROCK_REGION=us-west-2
	BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-6-20251014-v1:0
	```

2. AWS認証情報が環境に用意されていることを確認します(`aws configure` または環境変数)。

3. まず評価だけ回して接続を確認します。

	```sh
	uv run python scripts/run_loop.py --suite my_suite --provider bedrock --rounds 0
	```

	`--rounds 0` はベースライン評価のみを実行し、改善はしません。

	認証情報なしでも Bedrock 経路を検証したい場合は、moto ベースの統合テストが利用できます(実strands Agent → BedrockModel → boto3シリアライズまでを実行し、AWS API のみをエミュレート):

	```sh
	uv run pytest tests/test_bedrock_integration.py -q
	```

4. 接続を確認できたらループを回します。

	```sh
	uv run python scripts/run_loop.py --suite my_suite --provider bedrock --rounds 2
	```

提案品質が足りないときは、`config/settings.yaml` の `models:` セクションで役割ごとにプロバイダを変えられます(例: ジャッジのみ別プロバイダ)。モデルIDは `.env` の `BEDROCK_MODEL_ID` で全役合共通の1値を指定します(役割ごとのモデルID指定には未対応)。

## タスクをevolve/held-outに分割し直す

ループはスイート内のタスクを自動で半分ずつ evolve / held-out に分割します(実行ごとに `output/runs/<run_id>/splits.json` に記録)。分割を固定したいときは、スイートディレクトリに `splits.yaml` を置きます。

```yaml
evolve: [my-001, my-002, my-003]
held_out: [my-004, my-005]
```

`splits.yaml` がある場合はそちらが優先され、実行間で分割が固定されます。
