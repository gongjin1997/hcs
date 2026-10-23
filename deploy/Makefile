# HCS-Inspector 部署运维命令（在 deploy 目录执行；配置读取同目录 .env，compose 文件组合由 .env 的 COMPOSE_FILE 决定）
# 首次：make gen-env → 修改 .env 必改项 → make install；升级：make upgrade V=0.4.1；日常：make status / make backup
SHELL   := /bin/bash
COMPOSE := docker compose
S       ?=
V       ?=
F       ?=
TENANT  ?=
ONLY    ?=
LIMIT   ?= 5

.PHONY: help gen-env install upgrade rollback status ps logs restart up down reload-web backup restore migrate probe probe-list shell psql bundle config

help:            ## 显示帮助
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}'

gen-env:         ## 由 .env.example 生成 .env（随机密钥 / 口令）；已有 .env 时拒绝覆盖
	scripts/gen-env.sh

install:         ## 首次安装：环境检查 → 镜像（离线包 / 已有 / 源码构建）→ 迁移 + 初始化 → 启动 → 健康检查（可重复执行）
	scripts/install.sh

upgrade:         ## 升级：备份 → 新版本先迁移数据库 → 替换进程 → 健康检查，例：make upgrade V=0.4.1
	scripts/upgrade.sh $(V)

rollback:        ## 回退到升级前：改回旧版本号 → 用升级前备份恢复数据库 → 启动旧版本，例：make rollback V=0.4.0 F=backups/xxx.tar.gz
	scripts/rollback.sh "$(V)" "$(F)"

status:          ## 运行状态：容器健康 + 平台自检（进程心跳 / 通知积压 / 任务健康 / 平台告警 / 迁移版本）
	scripts/status.sh

ps:              ## 容器列表
	$(COMPOSE) ps

logs:            ## 查看日志（最近 200 行并跟随），例：make logs S=scheduler
	$(COMPOSE) logs -f --tail=200 $(S)

restart:         ## 重启服务（不带 S 重启全部应用进程），例：make restart S=notifier
	$(COMPOSE) restart $(or $(S),api scheduler notifier web)

up:              ## 按 .env 启动 / 对齐全部服务（修改 .env 后执行，会重建配置有变化的容器）
	$(COMPOSE) up -d --remove-orphans

down:            ## 停止并删除容器（数据目录 / 备份保留）
	$(COMPOSE) down

reload-web:      ## 修改 nginx/ 配置（白名单、证书）后：语法检查并热加载
	$(COMPOSE) exec web nginx -t && $(COMPOSE) exec web nginx -s reload

backup:          ## 备份数据库 + .env（含主密钥）+ nginx / 证书配置 → backups/（保留 BACKUP_KEEP 份）
	scripts/backup.sh

restore:         ## 从备份恢复（会覆盖当前数据库），例：make restore F=backups/hcs-inspector-20261001-020000.tar.gz [WITH_ENV=1]
	scripts/restore.sh "$(F)" $(if $(WITH_ENV),--with-env)

migrate:         ## 手动执行数据库迁移 + 幂等初始化（install / upgrade 已自动执行）
	$(COMPOSE) run --rm migrate

probe-list:      ## 列出已接入的租户（编码 / 认证方式 / 项目数）
	$(COMPOSE) exec -T api python -m app.tools.probe --list

probe:           ## HCS 接口探测（只读），报告拷到 exports/，例：make probe TENANT=tenant-a [ONLY=ecs,dcs] [LIMIT=20]
	@test -n "$(TENANT)" || { echo "用法：make probe TENANT=<租户编码> [ONLY=ecs,dcs] [LIMIT=20]（make probe-list 查看编码）"; exit 2; }
	@mkdir -p exports
	$(COMPOSE) exec -T api python -m app.tools.probe --tenant "$(TENANT)" $(if $(ONLY),--only "$(ONLY)") --limit $(LIMIT) --out "/app/data/probe-$(TENANT).json"; \
	  rc=$$?; $(COMPOSE) cp "api:/app/data/probe-$(TENANT).json" "exports/probe-$(TENANT).json" >/dev/null 2>&1 \
	  && echo "报告：exports/probe-$(TENANT).json（已脱敏，可发给开发核对字段映射）"; exit $$rc

shell:           ## 进入 api 容器（排查用）
	$(COMPOSE) exec api bash

psql:            ## 连接数据库（只读排查；修改数据前先 make backup）
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

config:          ## 打印合并后的 compose 配置（核对 COMPOSE_FILE / 端口 / 镜像版本；输出含密钥，勿外发）
	$(COMPOSE) config

bundle:          ## 构建机：打离线安装包 dist/hcs-inspector-<版本>-offline.tar.gz（加 DEMO=1 带 mailpit）
	scripts/offline-bundle.sh $(if $(DEMO),--with-demo)
