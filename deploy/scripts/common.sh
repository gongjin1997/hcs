#!/usr/bin/env bash
# 部署脚本公共函数（被其它脚本 source）。所有脚本都在 deploy 目录下执行 docker compose，自动读取 .env
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

c_info() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
c_ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

dc() { docker compose "$@"; }

env_get() {   # 读取 .env 中的变量（不 source，避免执行任意内容）
  local key="$1" def="${2:-}" v
  v=$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
  v="${v%\"}"; v="${v#\"}"
  printf '%s' "${v:-$def}"
}

need_docker() {
  command -v docker >/dev/null || die "未安装 docker（要求 Docker 20.10+ 与 docker compose v2 插件）"
  docker compose version >/dev/null 2>&1 || die "缺少 docker compose v2 插件（docker compose version 无输出）"
  docker info >/dev/null 2>&1 || die "无法连接 docker 守护进程（当前用户是否在 docker 组 / 是否需要 sudo）"
}

need_env() {
  [ -f .env ] || die "缺少 .env：先执行 scripts/gen-env.sh 生成，再按局点修改"
  local bad
  bad=$(grep -nE '__GEN_[A-Z0-9_]+__' .env || true)
  [ -z "$bad" ] || die ".env 中还有未生成的占位符：$bad"
  if [ "$(env_get INSPECTOR_ENV prod)" = "prod" ]; then
    [ -n "$(env_get INSPECTOR_MASTER_KEY)" ] || die ".env 未配置 INSPECTOR_MASTER_KEY"
    case "$(env_get INSPECTOR_BASE_URL)" in *example.com*) c_warn "INSPECTOR_BASE_URL 仍是示例地址（邮件链接会打不开）";; esac
    case "$(env_get INSPECTOR_SMTP_HOST)" in smtp.example.com) c_warn "INSPECTOR_SMTP_HOST 仍是示例值（通知邮件发不出去）";; esac
  fi
}

images_present() {   # 本机是否已有当前版本镜像
  local v p
  v="$(env_get HCS_INSPECTOR_VERSION 0.4.0)"; p="$(env_get IMAGE_PREFIX hcs-inspector)"
  docker image inspect "${p}-backend:${v}" >/dev/null 2>&1 && docker image inspect "${p}-web:${v}" >/dev/null 2>&1
}

load_images() {      # 离线包：images/*.tar.gz → 校验 SHA256SUMS → docker load
  local f n=0
  if [ -f images/SHA256SUMS ]; then
    (cd images && sha256sum -c --quiet SHA256SUMS) || die "离线包镜像校验失败（文件不完整或被修改），请重新拷贝 images/ 目录"
  fi
  for f in images/*.tar.gz images/*.tar; do
    [ -e "$f" ] || continue
    c_info "导入镜像 $f"
    if [[ "$f" == *.gz ]]; then gunzip -c "$f" | docker load; else docker load -i "$f"; fi
    n=$((n + 1))
  done
  [ "$n" -gt 0 ]
}

ensure_images() {
  if images_present; then return 0; fi
  if load_images && images_present; then return 0; fi
  if [ -d ../backend ] && [ -d ../frontend ]; then
    c_info "本机没有 v$(env_get HCS_INSPECTOR_VERSION) 镜像，从源码构建（需要能访问 PyPI / npm 源）"
    dc -f docker-compose.yml -f docker-compose.build.yml build
    return 0
  fi
  die "找不到 v$(env_get HCS_INSPECTOR_VERSION) 镜像：把离线包 images/ 目录放到部署目录，或在构建机执行 scripts/offline-bundle.sh"
}

wait_healthy() {     # wait_healthy <服务> [超时秒]
  local svc="$1" timeout="${2:-180}" i cid st
  for ((i = 0; i < timeout; i += 3)); do
    cid=$(dc ps -q "$svc" 2>/dev/null || true)
    if [ -n "$cid" ]; then
      st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)
      [ "$st" = "healthy" ] && return 0
    fi
    sleep 3
  done
  return 1
}
