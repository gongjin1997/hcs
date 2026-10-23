#!/usr/bin/env bash
# 升级：备份（旧版本）→ 导入新版本镜像 → 先迁移数据库（新版本 migrate）→ 再替换 scheduler / notifier / api / web → 健康检查
# 用法：scripts/upgrade.sh [新版本号]     # 不带参数时使用 .env 中的 HCS_INSPECTOR_VERSION
#       SKIP_BACKUP=1 scripts/upgrade.sh 0.4.1
# 回退：scripts/rollback.sh <旧版本号> <升级前备份文件>（make rollback V= F=）
source "$(dirname "$0")/common.sh"
need_docker; need_env
old="$(env_get HCS_INSPECTOR_VERSION)"
new="${1:-$old}"
set_version() { sed -i "s/^HCS_INSPECTOR_VERSION=.*/HCS_INSPECTOR_VERSION=$1/" .env; }

backup=""
if [ "${SKIP_BACKUP:-0}" != "1" ] && [ -n "$(dc ps -q postgres 2>/dev/null)" ]; then
  c_info "升级前备份（v$old）"
  scripts/backup.sh || die "备份失败，已中止升级（确认风险后可 SKIP_BACKUP=1 跳过）"
  backup="$(ls -1t "$(env_get BACKUP_DIR ./backups)"/hcs-inspector-*.tar.gz 2>/dev/null | head -1 || true)"
fi

# 迁移成功之前任何一步失败，都把 .env 版本号恢复为旧版本（旧进程一直在运行）
revert=0
on_exit() { if [ "$revert" = 1 ]; then set_version "$old"; c_warn "升级未完成，.env 版本号已恢复为 $old"; fi; }
trap on_exit EXIT
if [ "$new" != "$old" ]; then
  c_info "版本 $old → $new（更新 .env 中的 HCS_INSPECTOR_VERSION）"
  set_version "$new"; revert=1
fi
ensure_images

dc up -d postgres redis
wait_healthy postgres 120 || die "PostgreSQL 未就绪"
c_info "先执行新版本数据库迁移（旧进程仍在运行；迁移脚本保证向前兼容，失败时整体回滚）"
dc run --rm migrate || die "迁移失败，旧版本保持运行；保留上面的输出排查后重试"
revert=0   # 数据库已是新版本结构：之后的失败不再自动改回版本号，按提示回退

c_info "替换后台进程与 API"
dc up -d --no-deps scheduler notifier
dc up -d --no-deps api
wait_healthy api 180 || die "新版本 API 未就绪：make logs S=api；回退到升级前：make rollback V=$old F=${backup:-<升级前备份文件>}"
dc up -d --no-deps web
dc up -d --remove-orphans
c_ok "升级完成：v$new${backup:+（升级前备份：$backup）}"
scripts/status.sh || true
