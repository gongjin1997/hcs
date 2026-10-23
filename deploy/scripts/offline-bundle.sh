#!/usr/bin/env bash
# 在有外网的构建机打离线安装包：构建镜像 → docker save → 连同部署目录打成 hcs-inspector-<版本>-offline.tar.gz
# 用法（仓库 deploy 目录）：scripts/offline-bundle.sh [--with-demo]   # --with-demo 额外带上 mailpit 镜像（演示用）
source "$(dirname "$0")/common.sh"
need_docker
[ -d ../backend ] || die "请在源码仓库的 deploy 目录执行"
# compose 解析 env_file: [.env] 时要求文件存在；构建机没有 .env 时临时用模板占位，结束后删除
tmp_env=0; if [ ! -f .env ]; then cp .env.example .env; tmp_env=1; fi
trap 'if [ "$tmp_env" = 1 ]; then rm -f .env; fi' EXIT
v="$(grep -E '^HCS_INSPECTOR_VERSION=' .env.example | cut -d= -f2)"; p="hcs-inspector"
export HCS_INSPECTOR_VERSION="$v" IMAGE_PREFIX="$p" POSTGRES_PASSWORD="bundle"
out="dist/hcs-inspector-${v}-offline"; rm -rf "$out"; mkdir -p "$out/images"
c_info "构建镜像 v$v"
docker compose --env-file /dev/null -f docker-compose.yml -f docker-compose.build.yml build
imgs=("${p}-backend:${v}" "${p}-web:${v}" "postgres:16-alpine" "redis:7-alpine")
[ "${1:-}" = "--with-demo" ] && imgs+=("axllent/mailpit:latest")
for i in "${imgs[@]:2}"; do docker image inspect "$i" >/dev/null 2>&1 || docker pull "$i"; done
for i in "${imgs[@]}"; do
  c_info "导出 $i"
  docker save "$i" | gzip > "$out/images/$(echo "$i" | tr '/:' '__').tar.gz"
done
cp -r docker-compose.yml docker-compose.https.yml docker-compose.demo.yml .env.example Makefile README.md nginx certs scripts "$out/"
[ -d ../docs ] && mkdir -p "$out/docs" && cp ../docs/*.md ../CHANGELOG.md "$out/docs/" 2>/dev/null || true
(cd "$out/images" && sha256sum ./*.tar.gz > SHA256SUMS)
tar -czf "dist/hcs-inspector-${v}-offline.tar.gz" -C dist "hcs-inspector-${v}-offline"
c_ok "离线包：deploy/dist/hcs-inspector-${v}-offline.tar.gz（拷到目标机解压后 make install）"
