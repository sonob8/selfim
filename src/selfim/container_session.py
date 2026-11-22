"""ContainerSession — 候補イメージのコンテナライフサイクル管理。

責務:
- docker SDK でコンテナを detach 起動し、エフェメラルホストポートで公開する
- /ping が 200 を返すまで起動を待つ(AgentCore 互換サーバの準備完了判定)
- POST /invocations でタスクを投入し JSON 応答を返す
- 停止時にコンテナログを logs_dir へ保存し、コンテナを削除する

[DECISION 2026-11-22] ホストポートは固定しない(ports={"8080/tcp": None} で
docker に空きポートを割り当てさせ、container.attrs から読み取る)。
並行候補評価・複数runでのポート衝突を構造的に排除するため。
HTTP には urllib(標準ライブラリ)を使い、依存を増やさない。
transport は差し替え可能にして、実Dockerなしの単体テストを可能にする。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# HTTP 通信の差し替え用シグネチャ: (url, body, timeout) -> (status_code, body_bytes)。
Transport = Callable[[str, bytes, float], tuple[int, bytes]]
# /ping ヘルスチェックの差し替え用シグネチャ: (url, timeout) -> 200ならTrue。
Ping = Callable[[str, float], bool]


class InvocationError(Exception):
    """/invocations 呼出の失敗(非200・JSON不正・接続不能)。"""


def _urllib_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """標準ライブラリによる実際のHTTP通信(既定transport)。

    接続拒否・タイムアウト(URLError/OSError 系)も InvocationError へ変換する。
    これにより run_task は「例外をループに伝播させない」契約を守れる
    (ping 成功直後にコンテナが死んだ場合等にループ全体が落ちるのを防ぐ)。
    """
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise InvocationError(f"接続失敗: {url}: {exc}") from exc


def _urllib_ping(url: str, timeout: float) -> bool:
    """GET /ping が 200 を返すか(失敗は False)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


