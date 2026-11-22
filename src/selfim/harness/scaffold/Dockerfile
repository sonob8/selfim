# デプロイ境界モジュール(編集可能)。
# 制約(論文B Table III): data/ papers/ の COPY/ADD とホストパスのマウントは禁止。
# ベースイメージ・依存の変更のみを行うこと。
FROM python:3.12-slim

WORKDIR /app

# 依存を先にコピーしてレイヤキャッシュを効かせる(モジュール編集で再installさせない)。
COPY scaffold/requirements.txt /app/scaffold/requirements.txt
RUN pip install --no-cache-dir -r /app/scaffold/requirements.txt

# 固定ランタイム → 足場 → コンテンツモジュールの順にコピー。
COPY runtime/ /app/runtime/
COPY scaffold/ /app/scaffold/
COPY modules/ /app/modules/

ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8080

# AgentCore 互換サーバ(/invocations, /ping)を起動する。
CMD ["python", "-m", "runtime.app"]
