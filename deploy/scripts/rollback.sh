#!/usr/bin/env bash
# 回退到升级前：scripts/rollback.sh <旧版本号> <升级前备份文件>
# .env 版本号改回旧版本 → 确认旧版本镜像 → 用升级前备份覆盖数据库（restore.sh，需输入 yes）→ 启动旧版本
# 新版本已迁移过数据库，旧版本不能直接在新库上运行，所以回退必须配合升级前的备份（upgrade.sh 会自动备份）
source "$(dirname "$0")/common.sh"
need_docker; need_env
v="${1:-}"; f="${2:-}"
{ [ -n "$v" ] && [ -f "$f" ]; } || die "用法：scripts/rollback.sh <旧版本号> <升级前备份文件>，例如 make rollback V=0.4.0 F=backups/hcs-inspector-20261101-020000.tar.gz"
bv="$(tar -xzOf "$f" ./MANIFEST 2>/dev/null | sed -n 's/^version=\([^ ]*\).*/\1/p' || true)"
[ -z "$bv" ] || [ "$bv" = "$v" ] || c_warn "备份记录的版本是 v$bv，与回退目标 v$v 不一致（数据库结构以备份为准，建议回退到 v$bv）"
cur="$(env_get HCS_INSPECTOR_VERSION)"
done_ok=0
on_exit() { if [ "$done_ok" != 1 ]; then sed -i "s/^HCS_INSPECTOR_VERSION=.*/HCS_INSPECTOR_VERSION=${cur}/" .env; c_warn "回退未完成，.env 版本号保持 v$cur"; fi; }
trap on_exit EXIT
sed -i "s/^HCS_INSPECTOR_VERSION=.*/HCS_INSPECTOR_VERSION=${v}/" .env
ensure_images
c_info "回退 v$cur → v$v：用备份恢复数据库"
scripts/restore.sh "$f"
done_ok=1
c_ok "已回退到 v$v"
