"""Docker実行系 — 候補ハーネスのイメージビルドとコンテナ実行(TaskRunner 実装)。

責務:
- ImageBuilder: 候補ディレクトリ(=ビルドコンテキスト)からイメージをビルドする
- DockerAgentRunner: TaskRunner Protocol 実装。初回 run_task 時に
  「ビルド → コンテナ起動 → /ping 待ち」を行い、以降の呼出で /invocations を使う。
  実行成否(build_ok / smoke_ok)を検証ゲートへ供給する。

安全上の制約(論文B Table III):
- コンテナへ渡す環境変数は SELFIM_MODEL_PROVIDER のみ(bedrock時のみAWS系をパススルー)。
- 評価データ・ルーブリックはイメージに焼き込まず data/ をマウントもしない
  (タスク1件分のペイロードのみ実行時に /invocations へ渡る。ルーブリック・
  全タスク一覧は渡らない)。
- mock プロバイダ時は network_disabled で起動する(外部通信不要=持ち出し経路の遮断)。

[DECISION 2026-11-22] ループプロセスは候補コードを実行しない。tools/verify の
exec はコンテナ内(runtime/)で行われるため、この実行系はHTTP通信のみを行う。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from selfim.agent_runner import (
    AgentOutput,
    CallLog,
    build_execution_evidence,
)
from selfim.config_loader import DockerSettings
from selfim.container_session import ContainerSession, InvocationError, Transport, Ping
from selfim.schemas import ExecutionEvidence, InvocationEnvelope, TaskSpec

logger = logging.getLogger(__name__)

# 実行失敗出力のプレフィックス(ジャッジが不合格にする確実な目印)。
_INVOCATION_ERROR_PREFIX = "[INVOCATION_ERROR]"

# bedrockモード時のみコンテナへパススルーするAWS系環境変数。
# [DECISION 2026-11-22] 最小権限:mockモードでは何も渡さない。
_AWS_ENV_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@dataclass
class BuildResult:
    """docker build 1回の結果。

    Attributes:
        ok: ビルド成否。
        tag: ビルドに使ったイメージタグ。
        log_tail: 失敗時のログ末尾(ゲート拒否理由とログ保存に使う)。
    """

    ok: bool
    tag: str
    log_tail: str = ""


class ImageBuilder:
    """docker SDK でイメージをビルドする(クライアントは注入可能)。"""

    def __init__(self, client: Any = None) -> None:
        """クライアントを束縛する(None なら docker.from_env() を遅延初期化)。

        Args:
            client: docker SDK クライアント(テストではfakeを注入)。
        """
        self._client = client

    @property
    def client(self) -> Any:
        """docker SDK クライアント(初回アクセス時に遅延初期化する)。"""
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    def build(self, context_dir: Path, tag: str, timeout_sec: int = 300) -> BuildResult:
        """ビルドコンテキストからイメージをビルドする。

        Args:
            context_dir: ビルドコンテキスト(候補ハーネスディレクトリ)。
            tag: イメージタグ(selfim-<run_id>-<label>)。
            timeout_sec: ビルドタイムアウト秒(docker SDK の timeout へ渡す)。

        Returns:
            BuildResult(失敗時も例外を投げず ok=False で返す)。
        """
        try:
            self.client.images.build(path=str(context_dir), tag=tag, rm=True, timeout=timeout_sec)
            logger.info("image built: %s (context=%s)", tag, context_dir)
            return BuildResult(ok=True, tag=tag)
        except Exception as exc:  # noqa: BLE001 - ビルド失敗は候補却下で処理する
            log_tail = f"{type(exc).__name__}: {exc}"
            logger.warning("image build failed: %s (%s)", tag, log_tail)
            return BuildResult(ok=False, tag=tag, log_tail=log_tail)


class DockerAgentRunner:
    """候補ハーネスをイメージ化してコンテナ実行する TaskRunner。

    Attributes:
        harness_dir: 候補(またはワークスペース)のハーネスディレクトリ=ビルドコンテキスト。
        build_ok: 初回実行時のビルド成否(未実行なら True とみなす)。
        smoke_ok: コンテナ起動 + 有効なenvelope受取の成否。
    """

    def __init__(
        self,
        harness_dir: Path,
        model_provider: str,
        run_id: str,
        candidate_label: str,
        settings: DockerSettings,
        logs_dir: Path,
        client: Any = None,
        transport: Transport | None = None,
        ping: Ping | None = None,
    ) -> None:
        """実行に必要な設定を束縛する(ビルド・起動は初回 run_task まで遅延)。

        Args:
            harness_dir: ビルドコンテキストとするハーネスディレクトリ。
            model_provider: コンテナ内プロバイダ("mock" / "bedrock")。
            run_id: run識別子(イメージタグ・コンテナ名に使う)。
            candidate_label: 候補ラベル(s1, s2, ... / base)。イメージタグに使う。
            settings: docker実行系設定(タイムアウト等)。
            logs_dir: コンテナログの保存先(output/runs/<run_id>/logs/)。
            client: docker SDK クライアント(None なら ImageBuilder が遅延初期化)。
            transport: HTTP POST 実装(テストで差し替え。None なら実urllib)。
            ping: /ping 実装(テストで差し替え。None なら実urllib)。
        """
        self.harness_dir = harness_dir
        self.model_provider = model_provider
        self.image_tag = f"selfim-{run_id}-{candidate_label}"
        self.container_name = f"{self.image_tag}-c"
        self.settings = settings
        self.logs_dir = logs_dir
        self._client = client
        self._transport = transport
        self._ping = ping
        self._build: BuildResult | None = None
        self._session: ContainerSession | None = None
        self._smoke_ok: bool | None = None

    # ------------------------------------------------------------------
    # 内部: 遅延ビルド・起動
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """初回呼出時にビルドとコンテナ起動を行う(以降は何もしない)。"""
        if self._build is not None:
            return
        builder = ImageBuilder(client=self._client)
        self._build = builder.build(self.harness_dir, self.image_tag)
        if not self._build.ok:
            return  # ビルド失敗:コンテナは起動しない(ゲートで拒否される)
        session_kwargs: dict[str, Any] = {
            "container_port": self.settings.container_port,
            "ping_timeout_sec": self.settings.ping_timeout_sec,
            "ping_interval_sec": self.settings.ping_interval_sec,
            "invocation_timeout_sec": self.settings.invocation_timeout_sec,
            "logs_dir": self.logs_dir,
            "keep": self.settings.keep_containers,
        }
        if self._transport is not None:
            session_kwargs["transport"] = self._transport
        if self._ping is not None:
            session_kwargs["ping"] = self._ping
        self._session = ContainerSession(
            client=builder.client,
            image_tag=self.image_tag,
            name=self.container_name,
            env=self._container_env(),
            network_disabled=(
                self.model_provider == "mock" and self.settings.network_disabled_mock
            ),
            **session_kwargs,
        )
        try:
            self._session.start()
        except Exception:  # noqa: BLE001 - 起動失敗は全タスク失敗として処理する
            logger.exception("container start failed: %s", self.image_tag)
            self._smoke_ok = False

    def _container_env(self) -> dict[str, str]:
        """コンテナへ渡す環境変数(最小権限、論文B Table III)。

        mock: SELFIM_MODEL_PROVIDER のみ。
        bedrock: プロバイダ + AWS資格情報(ホストの env からパススルー)。
        """
        import os

        env = {"SELFIM_MODEL_PROVIDER": self.model_provider}
        if self.model_provider == "bedrock":
            for key in _AWS_ENV_KEYS:
                if key in os.environ:
                    env[key] = os.environ[key]
            region = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION"))
            if region:
                env["BEDROCK_REGION"] = region
            model_id = os.environ.get("BEDROCK_MODEL_ID")
            if model_id:
                env["BEDROCK_MODEL_ID"] = model_id
        return env

    # ------------------------------------------------------------------
    # TaskRunner Protocol
    # ------------------------------------------------------------------

    @property
    def build_ok(self) -> bool:
        """ビルド成否(未実行・成功は True、失敗は False)。"""
        return self._build is None or self._build.ok

    @property
    def smoke_ok(self) -> bool:
        """スモーク成否(有効なenvelopeを1回でも受信できたか)。

        セマンティクス: 成功が1回でもあれば True を維持する(新コードパスが
        実行された事実は一時的失敗で失われない)。1度も成功しないまま
        ok=False 応答があった場合のみ False。
        """
        return self._smoke_ok is not False

    def run_task(self, task: TaskSpec) -> AgentOutput:
        """1タスクをコンテナへ投入し、envelope を AgentOutput へ変換する。

        失敗(ビルド失敗・起動失敗・HTTPエラー・ok=False 応答)は
        [INVOCATION_ERROR] 付きの出力として返し、通常の評価フロー
        (ジャッジ不合格 → 非退行拒否)に乗せる。例外をループに伝播させない。

        Args:
            task: 実行するタスク。

        Returns:
            AgentOutput(envelope と token_usage を含む)。
        """
        self._ensure_ready()
        if self._build is not None and not self._build.ok:
            detail = self._build.log_tail
            return AgentOutput(
                text=f"{_INVOCATION_ERROR_PREFIX} build failed: {detail}",
                call_log=CallLog(),
                prompt="",
                error=f"build failed: {detail}",
            )
        if self._session is None:
            raise RuntimeError(
                "コンテナセッションが無効です(close() 後の再利用はできない。"
                "MainLoop は候補ごとに新しい実行器を組む前提)"
            )
        try:
            raw = self._session.invoke(task.model_dump())
            envelope = InvocationEnvelope.model_validate(raw)
        except (InvocationError, ValueError) as exc:
            detail = str(exc)
            logger.warning("invocation failed (%s): %s", self.image_tag, detail)
            return AgentOutput(
                text=f"{_INVOCATION_ERROR_PREFIX} {detail}",
                call_log=CallLog(),
                prompt="",
                error=detail,
            )
        if not envelope.ok:
            # ok=False はスモーク失敗候補。ただし成功経験があれば覆さない
            # (1候補=1コンテナ再利用時の一時的失敗で証拠を汚染しない)。
            if self._smoke_ok is None:
                self._smoke_ok = False
            return AgentOutput(
                text=f"{_INVOCATION_ERROR_PREFIX} {envelope.error}",
                call_log=CallLog(),
                prompt="",
                error=envelope.error,
            )
        # 有効なenvelopeを受信=新コードパス実行のスモーク成功(確定)
        self._smoke_ok = True
        return AgentOutput(
            text=envelope.output_text,
            call_log=CallLog.from_envelope(envelope),
            prompt=envelope.composed_prompt,
            tokens=envelope.token_usage.total_tokens,
            envelope=envelope,
        )

    def execution_evidence(self, output: AgentOutput, module: str) -> ExecutionEvidence:
        """モジュール種別ごとの実行エビデンス(build_ok/smoke_ok を反映)。"""
        return build_execution_evidence(
            output,
            module,
            self.harness_dir,
            build_ok=self.build_ok,
            smoke_ok=self.smoke_ok,
        )

    def close(self) -> None:
        """コンテナを停止・削除する(冪等)。"""
        if self._session is not None:
            self._session.stop()
            self._session = None
