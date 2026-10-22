# ---- HCS 巡检平台后端镜像（api / scheduler / notifier 共用，启动命令不同）----
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Shanghai
WORKDIR /app
RUN groupadd -r app && useradd -r -g app app
COPY pyproject.toml ./
COPY app ./app
COPY mock_hcs ./mock_hcs
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install ".[postgres]"
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
