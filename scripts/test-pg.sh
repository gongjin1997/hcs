#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
container=""
cleanup() { if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null; fi; }
trap cleanup EXIT
if [[ -z "${PG_URL:-}" ]]; then
  container="hcs-inspector-test-pg-$$"
  password="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(24))')"
  port="${PG_PORT:-55432}"
  docker run --rm -d --name "$container" -e POSTGRES_USER=hcs_test -e POSTGRES_DB=hcs_test \
    -e POSTGRES_PASSWORD="$password" -p "127.0.0.1:$port:5432" postgres:16-alpine >/dev/null
  ready=false
  for ((attempt=0; attempt<60; attempt++)); do
    if docker exec "$container" pg_isready -U hcs_test -d hcs_test >/dev/null 2>&1; then ready=true; break; fi
    sleep 1
  done
  if [[ "$ready" != true ]]; then printf 'PostgreSQL 测试容器未就绪\n' >&2; exit 1; fi
  export PG_URL="postgresql+psycopg://hcs_test:$password@127.0.0.1:$port/hcs_test"
fi
export INSPECTOR_TEST_DATABASE_URL="$PG_URL"
# conftest 为每次运行创建随机独立 schema，绝不清空 public 或其他项目数据。
.venv/bin/python -m pytest "$@"
