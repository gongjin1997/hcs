#!/usr/bin/env bash
# 首次离线部署入口；在传输目录校验后，安装到 /data/hcs-inspector。
set -euo pipefail

source_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
target=/data/hcs-inspector
check_only=0
fail() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
usage() { printf '用法：bash install.sh [--dir /data/hcs-inspector] [--check]\n'; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir) [ "$#" -ge 2 ] || fail '--dir 缺少部署目录'; target="$2"; shift 2;;
    --check) check_only=1; shift;;
    -h|--help) usage; exit 0;;
    *) usage >&2; fail "未知参数：$1";;
  esac
done
case "$target" in /*) ;; *) fail '部署目录必须是绝对路径';; esac
case "$target" in *:*|*$'\n'*|*$'\r'*) fail '部署目录不能包含冒号或换行';; esac
[ ! -L "$target" ] || fail '部署目录不能是符号链接'
target="$(realpath -m -- "$target")"
[ "$target" != / ] || fail '不能安装到根目录'

payload=(.env.example bundle.env deploy.sh install.sh Makefile
  docker-compose.yml docker-compose.offline.yml docker-compose.code.yml
  docker-compose.https.yml README.md SHA256SUMS scripts defaults code images)
verify_bundle() (
  cd -- "$1"
  local file
  for file in "${payload[@]}" images/PLATFORM images/manifest.tsv images/images.tar.gz \
    scripts/common.sh scripts/release-manager.py scripts/compose-compat.py scripts/update-code.sh; do
    [ -e "$file" ] || fail "首次部署文件缺失：$file"
  done
  [ -z "$(find scripts defaults code images -type l -print -quit)" ] || fail '部署文件不能包含符号链接'
  sha256sum -c --strict SHA256SUMS
  (cd images && sha256sum -c --strict SHA256SUMS)
  local packages=(code/*.tar.gz)
  [ "${#packages[@]}" = 1 ] && [ -f "${packages[0]}" ] || fail '首包必须包含一个完整业务代码包'
  case "$(cat images/PLATFORM)" in linux/arm64|linux/amd64) ;; *) fail '镜像架构声明无效';; esac
)

verify_bundle "$source_dir"
printf '文件校验通过；默认部署目录：%s\n' "$target"
[ "$check_only" = 0 ] || exit 0

if [ "$source_dir" != "$target" ]; then
  [ ! -e "$target" ] || {
    [ -d "$target" ] || fail "目标不是目录：$target"
    [ -z "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ] ||
      fail "目标目录已有文件，不覆盖。已有部署请执行：cd $target && bash deploy.sh"
  }
  # 在更改目录前检查 Docker、Compose 和目标架构；不拉取镜像或访问互联网。
  (source "$source_dir/scripts/common.sh"; need_docker; check_offline_platform)
  parent="$(dirname -- "$target")"
  mkdir -p -- "$parent"
  stage="$(mktemp -d "$parent/.hcs-inspector-install.XXXXXX")"
  trap 'if [ -n "${stage:-}" ]; then rm -rf -- "$stage"; fi' EXIT
  tar -C "$source_dir" -cf - -- "${payload[@]}" | tar -C "$stage" -xf -
  verify_bundle "$stage"
  mv -T -- "$stage" "$target"
  stage=''
  trap - EXIT
fi

printf '开始部署：%s\n' "$target"
cd -- "$target"
exec bash ./deploy.sh
