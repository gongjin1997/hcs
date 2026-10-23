#!/usr/bin/env bash
# 首次安装：检查环境 → 准备镜像（离线包 / 本机已有 / 源码构建）→ 启动数据库 → 迁移 + 初始化 → 启动全部服务 → 健康检查
source "$(dirname "$0")/common.sh"
need_docker
[ -f .env ] || { c_info "未找到 .env，自动生成"; scripts/gen-env.sh; c_warn "请先修改 .env 中的 INSPECTOR_BASE_URL / SMTP 等必改项，再重新执行 make install"; exit 0; }
need_env
mkdir -p "$(env_get DATA_DIR ./data)" "$(env_get BACKUP_DIR ./backups)" exports
ensure_images

c_info "启动 PostgreSQL / Redis"
dc up -d postgres redis
wait_healthy postgres 120 || die "PostgreSQL 未就绪：make logs S=postgres"

c_info "数据库迁移 + 初始化（内置规则 / 巡检任务 / admin）"
dc run --rm migrate || die "迁移失败：查看上面的输出；修复后重新执行 make install（可重复执行）"

c_info "启动 API / 调度 / 通知 / Web"
dc up -d --remove-orphans
wait_healthy api 180 || die "API 未就绪：make logs S=api"
wait_healthy web 60 || c_warn "web 健康检查未通过：make logs S=web"

c_ok "安装完成：http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(env_get WEB_PORT 80)/"
pw="$(env_get INSPECTOR_INIT_ADMIN_PASSWORD)"
if [ -n "$pw" ]; then
  c_warn "admin 初始口令见 .env 的 INSPECTOR_INIT_ADMIN_PASSWORD；首次登录后立即修改，并把该行清空"
fi
echo "下一步：登录控制台 → 系统管理 → 租户接入（录入 HCS 租户并点“连通性测试”）→ make probe TENANT=<租户编码> 做接口探测"
