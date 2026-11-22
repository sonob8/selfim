"""Docker実行系(ImageBuilder / ContainerSession / DockerAgentRunner)の単体テスト。

検証観点(fake DockerClient 使用・実Docker不要):
- ImageBuilder がビルド失敗を BuildResult(ok=False, log_tail) に変換すること
- ContainerSession がエフェメラルポートで/ping待機・/invocations呼出・ログ保存・削除をすること
- DockerAgentRunner がenvelopeを AgentOutput に変換し(call_log復元・tokens実測)、
  実行失敗を [INVOCATION_ERROR] 出力に落とすこと
- build 失敗時はコンテナを起動せず build_ok=False となること
- ビルド・スモーク成否が execution_evidence に反映されること
- 設定(config_loader)が executor / docker ブロック / ship を読めること
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from selfim.schemas import TaskSpec

import pytest


# ---------------------------------------------------------------------------
# fake Docker SDK オブジェクト
# ---------------------------------------------------------------------------


class FakeImage:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.id = "sha256:" + "a" * 64


class FakeBuildError(Exception):
    pass


class FakeImages:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.build_calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> tuple[FakeImage, Any]:
        self.build_calls.append(kwargs)
        if self.fail:
            raise FakeBuildError("SyntaxError in Dockerfile")
        return FakeImage(kwargs.get("tag", "")), iter([])

    def get(self, tag: str) -> FakeImage:
        return FakeImage(tag)


class FakeContainer:
    def __init__(self, host_port: int = 12345) -> None:
        self.attrs: dict[str, Any] = {
            "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": str(host_port)}]}}
        }
        self.removed = False
        self.stopped = False

    def logs(self) -> bytes:
        return b"container log line"

    def reload(self) -> None:
        """docker SDK の attrs 再取得相当(fake では何もしない)。"""

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeContainers:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.run_calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> FakeContainer:
        self.run_calls.append(kwargs)
        return self.container


class FakeDockerClient:
    """docker SDK クライアントの最小fake(DockerAgentRunner が触る面のみ)。"""

    def __init__(self, host_port: int = 12345, build_fail: bool = False) -> None:
        self.images = FakeImages(fail=build_fail)
        self.containers = FakeContainers(FakeContainer(host_port))


def _ok_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "output_text": "結論: ...",
        "composed_prompt": "SP\n\nSK",
        "call_log": [
            {"module_file": "tools.py", "function_name": "outline"},
        ],
        "token_usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "error": "",
    }


def _task() -> TaskSpec:
    return TaskSpec(task_id="t1", prompt="判断せよ", context="事実: x", reasoning_steps_required=["a", "b"])


# ---------------------------------------------------------------------------
# ImageBuilder / ContainerSession
# ---------------------------------------------------------------------------


class TestImageBuilder:
    def test_build_success_returns_ok(self, tmp_path: Path) -> None:
        from selfim.docker_executor import ImageBuilder

        client = FakeDockerClient()
        result = ImageBuilder(client=client).build(tmp_path, "selfim-run1-s1")

        assert result.ok is True
        assert result.tag == "selfim-run1-s1"
        assert client.images.build_calls[0]["path"] == str(tmp_path)

    def test_build_failure_returns_log_tail(self, tmp_path: Path) -> None:
        from selfim.docker_executor import ImageBuilder

        client = FakeDockerClient(build_fail=True)
        result = ImageBuilder(client=client).build(tmp_path, "selfim-run1-s1")

        assert result.ok is False
        # 失敗ログに例外種別・理由が残ること
        assert "FakeBuildError" in result.log_tail
        assert "Dockerfile" in result.log_tail


class TestContainerSession:
    def test_start_invoke_stop_lifecycle(self, tmp_path: Path) -> None:
        from selfim.container_session import ContainerSession

        client = FakeDockerClient(host_port=15555)
        urls_hit: list[str] = []

        def fake_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
            urls_hit.append(url)
            return 200, json.dumps(_ok_envelope()).encode("utf-8")

        def fake_ping(url: str, timeout: float) -> bool:
            urls_hit.append(url)
            return True

        session = ContainerSession(
            client=client,
            image_tag="selfim-run1-s1",
            name="selfim-run1-s1-c",
            env={"SELFIM_MODEL_PROVIDER": "mock"},
            container_port=8080,
            ping_timeout_sec=5,
            ping_interval_sec=0.01,
            invocation_timeout_sec=5,
            logs_dir=tmp_path,
            transport=fake_transport,
            ping=fake_ping,
        )
        with session:
            envelope = session.invoke({"task_id": "t1"})
            assert envelope["ok"] is True

        # エフェメラルポート(15555)へ/ping後に/invocationsしたこと
        assert any(u.endswith("/ping") for u in urls_hit)
        assert any("127.0.0.1:15555/invocations" in u for u in urls_hit)
        # コンテナが削除され、ログが保存されたこと
        assert client.containers.container.removed is True
        assert (tmp_path / "selfim-run1-s1-c.log").exists()

    def test_run_publishes_port_on_loopback_only(self, tmp_path: Path) -> None:
        """ポート公開はエフェメラル+ループバック限定(ネットワーク無効化は併用しない)。

        [DECISION 2026-11-22] --network none / internal はポート公開と両立しない
        (検証済み)ため、network_disabled は False のまま運用する。
        """
        from selfim.container_session import ContainerSession

        client = FakeDockerClient()

        session = ContainerSession(
            client=client,
            image_tag="img",
            name="c1",
            env={"SELFIM_MODEL_PROVIDER": "mock"},
            container_port=8080,
            ping_timeout_sec=5,
            ping_interval_sec=0.01,
            invocation_timeout_sec=5,
            logs_dir=tmp_path,
            transport=lambda u, b, t: (200, json.dumps(_ok_envelope()).encode()),
            ping=lambda u, t: True,
        )
        with session:
            pass
        call = client.containers.run_calls[0]
        # ホストポートはdockerに任せ、公開先はループバックに限定する
        assert call["ports"] == {"8080/tcp": ("127.0.0.1", None)}


# ---------------------------------------------------------------------------
# DockerAgentRunner
# ---------------------------------------------------------------------------


def _runner(
    tmp_path: Path,
    transport_status: int = 200,
    transport_body: bytes | None = None,
    build_fail: bool = False,
) -> tuple[Any, FakeDockerClient]:
    from selfim.config_loader import DockerSettings
    from selfim.docker_executor import DockerAgentRunner

    client = FakeDockerClient(build_fail=build_fail)
    body = transport_body if transport_body is not None else json.dumps(_ok_envelope()).encode()

    def fake_transport(url: str, req_body: bytes, timeout: float) -> tuple[int, bytes]:
        return transport_status, body

    runner = DockerAgentRunner(
        harness_dir=tmp_path / "harness",
        model_provider="mock",
        run_id="run1",
        candidate_label="s1",
        settings=DockerSettings(
            ping_timeout_sec=5, ping_interval_sec=0.01, invocation_timeout_sec=5
        ),
        logs_dir=tmp_path,
        client=client,
        transport=fake_transport,
        ping=lambda u, t: True,
    )
    return runner, client


class TestDockerAgentRunner:
    def test_run_task_converts_envelope_to_agent_output(self, tmp_path: Path) -> None:
        runner, _client = _runner(tmp_path)
        out = runner.run_task(_task())

        assert out.text == "結論: ..."
        assert out.prompt == "SP\n\nSK"  # envelope の composed_prompt
        assert out.tokens == 30  # envelope の実測 token_usage
        assert out.envelope is not None and out.envelope.ok is True
        assert out.error == ""
        assert out.call_log.count_for("tools.py") == 1
        runner.close()

    def test_invocation_failure_returns_error_output(self, tmp_path: Path) -> None:
        runner, client = _runner(tmp_path, transport_status=500, transport_body=b"err")
        out = runner.run_task(_task())

        assert out.text.startswith("[INVOCATION_ERROR]")
        assert out.error != ""
        assert out.tokens == 0
        # コンテナは後始末される
        runner.close()
        assert client.containers.container.removed is True

    def test_build_failure_skips_container_and_flags_build_ok(self, tmp_path: Path) -> None:
        runner, client = _runner(tmp_path, build_fail=True)
        out = runner.run_task(_task())

        assert out.text.startswith("[INVOCATION_ERROR]")
        assert runner.build_ok is False
        # コンテナは起動していない
        assert client.containers.run_calls == []
        runner.close()

    def test_execution_evidence_reflects_build_and_smoke(self, tmp_path: Path) -> None:
        runner, _client = _runner(tmp_path)
        out = runner.run_task(_task())
        assert runner.build_ok is True and runner.smoke_ok is True

        ev_dockerfile = runner.execution_evidence(out, "dockerfile")
        assert ev_dockerfile.module_imported is True
        assert ev_dockerfile.tool_calls == 1

        ev_tool = runner.execution_evidence(out, "tool")
        assert ev_tool.tool_calls == 1  # envelope の call_log から復元
        runner.close()

    def test_mock_env_is_minimal(self, tmp_path: Path) -> None:
        """mock時のコンテナenvは SELFIM_MODEL_PROVIDER のみ(最小権限)。"""
        runner, client = _runner(tmp_path)
        runner.run_task(_task())

        call = client.containers.run_calls[0]
        assert call["environment"] == {"SELFIM_MODEL_PROVIDER": "mock"}
        runner.close()

    def test_bedrock_env_passes_aws_keys_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bedrock時はAWS系envのみパススルーし、他は渡さない。"""
        from selfim.config_loader import DockerSettings
        from selfim.docker_executor import DockerAgentRunner

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
        monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.test")
        monkeypatch.setenv("SOME_UNRELATED_SECRET", "leak-me")

        client = FakeDockerClient()
        runner = DockerAgentRunner(
            harness_dir=tmp_path / "harness",
            model_provider="bedrock",
            run_id="run1",
            candidate_label="s1",
            settings=DockerSettings(ping_timeout_sec=5, ping_interval_sec=0.01),
            logs_dir=tmp_path,
            client=client,
            transport=lambda u, b, t: (200, json.dumps(_ok_envelope()).encode()),
            ping=lambda u, t: True,
        )
        runner.run_task(_task())

        env = client.containers.run_calls[0]["environment"]
        assert env["SELFIM_MODEL_PROVIDER"] == "bedrock"
        assert env["AWS_ACCESS_KEY_ID"] == "AKIA-test"
        assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
        assert env["BEDROCK_REGION"] == "us-west-2"
        assert env["BEDROCK_MODEL_ID"] == "anthropic.test"
        assert "SOME_UNRELATED_SECRET" not in env  # 無関係な秘密は渡さない
        runner.close()

    def test_transport_connection_error_returns_error_output(
        self, tmp_path: Path
    ) -> None:
        """接続失敗(OSError系)も例外を伝播させずエラー出力に変換すること。"""
        from selfim.container_session import InvocationError
        from selfim.config_loader import DockerSettings
        from selfim.docker_executor import DockerAgentRunner

        def broken_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
            raise InvocationError("接続失敗: ConnectionRefusedError")

        client = FakeDockerClient()
        runner = DockerAgentRunner(
            harness_dir=tmp_path / "harness",
            model_provider="mock",
            run_id="run1",
            candidate_label="s1",
            settings=DockerSettings(ping_timeout_sec=5, ping_interval_sec=0.01),
            logs_dir=tmp_path,
            client=client,
            transport=broken_transport,
            ping=lambda u, t: True,
        )
        out = runner.run_task(_task())
        assert out.text.startswith("[INVOCATION_ERROR]")
        assert "接続失敗" in out.error
        runner.close()

    def test_invalid_json_response_returns_error_output(self, tmp_path: Path) -> None:
        runner, _client = _runner(tmp_path, transport_body=b"not-json")
        out = runner.run_task(_task())
        assert out.text.startswith("[INVOCATION_ERROR]")
        runner.close()

    def test_envelope_ok_false_sets_smoke_failed(self, tmp_path: Path) -> None:
        bad = _ok_envelope() | {"ok": False, "error": "RuntimeError: boom"}
        runner, _client = _runner(tmp_path, transport_body=json.dumps(bad).encode())
        out = runner.run_task(_task())

        assert out.text.startswith("[INVOCATION_ERROR]")
        assert "boom" in out.error
        assert runner.smoke_ok is False  # 一度も成功していないのでスモーク失敗
        runner.close()

    def test_transient_failure_does_not_poison_smoke(self, tmp_path: Path) -> None:
        """一度の ok=False の後に成功すれば smoke_ok=True を維持すること。"""
        from selfim.config_loader import DockerSettings
        from selfim.docker_executor import DockerAgentRunner

        state = {"calls": 0}

        def flaky_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
            state["calls"] += 1
            if state["calls"] == 1:
                bad = _ok_envelope() | {"ok": False, "error": "transient"}
                return 200, json.dumps(bad).encode()
            return 200, json.dumps(_ok_envelope()).encode()

        client = FakeDockerClient()
        runner = DockerAgentRunner(
            harness_dir=tmp_path / "harness",
            model_provider="mock",
            run_id="run1",
            candidate_label="s1",
            settings=DockerSettings(ping_timeout_sec=5, ping_interval_sec=0.01),
            logs_dir=tmp_path,
            client=client,
            transport=flaky_transport,
            ping=lambda u, t: True,
        )
        runner.run_task(_task())  # 1回目: ok=False
        out = runner.run_task(_task())  # 2回目: 成功
        assert out.envelope is not None and out.envelope.ok is True
        assert runner.smoke_ok is True
        runner.close()


