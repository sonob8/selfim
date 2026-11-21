# CLAUDE.md — selfim プロジェクト方針

本プロジェクトは2本の論文を統合し、strands-agents エージェントのハーネス(System Prompt / Skill / Tool定義)を自動改善するループシステムを構築する。
グローバル規約(~/.claude/CLAUDE.md)に加え、本プロジェクト固有の方針を以下に定める。

---

## 1. 論文要約

### 論文A: `papers/google_selfimrrovement_agent.pdf`
**Self-Improvement Memory Enables Continual Recursive Self-Improvement (SIM/CRSI, Google Cloud AI, 2026-11-13)**

- **問題**: ハーネス最適化(RSI)は固定のベンチマーク×モデルで行われ、設定が変わると「なぜその編集をしたのか・何を期待したのか・結果はどうだったか」という履歴がハーネスから失われ、同じ試行の繰り返しや目的不明の編集が起きる。
- **SIM (Self-Improvement Memory)**: 改善履歴を**根原因(root cause)ごとに試行をグループ化したDB**として管理。
  - エントリ: `m_i = (c_i, [(a_ij, o_ij, p_ij)]_j)` — 根原因 `c`、修正と根拠 `a`、予測/観測成果 `o`、出自 `p`(ドメイン・ベンチ・モデル・ラウンド)。
  - **予測は評価前に記録し、評価後に検証**(fixed/missed で確定)。
  - Proposer/Selector は**検索・truncationなしでSIM全文**を参照(§3.2)。
  - 各提案は「既存根原因の引用 or 新規根原因の命名」必須。マッチング判断は **Proposer自身**(類似度モデル不使用、D.1)。根原因テキスト完全一致のエントリは決定的マージ + `aliases` 記録。
  - **採択された候補のみ**SIMに記録。不採択なら `U(M, ∅) = M`(SIM不変)。後で編集が置き換えられても記録は残す(失敗履歴の保持、P8ケース)。
- **SIM JSONスキーマ (D.3, 本実装のスキーマ正当解)**:
  ```json
  {
    "next_id": 10,
    "carried_from": {"arm": "...", "domain": "...", "into": "..."},
    "aliases": {"P6": "P5"},
    "problems": [{
      "id": "P5", "name": "<root-cause hypothesis>", "opened_round": 6,
      "attempts": [{
        "round": 6, "sample": "s2", "accepted": true, "backfilled": false,
        "what": "...", "files": "...",
        "predicted": ["task-a"], "fixed": ["task-a"], "missed": [],
        "passed": 49, "total": 60, "domain": "coding_enriched"
      }]
    }]
  }
  ```
- **ループ (Eq.1/3)**: `Ct = P(Ht, Ft, Dt; Mt)`、`Ht+1 = S(Ht, Ft, Ct, FCt; Mt)`。N=10ラウンド、k=5候補。スクリーニング落ち候補は評価しない。不採択なら `Ht+1 = Ht`。
- **D.2 の8原則**(Proposer/Selectorプロンプトに埋め込む): ①エントリは過去の経験として柔軟に扱う ②エントリのドメイン/ベンチ/モデルを確認 ③既存診断で説明できるならそれから始める ④根拠を再利用し、ベンチ/モデル変更後は編集を再評価 ⑤新エビデンスがあれば revise/replace/override ⑥テスト済みの方向は新エビデンスがある時だけ再訪 ⑦既存診断で説明できない失敗は新規エントリ ⑧似た設定ではタスク固有エビデンスを重視、両方が変われば軽視。
- **CRSI**: ソース実行の `(H_S, M_S)` をターゲットに持ち込み最適化を継続。比較条件: Cold Start / Harness Transfer / Problem-Only Memory / Full SIM(Full SIMが全設定で最高)。

### 論文B: `papers/Harnesses That Improve Themselves.pdf`
**Harnesses That Improve Themselves: A Unifying Framework for Self-Improving Agent Harnesses (Shah & Kathrotia, Google, 2026-11-17)**

- **ハーネスの4コンポーネント(モジュール分解)**:
  1. **Context & State** — システムプロンプト、記憶、compaction
  2. **Agent Control** — ターンループ、リトライ、サブエージェント
  3. **Action Interface** — ツール/ACI、サンドボックス実行、出力整形
  4. **Verification & Recovery** — 停止前検証、テスト実行、修復ルーティング
- **4段階アウターループ**: ①Diagnose Failures(検証エラークラスタリング、合格/不合格軌跡のdiff)→ ②Propose Edits(モジュールコードパッチ)→ ③Validate & Filter(ランタイム実行確認、非退行、トークン予算)→ ④Deploy or Route。
- **4つの Safe Guard (過学習防止)**:
  1. **モジュールを別々に進化**(同時全文書き換えは共有状態仮定を壊す — ModularRSI ablation)
  2. **編集は小さく一般的に**(繰り返し失敗クラスタに紐付ける。単発reflectionは"起きなかった失敗"へのルール追加=Phantom Guardrails を生む。編集サイズ予算を縮小しながら適用)
  3. **受け入れチェック厳守**(構文確認、evolve+validation分割での非退行、**新コードパスが実際に実行されたことの実行時計測確認**、デッドコード剪定)
  4. **dev/testタスクプールの分離**(evolveセットで進化し、held-outは凍結後にのみ評価)