class ContainerSession:
    """1候補イメージのコンテナセッション(1候補=1コンテナをスイート全体で再利用)。

    [DECISION 2026-11-22] タスクごとではなく候補ごとにコンテナを起動する。
    コンテナ起動はWSL2で概ね0.5〜2秒かかるのに対し、スイートは
    tasks×rollouts 回のinvocationを要するため、都度起動は非効率である。
    タスク間の状態汚染は local 実行系(同一プロセスで全タスク実行)と
    同じセマンティクスとして許容する。
    """

    def __init__(
        self,
        client: Any,
        image_tag: str,
        name: str,
        env: dict[str, str],
        container_port: int = 8080,
        ping_timeout_sec: float = 60.0,
        ping_interval_sec: float = 0.5,
        invocation_timeout_sec: float = 120.0,
        logs_dir: Path | None = None,
        keep: bool = False,
        network_disabled: bool = False,
        transport: Transport = _urllib_transport,
        ping: Ping = _urllib_ping,
    ) -> None:
        """セッションの設定を束縛する(start は遅延実行)。

        Args:
            client: docker SDK クライアント(docker.from_env() の返り値、テストではfake)。
            image_tag: 起動するイメージタグ。
            name: コンテナ名(ログファイル名にも使う)。
            env: コンテナへ渡す環境変数(SELFIM_MODEL_PROVIDER 等、必要最小限)。
            container_port: コンテナ内ポート(AgentCore 規定は 8080)。
            ping_timeout_sec: /ping 待ちの上限秒。
            ping_interval_sec: /ping ポーリング間隔秒。
            invocation_timeout_sec: 1invocation のタイムアウト秒。
            logs_dir: コンテナログの保存先(None なら保存しない)。
            keep: true はコンテナを削除しない(デバッグ用)。
            network_disabled: true はネットワーク無効で起動(mock用)。
            transport: HTTP POST の実装(テストで差し替え)。
            ping: /ping ヘルスチェックの実装(テストで差し替え)。
        """
        self.client = client
        self.image_tag = image_tag
        self.name = name
        self.env = env
        self.container_port = container_port
        self.ping_timeout_sec = ping_timeout_sec
        self.ping_interval_sec = ping_interval_sec
        self.invocation_timeout_sec = invocation_timeout_sec
        self.logs_dir = logs_dir
        self.keep = keep
        self.network_disabled = network_disabled
        self.transport = transport
        self.ping = ping
        self._container: Any = None
        self._host_port: int | None = None

    @property
    def host_port(self) -> int:
        """割り当て済みホストポートを返す(未起動なら RuntimeError)。"""
        if self._host_port is None:
            raise RuntimeError("コンテナが未起動です(start() を先に呼ぶこと)")
        return self._host_port

    def start(self) -> None:
        """コンテナを起動し、/ping が通るまで待つ。

        Raises:
            RuntimeError: コンテナ起動後、ping_timeout_sec 内に /ping が通らない場合。
        """
        if self._container is not None:
            return  # 冪等:起動済みなら何もしない
        self._container = self.client.containers.run(
            image=self.image_tag,
            detach=True,
            name=self.name,
            environment=dict(self.env),
            # ("127.0.0.1", None): ホストポートは docker に空きポートを割り当てさせ、
            # 公開先はループバックに限定する(候補コードのHTTPサーバへLANから
            # 到達可能にしないため。レビュー2026-11-22指摘)。
            ports={f"{self.container_port}/tcp": ("127.0.0.1", None)},
            auto_remove=False,
            network_disabled=self.network_disabled,
        )
        self._container.reload()
        bindings = self._container.attrs["NetworkSettings"]["Ports"].get(
            f"{self.container_port}/tcp"
        )
        if not bindings:
            raise RuntimeError(f"ホストポートが割り当てられなかった: {self.image_tag}")
        self._host_port = int(bindings[0]["HostPort"])
        logger.info("container %s started on 127.0.0.1:%d", self.name, self._host_port)

        deadline = time.monotonic() + self.ping_timeout_sec
        while time.monotonic() < deadline:
            if self.ping(f"http://127.0.0.1:{self._host_port}/ping", 2.0):
                return
            time.sleep(self.ping_interval_sec)
        raise RuntimeError(
            f"コンテナ {self.name} が /ping に応答しない(起動失敗またはタイムアウト)"
        )

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """/invocations へタスクをPOSTし、JSON応答dictを返す。

        Args:
            payload: タスクdict(TaskSpec.model_dump())。

        Returns:
            応答JSONを解析した dict。

        Raises:
            RuntimeError: コンテナ未起動の場合。
            InvocationError: 非200・JSON不正の場合。
        """
        if self._container is None:
            raise RuntimeError("コンテナが未起動です(start() を先に呼ぶこと)")
        url = f"http://127.0.0.1:{self.host_port}/invocations"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, resp_body = self.transport(url, body, self.invocation_timeout_sec)
        if status != 200:
            raise InvocationError(f"invocation 失敗: HTTP {status}: {resp_body[:200]!r}")
        try:
            parsed = json.loads(resp_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvocationError(f"invocation 応答がJSONでない: {exc}") from exc
        if not isinstance(parsed, dict):
            raise InvocationError("invocation 応答がJSONオブジェクトでない")
        return parsed

    def stop(self) -> None:
        """コンテナログを保存し、コンテナを削除する(冪等)。"""
        if self._container is None:
            return
        try:
            if self.logs_dir is not None:
                self.logs_dir.mkdir(parents=True, exist_ok=True)
                log_path = self.logs_dir / f"{self.name}.log"
                # append-only:ループ実行ログは追記のみ(プロジェクト規約)。
                with log_path.open("ab") as f:
                    f.write(self._container.logs())
            if not self.keep:
                self._container.remove(force=True)
                logger.info("container %s removed", self.name)
        except Exception:  # noqa: BLE001 - 後始末失敗で評価結果を落とさない
            logger.exception("container cleanup failed: %s", self.name)
        finally:
            self._container = None
            self._host_port = None

    def __enter__(self) -> "ContainerSession":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
