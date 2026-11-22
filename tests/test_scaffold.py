"""シードハーネスの足場(scaffold)と固定ランタイム(runtime)の単体テスト。

検証観点(in-process、Docker不要):
- 実シードが7モジュール+固定runtime+.dockerignoreを備えること
- scaffold/entrypoint.py の run_task がInvocationEnvelope形式のdictを返すこと
- mockプロバイダ(コンテナ内移植版)がマーカー駆動で挙動を変えること(設計判断 D-5 互換)
- compose_prompt が AgentRunner._compose_prompt と同一規則であること(local/docker間の
  実行証拠セマンティクス一致)
- runtime/app.py の invoke がエントポイント例外をエラーenvelopeに変換すること
"""

from pathlib import Path

import pytest

from selfim.module_registry import MODULE_PATHS

# 実シードハーネスのパス(src/selfim/harness)
SEED_DIR = Path(__file__).resolve().parent.parent / "src" / "selfim" / "harness"
RUNTIME_DIR = SEED_DIR / "runtime"


@pytest.fixture()
def seed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """コンテナ内ランタイムを実シードのモジュール/足場に対して動かす環境変数を設定する。"""
    monkeypatch.setenv("SELFIM_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("SELFIM_MODULES_DIR", str(SEED_DIR / "modules"))
    monkeypatch.setenv("SELFIM_SCAFFOLD_DIR", str(SEED_DIR / "scaffold"))


def _load_entrypoint():
    """scaffold/entrypoint.py をテストプロセスへ動的ロードするヘルパー。

    コンテナ内では PYTHONPATH=/app で `from runtime import agent_runtime` が
    解決される。in-processテストではこれを再現するためハーネスルートを
    sys.path へ一時的に追加する。
    """
    import importlib.util
    import sys

    if str(SEED_DIR) not in sys.path:
        sys.path.insert(0, str(SEED_DIR))
    path = SEED_DIR / "scaffold" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("seed_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task_dict() -> dict:
    """テスト用タスク(TaskSpec.model_dump と同じ形状)。"""
    return {
        "task_id": "legal-001",
        "prompt": "この事案について判断を示してください。",
        "context": "事実: 被告は契約書に署名した。\n事実: 署名日は3月1日である。",
        "reasoning_steps_required": ["契約の成立", "効力の障害", "結論の導出"],
    }


class TestSeedLayout:
    def test_real_seed_harness_has_all_seven_modules(self) -> None:
        # リポジトリの実シードハーネスが7モジュール+runtime+.dockerignoreを備えること
        for name, rel in MODULE_PATHS.items():
            assert (SEED_DIR / rel).exists(), f"実シードに {name} ({rel}) がない"
        assert (RUNTIME_DIR / "app.py").exists()
        assert (RUNTIME_DIR / "agent_runtime.py").exists()
        assert (RUNTIME_DIR / "mock_provider.py").exists()
        assert (SEED_DIR / ".dockerignore").exists()

    def test_dockerfile_uses_layer_caching_order(self) -> None:
        # requirements の COPY+install がコード COPY より先にあること(ビルド高速化)
        dockerfile = (SEED_DIR / "scaffold" / "Dockerfile").read_text(encoding="utf-8")
        assert dockerfile.index("requirements.txt") < dockerfile.index("COPY modules/")
        assert "COPY modules/" in dockerfile

    def test_dockerignore_excludes_candidates(self) -> None:
        # 候補ディレクトリがビルドコンテキストに混入しないこと(コピーコスト・漏洩防止)
        ignore = (SEED_DIR / ".dockerignore").read_text(encoding="utf-8")
        assert "candidates/" in ignore


class TestEntrypointRunTask:
    def test_run_task_returns_envelope_dict(self, seed_env: None) -> None:
        entrypoint = _load_entrypoint()
        result = entrypoint.run_task(_task_dict())

        # InvocationEnvelope の全キーを持ち、成功していること
        assert set(result.keys()) >= {
            "ok",
            "output_text",
            "composed_prompt",
            "call_log",
            "token_usage",
            "error",
        }
        assert result["ok"] is True
        assert result["error"] == ""
        # 結論行とステップが出力にあること(シードはマーカーなし→先頭2ステップ)
        assert "結論" in result["output_text"]
        assert "ステップ1" in result["output_text"]
        assert "ステップ3" not in result["output_text"]

    def test_call_log_records_tool_and_verify(self, seed_env: None) -> None:
        entrypoint = _load_entrypoint()
        result = entrypoint.run_task(_task_dict())
        files = [e["module_file"] for e in result["call_log"]]
        assert "tools.py" in files
        assert "verify.py" in files
        # verify のチェックリストが出力に反映されていること
        assert "検証チェック" in result["output_text"]

    def test_composed_prompt_matches_local_runner_rule(self, seed_env: None) -> None:
        # local実行系(AgentRunner._compose_prompt)と同一の結合規則であること。
        # これにより docker実行でも「内容包含」の実行証拠判定が同一セマンティクスになる。
        from selfim.agent_runner import AgentRunner

        entrypoint = _load_entrypoint()
        result = entrypoint.run_task(_task_dict())
        local = AgentRunner(harness_dir=SEED_DIR, model=object())
        sources = local._load_sources()
        assert result["composed_prompt"] == local._compose_prompt(sources)
        # シードの system_prompt 全文が組込まれていること(包含証拠の前提)
        assert sources["system_prompt"].strip() in result["composed_prompt"]


class TestMockProviderMarkers:
    """コンテナ内mockプロバイダのマーカー駆動挙動(設計判断 D-5 の移植)。"""

    def test_all_steps_marker_changes_output(
        self, seed_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # system_prompt に MARKER_ALL_STEPS 相当の文言を追加すると全ステップ出力になる
        import shutil

        work = tmp_path / "modules"
        shutil.copytree(SEED_DIR / "modules", work)
        prompt = work / "system_prompt.txt"
        prompt.write_text(
            prompt.read_text(encoding="utf-8") + "\nステップn: すべての思考ステップを明示せよ。\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("SELFIM_MODULES_DIR", str(work))
        entrypoint = _load_entrypoint()
        result = entrypoint.run_task(_task_dict())
        assert "ステップ3" in result["output_text"]


class TestAppInvoke:
    def test_invoke_converts_entrypoint_exception_to_error_envelope(
        self, seed_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # エントポイントが例外を投げてもHTTPサーバは500で死なず、
        # ok=False のエラーenvelopeを返す(コンテナ再起動不要にする固定runtimeの役割)
        import sys

        if str(SEED_DIR) not in sys.path:
            sys.path.insert(0, str(SEED_DIR))
        import runtime.app as runtime_app  # bedrock_agentcore に依存しない設計のためimport可能

        monkeypatch.setattr(
            runtime_app,
            "_load_entrypoint",
            lambda: type("Broken", (), {"run_task": lambda self, t: (_ for _ in ()).throw(RuntimeError("boom"))})(),
        )
        result = runtime_app.invoke({"task_id": "t1"})
        assert result["ok"] is False
        assert "boom" in result["error"]
