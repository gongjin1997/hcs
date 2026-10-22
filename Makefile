# HCS-Inspector 常用命令（在仓库根目录执行）
PY      ?= python3
VENV    := backend/.venv
BIN     := $(VENV)/bin
COMPOSE := docker compose -f deploy/docker-compose.yml

.PHONY: help init dev-api dev-mock dev-web scheduler notifier run-once ledger-sync report migrate bootstrap revision lint test check build up down logs

help:            ## 显示帮助
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

init:            ## 初始化后端虚拟环境与前端依赖
	$(PY) -m venv $(VENV) && $(BIN)/pip install -U pip && $(BIN)/pip install -e "backend[dev,postgres]"
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

revision:        ## 生成迁移脚本，例：make revision m="add silence table"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

lint:            ## 代码检查：ruff + mypy + vue-tsc
	cd backend && .venv/bin/ruff check . && .venv/bin/mypy app mock_hcs
	cd frontend && npm run typecheck

test:            ## 后端单元 / 接口测试
	cd backend && .venv/bin/pytest

check: lint test ## 提交前检查（CI 同款）
	cd frontend && npm run build

build:           ## 构建镜像
	$(COMPOSE) build

up:              ## 一键启动完整环境（PostgreSQL / Redis / Mailpit / Mock HCS / API / Worker / Web）
	$(COMPOSE) up -d --build

down:            ## 停止并清理容器
	$(COMPOSE) down

logs:            ## 查看日志，例：make logs s=api
	$(COMPOSE) logs -f $(s)