- **失敗帰属**: ハーネス欠陥 vs モデル限界の切り分け。推論は正しいのに実行が失敗(パース拒否、cwd消失、compactionで制約消失、テスト前終了)ならハーネスが悪い。ツール実行が正常で全rolloutがドメインロジックで失敗ならハーネス編集を控える。
- **安全境界 (Table III)**: 評価器・ログはエージェント書込スコープ外の読み取り専用隔離。ハーネスは自らの権限境界・採点基準・監査ログを制御してはならない。
- **実験知見**: Modular Gated Evolution が全文書き換えより精度同等以上でトークン約1/10。プロンプト追記だけでは早期終了を防げない(ランタイムコードが必要)。

## 2. 統合設計の方向性(本プロジェクトの立ち位置)

| 論文Bから採用(実行制御) | 論文Aから採用(記憶) |
|---|---|
| ハーネスを4モジュールに分解し**1候補=1モジュール**に限定 | SIM DB(根原因×試行履歴)をProposer/Selectorの記憶に |
| 実行前スクリーニング + 受け入れゲート(構文/実行/非退行/予算) | 予測→評価→事後検証の2段階記録 |
| 繰り返し失敗クラスタへの紐付け必須、単発は却下 | 根原因の引用 or 新規命名を提案に必須化、Proposerがマッチング判断 |
| 評価器/データ/ログの隔離(書込スコープ外) | 採択のみSIM更新、不採択はSIM不変、置換後も記録保持 |

- ユーザー指定の**5コンポーネント**: SIMManager / Evaluator / Proposer / Selector / MainLoop。論文B由来の **ModuleRegistry**(モジュール分解・候補隔離適用)と **ValidationGate**(受け入れゲート)を追加する。
- 改善対象モジュールと論文Bコンポーネントの対応:
  - `system_prompt.txt` → Context & State
  - `skills/task_procedure/SKILL.md` → Agent Control(手順知識)
  - `tools/reasoning_tools.py` → Action Interface(@tool定義)
  - `verify_checklist.py` → Verification & Recovery(完了前チェックリスト生成)
- タスクは**推論タスク**(例: 裁判の判断=途中思考ステップ含む、人事評価)。スイート+ルーブリックは `data/tasks/<suite>/`(ユーザー提供・差し替え可能)。Ground TruthなしのLLM-as-a-Judgeで「数値スコア(0-100)+評価理由・不足点トレース」を出す。
- LLM基盤: 最終的に **Bedrock**。現状APIキーなしのため、プロバイダ抽象化(`bedrock | mock`)+ **MockModel**(決定的・スクリプト可能)でキーなし開発/テスト/デモを可能にする。

## 3. 成功基準(3点照合の判定基準)

1. SIM JSONスキーマが論文A D.3 と完全一致(上記スキーマ参照)
2. ループ1回転 = 評価→提案→スクリーニング→適用→再評価→SIM更新 の順。MockModelでキーなしE2Eが動く
3. 提案は根原因引用 or 新規命名を含み、予測は評価前に記録、成果は評価後に記録。採択のみSIM記録、不採択はSIM不変
4. 1候補=1モジュール(論文B SG-1)、繰り返し失敗に紐付かない編集は却下(SG-2)
5. 受け入れゲート: 構文/import + 新コードパス実行確認 + 非退行 + diff行数予算(SG-3, Eq.(1)のG_val)
6. Evaluatorはルーブリック採点で数値スコア+理由/不足点トレースを出力
7. タスクスイート+ルーブリックは `data/tasks/` で差し替え可能
8. 評価データ・ルーブリックは候補ハーネスの書込スコープ外(読み取り専用隔離)

## 4. 開発ワークフロー(Track B: pstack-style)

承認ゲートを省略しない:
- **G1** README(Diátaxis)承認 → 承認なしに実装へ進まない
- **G2** 設計候補の選択(/pstack:architect、usage sketch + 型定義 + rationale)
- **G3** テスト計画承認 → RED/GREEN TDD、GREENごとに単位コミット(直前にdiff-reviewerサブエージェント)
- **G4** verification結果提示(MockModel E2Eエビデンス)
- **G5** 3点照合(README⇔実装⇔Feature Map)の結果と残存不一致の処理方針

実装順: schemas → sim_manager → module_registry/validation_gate → evaluator → proposer/selector → main_loop → CLI/サンプルスイート。

## 5. プロジェクト固有の禁止事項

- `papers/`, `data/tasks/` は読み取り専用として扱う。候補ハーネスがこれらへ書き込む実装は禁止(論文B Table III-1)
- 実行ログは append-only で `output/runs/<run_id>/logs/` に隔離。候補は `output/runs/<run_id>/candidates/` 配下でのみ作業
- シードハーネス(`src/selfim/harness/`)はループ実行中に直接書き換えない。必ずrunワークスペースへ複製してから編集
- SIMは1 runにつき1 JSONファイル(`output/runs/<run_id>/sim.json`)。検索やtruncationで部分的にプロンプトへ渡さない(全文参照)
- `main.py` 等のルート直下ファイルを新規作成しない(標準レイアウト準拠)
- コメント・docstringは日本語で冗長に(入力・処理・出力・制約を記す)。`[FIX <date>]` / `[DECISION <date>]` タグの根拠コメント規則はグローバルCLAUDE.mdに従う
