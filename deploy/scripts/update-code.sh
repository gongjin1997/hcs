#!/usr/bin/env bash
# 只使用现有运行时应用小代码包，不 docker load/pull/build，不运行宿主机 pip/npm。
source "$(dirname "$0")/common.sh"
need_docker
need_env
[ "$(env_get INSPECTOR_ENV)" = prod ] || die "代码更新入口仅用于正式环境"
[ "$(env_get INSPECTOR_DEMO_SEED false)" = false ] || die "正式环境不能启用演示数据"
package="${1:-}"
[ "$#" -eq 1 ] && [ -f "$package" ] || die "用法：bash deploy.sh update /path/hcs-inspector-code-版本.tar.gz"
images_present || die "缺少首次安装的本地运行时镜像；代码更新不会拉取或导入镜像"
if [ -f bundle.env ]; then
  offline_images_present || die "本地镜像与首次安装清单不一致；代码更新不会替换镜像"
fi
c_info "校验代码及离线依赖（复用本地镜像）"
candidate=$(prepare_code_archive "$package")
previous=$(active_code_dir)
if [ "$candidate" = "$previous" ]; then
  c_ok "已经是该代码版本，原配置保留"
  exit 0
fi

if [ -n "$(dc ps -q postgres 2>/dev/null)" ] && [ -z "$(env_get INSPECTOR_DATABASE_URL)" ]; then
  c_info "更新前备份数据库和配置"
  bash scripts/backup.sh || die "备份失败，当前服务和代码保持不变"
fi

c_info "执行新代码迁移，保留原租户与开关"
dc stop scheduler notifier
if ! HCS_CODE_DIR="$candidate" dc run --rm -T --no-deps migrate; then
  dc up -d --no-deps scheduler notifier || true
  die "迁移失败，未切换活动代码；原 API 保留，请根据日志及更新前备份处理数据库"
fi
activate_code "$candidate"
unset HCS_CODE_DIR
start_application
c_ok "代码更新完成：${candidate##*/}（运行时镜像和原 .env 未变）"
