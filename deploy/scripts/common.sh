#!/usr/bin/env bash
# 部署脚本公共函数（被其它脚本 source）。目标机仅使用独立 docker-compose 命令。
set -euo pipefail
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEPLOY_DIR"

c_info() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
c_ok()   { printf '\033[32m[ OK ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*" >&2; }
die()    { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

prepare_configs() {
  # 默认配置单独存放；升级解压不会覆盖局点已经维护的 nginx / CA 文件。
  local file target
  if [ -d defaults ]; then
    while IFS= read -r -d '' file; do
      target="${file#defaults/}"
      [ ! -e "$target" ] || continue
      mkdir -p "$(dirname "$target")"
      cp "$file" "$target"
    done < <(find defaults -type f -print0)
  fi
  mkdir -p certs
}

select_compose() {
  [ -z "${COMPOSE_MAJOR:-}" ] || return 0
  local version
  command -v docker-compose >/dev/null 2>&1 || die "缺少独立 docker-compose 命令"
  version=$(docker-compose version --short 2>/dev/null) || die "无法运行独立 docker-compose 命令"
  COMPOSE_CMD=(docker-compose)
  [[ "$version" =~ ([0-9]+)\.([0-9]+) ]] || die "无法识别 Compose 版本"
  COMPOSE_MAJOR="${BASH_REMATCH[1]}"
  if [ "$COMPOSE_MAJOR" = 1 ] && [ "${BASH_REMATCH[2]}" -lt 29 ]; then
    die "docker-compose 至少需要 1.29"
  fi
}

runtime_image() { printf '%s-backend:%s' "$(env_get IMAGE_PREFIX hcs-inspector)" "$(env_get HCS_INSPECTOR_VERSION 0.4.0)"; }

compose_config_images() {
  # 独立 Compose 1.29 没有 config --images；只提取规范化 YAML 的服务 image 字段。
  # 完整配置（含 .env 值）仅经过本地管道，不输出或写入日志、缓存。
  if [ "$COMPOSE_MAJOR" = 1 ]; then
    "${COMPOSE_CMD[@]}" "$@" config | awk '
      /^services:$/ { services = 1; next }
      /^[^ ]/ { services = 0 }
      services && /^    image: / {
        image = $0
        sub(/^    image:[[:space:]]*/, "", image)
        gsub(/^["\047]|["\047]$/, "", image)
        if (image !~ /^[A-Za-z0-9][A-Za-z0-9._:@\/+\-]*$/) exit 1
        print image
        count++
      }
      END { if (!count) exit 1 }
    '
  else
    "${COMPOSE_CMD[@]}" "$@" config --images
  fi
}

active_code_dir() {
  local value=""
  if [ -f .code.env ]; then
    value=$(grep '^HCS_CODE_DIR=' .code.env | cut -d= -f2-)
    case "$value" in "$DEPLOY_DIR"/releases/*) ;; *) die "代码目录声明无效";; esac
  fi
  printf '%s' "$value"
}

prepare_code_archive() {
  local package="$1" release image
  [[ "$package" = /* ]] || package="$DEPLOY_DIR/$package"
  [ -f "$package" ] || die "找不到代码包：$package"
  mkdir -p releases
  image=$(runtime_image)
  docker image inspect "$image" >/dev/null 2>&1 || die "缺少已有后端运行时镜像，请先安装首次离线包"
  release=$(docker run --rm --pull=never --platform "$(env_get OFFLINE_PLATFORM linux/arm64)" --network none --user "$(id -u):$(id -g)" \
    -v "$DEPLOY_DIR/scripts/release-manager.py:/manager.py:ro" -v "$package:/package.tar.gz:ro" \
    -v "$DEPLOY_DIR/releases:/releases" --entrypoint python "$image" \
    /manager.py prepare /package.tar.gz /releases) || die "代码包校验或离线依赖准备失败，当前版本不变"
  [[ "$release" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "代码版本标识无效"
  printf '%s/releases/%s' "$DEPLOY_DIR" "$release"
}

activate_code() {
  local directory="$1" temporary
  case "$directory" in "$DEPLOY_DIR"/releases/*) ;; *) die "代码目录无效";; esac
  temporary=$(mktemp .code-env.XXXXXX)
  printf 'HCS_CODE_DIR=%s\n' "$directory" > "$temporary"
  chmod 600 "$temporary"
  mv "$temporary" .code.env
}

ensure_code() {
  local directory package="" item
  directory=$(active_code_dir)
  if [ -z "$directory" ]; then
    for item in code/hcs-inspector-code-*.tar.gz; do
      [ -f "$item" ] || continue
      [ -z "$package" ] || die "首次安装目录有多个代码包，请只保留一个"
      package="$item"
    done
    [ -n "$package" ] || return 0  # 兼容旧的镜像内置代码部署。
    c_info "准备正式环境代码"
    directory=$(prepare_code_archive "$package")
    activate_code "$directory"
  fi
  [ -d "$directory/backend/app" ] && [ -f "$directory/web/index.html" ] || die "活动代码目录不完整"
}

start_application() {
  # 删除应用容器但保留数据卷，兼容 Compose v1 与新 Docker 的 ContainerConfig 差异。
  dc stop api scheduler notifier web
  dc rm -f api scheduler notifier web
  dc up -d --no-deps api
  wait_healthy api 180 || die "API 未就绪：bash deploy.sh logs api"
  dc up -d --no-deps scheduler notifier web
  wait_healthy web 90 || die "Web 未就绪：bash deploy.sh logs web"
  wait_healthy scheduler 180 || die "调度进程未就绪：bash deploy.sh logs scheduler"
  wait_healthy notifier 180 || die "通知进程未就绪：bash deploy.sh logs notifier"
}

legacy_compose_file() {
  local file="$1" mode=local hash image
  [ -z "${INSPECTOR_DATABASE_URL:-$(env_get INSPECTOR_DATABASE_URL)}" ] || mode=external
  hash=$({ sha256sum "$file" scripts/compose-compat.py; printf '%s\n' "$mode"; } | sha256sum | awk '{print $1}')
  mkdir -p .compose-legacy
  if [ ! -s ".compose-legacy/$hash.yml" ]; then
    image=$(runtime_image)
    docker image inspect "$image" >/dev/null 2>&1 || die "请先执行 bash deploy.sh 导入运行时镜像"
    local input="$file"
    [[ "$input" = /* ]] || input="$DEPLOY_DIR/$input"
    docker run --rm --pull=never --platform "$(env_get OFFLINE_PLATFORM linux/arm64)" --network none --user "$(id -u):$(id -g)" \
      -v "$DEPLOY_DIR/scripts/compose-compat.py:/manager.py:ro" -v "$input:/input.yml:ro" \
      -v "$DEPLOY_DIR/.compose-legacy:/out" \
      --entrypoint python "$image" /manager.py /input.yml "/out/$hash.yml" "$mode"
  fi
  printf '%s' ".compose-legacy/$hash.yml"
}

_dc() (
  umask 077
  # 只合并镜像和代码目录；不 source 原 .env，不要求 v1 不支持的多个 --env-file。
  local key value file files="${COMPOSE_FILE:-$(env_get COMPOSE_FILE docker-compose.yml)}"
  local -a args=() parts=()
  if [ -f bundle.env ]; then
    while IFS='=' read -r key value; do
      case "$key" in HCS_INSPECTOR_VERSION|IMAGE_PREFIX|POSTGRES_IMAGE|REDIS_IMAGE|OFFLINE_PLATFORM) export "$key=$value";; esac
    done < bundle.env
  fi
  if [ -z "${HCS_CODE_DIR:-}" ] && [ -f .code.env ]; then
    value=$(grep '^HCS_CODE_DIR=' .code.env | cut -d= -f2-)
    case "$value" in "$DEPLOY_DIR"/releases/*) export HCS_CODE_DIR="$value";; *) die "代码目录声明无效";; esac
  fi
  IFS=: read -r -a parts <<< "$files"
  for file in "${parts[@]}"; do
    [ "$file" = docker-compose.offline.yml ] && continue
    if [ "$COMPOSE_MAJOR" = 1 ]; then file=$(legacy_compose_file "$file"); fi
    args+=(-f "$file")
  done
  if [ -f bundle.env ]; then
    file=docker-compose.offline.yml
    if [ "$COMPOSE_MAJOR" = 1 ]; then file=$(legacy_compose_file "$file"); fi
    args+=(-f "$file")
  fi
  if [ -n "${HCS_CODE_DIR:-}" ]; then
    file=docker-compose.code.yml
    [ ! -f "$HCS_CODE_DIR/$file" ] || file="$HCS_CODE_DIR/$file"
    if [ "$COMPOSE_MAJOR" = 1 ]; then file=$(legacy_compose_file "$file"); fi
    args+=(-f "$file")
  fi
  # v1 没有 pull_policy。已有镜像逐一核验后才允许创建容器，禁止隐式拉取/构建。
  if [ "$COMPOSE_MAJOR" = 1 ] && { [ "${1:-}" = up ] || [ "${1:-}" = run ]; }; then
    local image images
    images=$(compose_config_images --project-directory "$DEPLOY_DIR" "${args[@]}") || exit 1
    while IFS= read -r image; do docker image inspect "$image" >/dev/null 2>&1 || die "缺少本地镜像 $image；离线部署不会自动拉取"; done <<< "$images"
  fi
  if [ "$COMPOSE_MAJOR" = 1 ] && [ "$#" = 2 ] && [ "$1" = config ] && [ "$2" = --images ]; then
    compose_config_images --project-directory "$DEPLOY_DIR" -p "${COMPOSE_PROJECT_NAME:-$(env_get COMPOSE_PROJECT_NAME hcs-inspector)}" "${args[@]}"
  else
    "${COMPOSE_CMD[@]}" --project-directory "$DEPLOY_DIR" -p "${COMPOSE_PROJECT_NAME:-$(env_get COMPOSE_PROJECT_NAME hcs-inspector)}" "${args[@]}" "$@"
  fi
)

dc() {
  select_compose
  _dc "$@"
}

env_get() {   # 读取 .env 中的变量（不 source，避免执行任意内容）
  local key="$1" def="${2:-}" v file=.env
  case "$key" in
    HCS_INSPECTOR_VERSION|IMAGE_PREFIX|POSTGRES_IMAGE|REDIS_IMAGE|OFFLINE_PLATFORM)
      [ ! -f bundle.env ] || file=bundle.env;;
  esac
  v=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  v="${v%\"}"; v="${v#\"}"
  v="${v%\'}"; v="${v#\'}"
  printf '%s' "${v:-$def}"
}

need_docker() {
  command -v docker >/dev/null || die "未安装 docker（要求 Docker 20.10+）"
  select_compose
  docker info >/dev/null 2>&1 || die "无法连接 docker 守护进程（当前用户是否在 docker 组 / 是否需要 sudo）"
}

need_env() {
  [ -f .env ] || die "缺少 .env：先执行 scripts/gen-env.sh 生成，再按局点修改"
  local bad
  bad=$(grep -nE '__GEN_[A-Z0-9_]+__' .env | cut -d= -f1 || true)
  [ -z "$bad" ] || die ".env 中还有未生成的占位符：$bad"
  if [ "$(env_get INSPECTOR_ENV prod)" = "prod" ]; then
    [ -n "$(env_get INSPECTOR_MASTER_KEY)" ] || die ".env 未配置 INSPECTOR_MASTER_KEY"
    case "$(env_get INSPECTOR_BASE_URL)" in *example.com*) c_warn "INSPECTOR_BASE_URL 仍是示例地址（邮件链接会打不开）";; esac
    case "$(env_get INSPECTOR_SMTP_HOST)" in smtp.example.com) c_warn "INSPECTOR_SMTP_HOST 仍是示例值（通知邮件发不出去）";; esac
  fi
}

images_present() {   # 本机是否已有当前版本镜像
  local image images
  images=$(dc config --images) || return 1
  [ -n "$images" ] || return 1
  while IFS= read -r image; do
    docker image inspect "$image" >/dev/null 2>&1 || return 1
  done <<< "$images"
}

check_offline_platform() {
  local expected actual
  expected=$(cat images/PLATFORM)
  case "$expected" in linux/arm64|linux/amd64) ;; *) die "离线包架构声明无效";; esac
  actual=$(docker info --format '{{.OSType}}/{{.Architecture}}')
  case "$actual" in linux/aarch64) actual=linux/arm64;; linux/x86_64) actual=linux/amd64;; esac
  if [ "$actual" != "$expected" ]; then
    [ "${HCS_ALLOW_EMULATION:-0}" = 1 ] || die "离线包要求 $expected，当前 Docker 为 $actual；请使用对应架构的主机/镜像包"
    c_warn "正在使用架构模拟进行部署验收（$actual → $expected）"
  fi
}

offline_images_present() {
  local image expected platform actual required found actual_id
  while IFS=$'\t' read -r image expected platform; do
    [ -n "$image" ] && [ -n "$expected" ] && [ "$platform" = "$(cat images/PLATFORM)" ] || return 1
    actual=$(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image" 2>/dev/null) || return 1
    actual_id="${actual%% *}"
    # 不同 Docker 镜像存储后端可能返回 manifest ID 或 config ID；两者均由本包实际内容校验生成。
    [ "${actual#* }" = "$platform" ] && [[ ",$expected," == *",$actual_id,"* ]] || return 1
  done < images/manifest.tsv
  required=$(dc config --images) || return 1
  [ -n "$required" ] || return 1
  while IFS= read -r image; do
    found=$(awk -F '\t' -v image="$image" '$1 == image {print $1}' images/manifest.tsv)
    [ "$found" = "$image" ] || return 1
  done <<< "$required"
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
  if [ -f bundle.env ]; then
    [ -s images/manifest.tsv ] && [ -s images/SHA256SUMS ] && [ -s images/PLATFORM ] || die "离线包缺少镜像清单或校验文件"
    check_offline_platform
    (cd images && sha256sum -c --quiet SHA256SUMS) || die "离线包校验失败，请重新拷贝完整安装包"
    if ! offline_images_present; then
      load_images || die "导入离线镜像失败"
    fi
    offline_images_present || die "镜像名称、ID 或架构与离线包不一致；请勿覆盖 bundle.env 中的镜像设置"
    return 0
  fi
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
