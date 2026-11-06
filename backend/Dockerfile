# ---- HCS 巡检平台后端镜像：api / scheduler / notifier / migrate 共用，启动命令不同 ----
# 构建：在仓库根目录 make build（或 deploy/scripts/offline-bundle.sh 打离线包）
FROM python:3.12-slim
# 内网构建可指定 PyPI 镜像：docker build --build-arg PIP_INDEX_URL=https://mirrors.example.com/pypi/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_INDEX_URL=${PIP_INDEX_URL} TZ=Asia/Shanghai \
    INSPECTOR_ENV=prod INSPECTOR_DEMO_SEED=false
WORKDIR /app
# /app/data：导出 / 探测报告等文件（compose 挂载命名卷）；/app/certs：内部 CA 证书（只读挂载）
RUN groupadd -r app && useradd -r -g app app && mkdir -p /app/data /app/certs && chown app:app /app/data
COPY pyproject.toml constraints.txt ./
COPY app ./app
COPY mock_hcs ./mock_hcs
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install -c constraints.txt ".[postgres]"
# 源码可能由严格 umask 创建（600 / 700）；保留 root 所有权，确保非 root 运行用户可读/遍历。
RUN chmod -R a+rX /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"
# --forwarded-allow-ips：信任 nginx 转发的客户端 IP（API 端口不对外暴露，只在 compose 内网可达）
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' --workers ${API_WORKERS:-2} --timeout-graceful-shutdown ${API_GRACEFUL_TIMEOUT:-8}"]
