#!/usr/bin/env bash
# 构建正式环境离线包；不读取真实 .env，不打包数据、凭据、证书或演示服务。
# 用法：bash deploy/scripts/offline-bundle.sh [--platform linux/arm64|linux/amd64]
source "$(dirname "$0")/common.sh"
platform="${PLATFORM:-linux/arm64}"
reuse=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --platform) [ "$#" -ge 2 ] || die "--platform 缺少值"; platform="$2"; shift 2;;
    --reuse-images) reuse=1; shift;;
    *) die "用法：offline-bundle.sh [--platform linux/arm64|linux/amd64] [--reuse-images]（仅正式环境）";;
  esac
done
case "$platform" in linux/arm64|linux/amd64) ;; *) die "不支持的目标架构：$platform";; esac
need_docker
command -v python3 >/dev/null || die "构建机需要 Python 3 读取镜像清单（目标机不需要）"
docker buildx version >/dev/null 2>&1 || die "构建机缺少 docker buildx；目标机不需要 buildx"
[ -f ../backend/Dockerfile ] && [ -f ../frontend/Dockerfile ] || die "请从源码仓库执行本脚本"
arch="${platform#linux/}"
v="$(sed -n 's/^HCS_INSPECTOR_VERSION=//p' .env.example)"
[[ "$v" =~ ^[0-9A-Za-z][0-9A-Za-z._-]*$ ]] || die "版本号无效"
p="hcs-inspector-${arch}"
name="hcs-inspector-${v}-${arch}-offline"
mkdir -p dist
stage=$(mktemp -d "dist/.${name}.XXXXXX")
out="$stage/$name"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$out/images" "$out/code" "$out/defaults/nginx/snippets" "$out/defaults/nginx/certs" "$out/defaults/certs" "$out/scripts"

if [ "$reuse" = 0 ]; then
c_info "构建 $platform 正式环境运行时镜像 v$v"
docker buildx build --platform "$platform" --load --provenance=false --sbom=false \
  --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}" \
  -t "${p}-backend:${v}" ../backend
docker buildx build --platform "$platform" --load --provenance=false --sbom=false \
  --build-arg "NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}" \
  -t "${p}-web:${v}" ../frontend
fi
images=("${p}-backend:${v}" "${p}-web:${v}")
for base in postgres:16-alpine redis:7-alpine; do
  if [ "$reuse" = 0 ]; then
  # 只写本项目专用标签，不覆盖构建机已有的 amd64 postgres / redis 标签。
  docker buildx build --platform "$platform" --load --provenance=false --sbom=false -t "${p}-${base}" - <<EOF
FROM $base
EOF
  fi
  images+=("${p}-${base}")
done
printf '%s\n' "$platform" > "$out/images/PLATFORM"
for image in "${images[@]}"; do
  actual=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")
  [ "$actual" = "$platform" ] || die "$image 实际架构为 $actual，不能打入 $platform 离线包"
  printf '%s\t%s\t%s\n' "$image" "$(docker image inspect --format '{{.Id}}' "$image")" "$actual" >> "$out/images/manifest.tsv"
done
c_info "导出四个运行镜像"
docker save "${images[@]}" | gzip -1 > "$out/images/images.tar.gz"
python3 scripts/image-manifest.py "$out/images"
c_info "构建独立业务代码（目标机无需 Node / Python）"
if [ ! -d ../frontend/node_modules ]; then (cd ../frontend && npm ci --no-audit --no-fund); fi
(cd ../frontend && npm run build) >&2
code_output=$(python3 ../scripts/code_bundle.py --skip-web-build)
code_package="${code_output%%$'\n'*}"
cp "$code_package" "$code_package.sha256" "$out/code/"
cp docker-compose.yml docker-compose.offline.yml docker-compose.code.yml docker-compose.https.yml .env.example deploy.sh install.sh Makefile "$out/"
cp scripts/{common,gen-env,install,status,backup,restore,upgrade,rollback,update-code,network-check}.sh "$out/scripts/"
cp scripts/{release-manager,compose-compat,network-check}.py "$out/scripts/"
cp nginx/{http,https}.conf "$out/defaults/nginx/"
cp nginx/snippets/{allowlist,metrics-allow,security-headers,hcs-app,logging}.conf "$out/defaults/nginx/snippets/"
cp nginx/certs/README.md "$out/defaults/nginx/certs/"
cp certs/README.md "$out/defaults/certs/"
cp ../docs/ARM64离线部署.md "$out/README.md"
cat > "$out/bundle.env" <<EOF
# 由打包脚本生成；只有镜像设置，原 .env 的正式配置与密钥保留。
HCS_INSPECTOR_VERSION=$v
IMAGE_PREFIX=$p
POSTGRES_IMAGE=${p}-postgres:16-alpine
REDIS_IMAGE=${p}-redis:7-alpine
OFFLINE_PLATFORM=$platform
EOF
chmod +x "$out/deploy.sh" "$out/install.sh" "$out/scripts/"*.sh
(cd "$out/images" && sha256sum images.tar.gz manifest.tsv PLATFORM > SHA256SUMS)
(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS)
tar -czf "dist/${name}.tar.gz.tmp" -C "$stage" "$name"
mv "dist/${name}.tar.gz.tmp" "dist/${name}.tar.gz"
(cd dist && sha256sum "${name}.tar.gz" > "${name}.tar.gz.sha256")
c_ok "离线包：$DEPLOY_DIR/dist/${name}.tar.gz"
c_ok "目标机解压后执行：bash install.sh（默认 /data/hcs-inspector；docker + docker-compose；后续只需上传代码包）"
