#!/usr/bin/env bash
# 状态：容器 / 健康检查 + 平台自检（进程心跳、通知积压、任务健康、平台告警、迁移版本）
source "$(dirname "$0")/common.sh"
need_docker
if [ "$COMPOSE_MAJOR" = 1 ]; then dc ps; else dc ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'; fi
echo
cid=$(dc ps -q api 2>/dev/null || true)
if [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = true ]; then
  dc exec -T api python -m app.tools.status "$@"
else
  die "API 未运行：bash deploy.sh 或 bash deploy.sh logs api"
fi
