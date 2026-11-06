#!/usr/bin/env bash
# 首次安装：检查环境 → 准备镜像（离线包 / 本机已有 / 源码构建）→ 启动数据库 → 迁移 + 初始化 → 启动全部服务 → 健康检查
source "$(dirname "$0")/common.sh"
need_docker
[ -f .env ] || { c_info "未找到 .env，自动生成"; scripts/gen-env.sh; }
need_env
prepare_configs
mkdir -p "$(env_get DATA_DIR ./data)" "$(env_get BACKUP_DIR ./backups)" exports
ensure_images
ensure_code

c_info "启动 PostgreSQL / Redis"
dc up -d --no-recreate postgres redis
wait_healthy postgres 120 || die "PostgreSQL 未就绪：bash deploy.sh logs postgres"
wait_healthy redis 120 || die "Redis 未就绪：bash deploy.sh logs redis"

c_info "数据库迁移 + 初始化（内置规则 / 巡检任务 / admin）"
dc run --rm -T --no-deps migrate || die "迁移失败：查看上面的输出；修复后重新执行 bash deploy.sh（可重复执行）"

c_info "启动 API / 调度 / 通知 / Web"
start_application

c_ok "部署完成：$(env_get INSPECTOR_BASE_URL)"
pw="$(env_get INSPECTOR_INIT_ADMIN_PASSWORD)"
if [ -n "$pw" ]; then
  c_warn "admin 初始口令见 .env 的 INSPECTOR_INIT_ADMIN_PASSWORD；首次登录后立即修改，并把该行清空"
fi
echo "已有租户、开关及邮件配置保留。新装的采集、巡检任务默认关闭，请按需启用。"
