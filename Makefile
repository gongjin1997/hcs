# HCS-Inspector 常用命令（在仓库根目录执行）
PY      ?= python3
VENV    := backend/.venv
BIN     := $(VENV)/bin
# 一键演示环境：生产 compose + 演示覆盖（Mock HCS / Mailpit / 演示数据）+ 源码构建；生产部署在 deploy/ 目录用 deploy/Makefile
WEB_PORT ?= 8080
COMPOSE  := WEB_PORT=$(WEB_PORT) docker compose --project-directory deploy -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml -f deploy/docker-compose.build.yml

.PHONY: help init dev-api dev-mock dev-web scheduler notifier run-once ledger-sync report migrate bootstrap status probe revision apidoc lock lint test cov check build up down logs

help:            ## 显示帮助
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

init:            ## 初始化后端虚拟环境与前端依赖（需要 Python 3.12+，可用 make init PY=python3.12 指定）
	@$(PY) -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "需要 Python 3.12+，当前 $(PY) 为 $$($(PY) --version 2>&1)；可用 make init PY=python3.12"; exit 1; }
	$(PY) -m venv $(VENV) && $(BIN)/pip install -U pip && $(BIN)/pip install -c backend/constraints.txt -e "backend[dev,postgres]"
	cp -n backend/.env.example backend/.env || true
	cd frontend && npm ci

dev-api:         ## 启动后端 API（热重载，:8000，自动建表 + 演示数据）
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

dev-mock:        ## 启动 Mock HCS 8.5.1（:9000，两个租户：账号密码 + CES / AK/SK + Prometheus；IAM、巡检与台账服务、CCE K8s API；配合 INSPECTOR_HCS_MOCK_URL 使用）
	cd backend && .venv/bin/uvicorn mock_hcs.app:app --port 9000

dev-web:         ## 启动前端开发服务器（:5173，/api 代理到 :8000）
	cd frontend && npm run dev

scheduler:       ## 启动调度进程：巡检任务 + 台账同步 + 定时报表（APScheduler + Redis 分布式锁）
	cd backend && .venv/bin/python -m app.workers.scheduler

notifier:        ## 启动通知投递进程（Outbox → SMTP）
	cd backend && .venv/bin/python -m app.workers.notifier

run-once:        ## 手动执行一次巡检（默认全部启用租户），例：make run-once task=ecs_status
	cd backend && .venv/bin/python -m app.workers.run_once $(task)

ledger-sync:     ## 手动同步一次资源台账（需 HCS 或 make dev-mock）
	cd backend && .venv/bin/python -m app.workers.run_once ledger_sync

report:          ## 手动生成上一周期报表，例：make report kind=DAILY（DAILY / WEEKLY / MONTHLY / LEDGER）
	cd backend && .venv/bin/python -m app.workers.run_once report $(or $(kind),DAILY)

migrate:         ## 数据库迁移到最新版本
	cd backend && .venv/bin/alembic upgrade head

bootstrap:       ## 生产初始化（幂等）：内置规则 / 巡检任务 / 台账同步任务；首次部署设置 INSPECTOR_INIT_ADMIN_PASSWORD 创建 admin
	cd backend && .venv/bin/python -m app.workers.bootstrap

status:          ## 平台自检（进程心跳 / 通知积压 / 任务健康 / 平台告警 / 迁移版本），例：make status args=--json
	cd backend && .venv/bin/python -m app.tools.status $(args)

probe:           ## HCS 接口探测（只读），例：make probe args="--tenant tenant-a --only ecs,dcs"；对 Mock 加 --mock-url http://127.0.0.1:9000
	cd backend && .venv/bin/python -m app.tools.probe $(args)

apidoc:          ## 根据代码重新生成接口清单 docs/接口清单.md（方法 / 路径 / 说明 / 所需权限点）
	cd backend && .venv/bin/python -m app.tools.apidoc > ../docs/接口清单.md

lock:            ## 升级依赖后重新生成 backend/constraints.txt（镜像构建 / CI 按此锁定版本）
	cd backend && { sed -n '1,2p' constraints.txt; .venv/bin/pip freeze --exclude-editable | grep -viE '^hcs[-_]inspector'; } > constraints.txt.new && mv constraints.txt.new constraints.txt

revision:        ## 生成迁移脚本，例：make revision m="add silence table"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

lint:            ## 代码检查：ruff + mypy + vue-tsc
	cd backend && .venv/bin/ruff check . && .venv/bin/mypy app mock_hcs
	cd frontend && npm run typecheck

test:            ## 后端单元 / 接口测试
	cd backend && .venv/bin/pytest

cov:             ## 后端测试 + 覆盖率（终端报告 + backend/htmlcov/；门槛见 pyproject 的 fail_under）
	cd backend && .venv/bin/pytest --cov=app --cov-report=term --cov-report=html

check: lint test ## 提交前检查（CI 同款）
	cd frontend && npm run build

build:           ## 从源码构建镜像（backend / web）
	@test -f deploy/.env || deploy/scripts/gen-env.sh
	$(COMPOSE) build

up:              ## 一键演示环境（Docker）：PostgreSQL / Redis / Mock HCS / Mailpit / API / 调度 / 通知 / Web；控制台 :8080，邮件 :8025
	@test -f deploy/.env || deploy/scripts/gen-env.sh
	$(COMPOSE) up -d --build

down:            ## 停止并删除演示环境容器（数据保留在 deploy/data）
	$(COMPOSE) down

logs:            ## 查看演示环境日志，例：make logs s=api
	$(COMPOSE) logs -f --tail=200 $(s)
