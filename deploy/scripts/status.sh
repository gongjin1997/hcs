#!/usr/bin/env bash
# 状态：容器 / 健康检查 + 平台自检（进程心跳、通知积压、任务健康、平台告警、迁移版本）
source "$(dirname "$0")/common.sh"
need_docker
dc ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
echo
if [ -n "$(dc ps -q --status running api 2>/dev/null)" ]; then
  dc exec -T api python -m app.tools.status "$@"
else
  die "api 未运行：make up 或 make logs S=api"
fi