# ---------------------------------------------------------------------------
# config_loader 拡張
# ---------------------------------------------------------------------------


class TestConfigExtension:
    def test_defaults_include_executor_docker_ship(self) -> None:
        from selfim.config_loader import _DEFAULTS

        assert _DEFAULTS["executor"] == "local"
        assert "docker" in _DEFAULTS and "ship" in _DEFAULTS

    def test_load_settings_parses_docker_block(self, tmp_path: Path) -> None:
        from selfim.config_loader import load_settings

        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text(
            "executor: docker\n"
            "docker:\n"
            "  container_port: 8080\n"
            "  ping_timeout_sec: 30\n"
            "  invocation_timeout_sec: 60\n"
            "  keep_containers: true\n"
            "ship:\n"
            "  enabled: true\n",
            encoding="utf-8",
        )
        settings = load_settings(settings_path)

        assert settings.executor == "docker"
        assert settings.docker.container_port == 8080
        assert settings.docker.ping_timeout_sec == 30.0
        assert settings.docker.invocation_timeout_sec == 60.0
        assert settings.docker.keep_containers is True
        assert settings.ship.enabled is True

    def test_load_settings_defaults_when_keys_absent(self, tmp_path: Path) -> None:
        from selfim.config_loader import load_settings

        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text("n_rounds: 2\n", encoding="utf-8")
        settings = load_settings(settings_path)

        assert settings.executor == "local"
        assert settings.docker.container_port == 8080
        assert settings.docker.ping_timeout_sec == 60.0
        assert settings.ship.enabled is True
