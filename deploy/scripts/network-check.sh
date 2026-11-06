#!/usr/bin/env bash
# 已部署环境只读网络诊断；Python 在已有 API 容器中运行，宿主只需 Docker / docker-compose。
set -euo pipefail
script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
deployment=/data/hcs-inspector
args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --deploy-dir) [ "$#" -ge 2 ] || { echo '--deploy-dir 缺少值' >&2; exit 2; }; deployment="$2"; shift 2;;
    -h|--help) printf '用法：bash network-check.sh --tenant 租户编号 [--services all] [--project-ref 项目编号] [--deploy-dir /data/hcs-inspector]\n也可使用 --url https://IAM地址；不发送账号、密码或 AK/SK，不修改配置。\n'; exit 0;;
    *) args+=("$1"); shift;;
  esac
done
[ -f "$deployment/scripts/common.sh" ] || { echo '找不到部署目录，请用 --deploy-dir 指定' >&2; exit 2; }
[ -f "$script_dir/network-check.py" ] || { echo '缺少同目录 network-check.py' >&2; exit 2; }
source "$deployment/scripts/common.sh"
need_docker
dc exec -T api python - "${args[@]}" < "$script_dir/network-check.py"
