#!/usr/bin/env bash
# 备份：PostgreSQL 全量（pg_dump 自定义格式）+ .env（含主密钥）+ nginx / 证书配置 → backups/hcs-inspector-<时间>.tar.gz
# 建议 crontab 每天执行：0 2 * * * cd /opt/hcs-inspector && scripts/backup.sh >> backups/backup.log 2>&1
source "$(dirname "$0")/common.sh"
need_docker; need_env
dir="$(env_get BACKUP_DIR ./backups)"; keep="$(env_get BACKUP_KEEP 14)"
ts="$(date +%Y%m%d-%H%M%S)"; work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
mkdir -p "$dir"
db="$(env_get POSTGRES_DB inspector)"; user="$(env_get POSTGRES_USER inspector)"
c_info "导出数据库 $db"
dc exec -T postgres pg_dump -U "$user" -d "$db" -Fc --no-owner > "$work/db.dump" || die "pg_dump 失败（postgres 是否在运行：make ps）"
cp -p .env "$work/env"
tar -cf "$work/conf.tar" nginx certs docker-compose*.yml 2>/dev/null || true
echo "version=$(env_get HCS_INSPECTOR_VERSION) time=$ts host=$(hostname)" > "$work/MANIFEST"
umask 077
tar -czf "$dir/hcs-inspector-$ts.tar.gz" -C "$work" .
c_ok "备份完成：$dir/hcs-inspector-$ts.tar.gz（$(du -h "$dir/hcs-inspector-$ts.tar.gz" | cut -f1)，含 .env 与主密钥，请妥善保管）"
# 只保留最近 N 份
ls -1t "$dir"/hcs-inspector-*.tar.gz 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -f
