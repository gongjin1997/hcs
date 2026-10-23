#!/usr/bin/env bash
# 恢复：scripts/restore.sh backups/hcs-inspector-<时间>.tar.gz [--with-env]
# 停止应用进程 → 用备份覆盖数据库 → 启动；--with-env 同时用备份里的 .env 覆盖当前 .env（主密钥必须与数据库匹配）
source "$(dirname "$0")/common.sh"
need_docker
f="${1:-}"; [ -f "$f" ] || die "用法：scripts/restore.sh <备份文件.tar.gz> [--with-env]"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
tar -xzf "$f" -C "$work"
[ -f "$work/db.dump" ] || die "备份文件里没有 db.dump"
cat "$work/MANIFEST" 2>/dev/null || true
if [ "${2:-}" = "--with-env" ]; then cp -p .env ".env.before-restore.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true; cp -p "$work/env" .env; fi
need_env
if ! grep -q "^INSPECTOR_MASTER_KEY=$(env_get INSPECTOR_MASTER_KEY)$" "$work/env" 2>/dev/null; then
  c_warn "当前 .env 的 INSPECTOR_MASTER_KEY 与备份不一致：恢复后租户凭据将无法解密（可加 --with-env 使用备份里的 .env）"
fi
read -r -p "将用备份覆盖当前数据库（当前数据会丢失），输入 yes 继续：" ans
[ "$ans" = "yes" ] || die "已取消"
db="$(env_get POSTGRES_DB inspector)"; user="$(env_get POSTGRES_USER inspector)"
dc stop web api scheduler notifier || true
dc up -d postgres; wait_healthy postgres 120 || die "PostgreSQL 未就绪"
c_info "恢复数据库 $db"
dc exec -T postgres psql -U "$user" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$db' AND pid<>pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"$db\";" -c "CREATE DATABASE \"$db\" OWNER \"$user\";"
dc exec -T postgres pg_restore -U "$user" -d "$db" --no-owner < "$work/db.dump"
c_info "迁移到当前版本并启动"
dc run --rm migrate
dc up -d
wait_healthy api 180 && c_ok "恢复完成" || die "API 未就绪：make logs S=api"
