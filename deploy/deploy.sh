#!/usr/bin/env bash
# 正式环境一键部署：保留已有 .env / 数据；新装时自动生成生产配置。
source "$(dirname "$0")/scripts/common.sh"
need_docker
case "${1:-}" in
  status) exec bash scripts/status.sh;;
  logs) dc logs -f --tail=100 "${2:-api}"; exit 0;;
  stop) dc stop; exit 0;;
  update) shift; exec bash scripts/update-code.sh "$@";;
  "") ;;
  *) die "用法：bash deploy.sh [update 代码包.tar.gz|status|logs [服务名]|stop]";;
esac
if [ -f images/PLATFORM ]; then
  check_offline_platform
fi
if [ ! -f .env ]; then
  [ ! -f data/postgres/PG_VERSION ] || die "发现已有数据库但缺少 .env；请先恢复原 .env（尤其是数据库口令和主密钥），不能重新生成"
  scripts/gen-env.sh
fi
need_env
[ "$(env_get INSPECTOR_ENV)" = prod ] || die "本入口仅部署正式环境：INSPECTOR_ENV 必须为 prod"
[ "$(env_get INSPECTOR_DEMO_SEED false)" = false ] || die "正式环境必须设置 INSPECTOR_DEMO_SEED=false"
[ -z "$(env_get INSPECTOR_HCS_MOCK_URL)" ] || die "正式环境不能配置 INSPECTOR_HCS_MOCK_URL"
case "${COMPOSE_FILE:-$(env_get COMPOSE_FILE docker-compose.yml)}" in *demo*) die "正式环境不能启用 docker-compose.demo.yml";; esac
exec bash scripts/install.sh
