#!/usr/bin/env bash
# 由 .env.example 生成 .env：随机生成数据库口令、JWT / 邮件链接密钥、凭据加密主密钥、admin 初始口令、/metrics 令牌
# 用法：scripts/gen-env.sh            # 已有 .env 时拒绝覆盖
#       scripts/gen-env.sh --force    # 先备份为 .env.bak.<时间> 再重建（注意：主密钥变化后已保存的租户凭据无法解密！）
source "$(dirname "$0")/common.sh"

if [ -f .env ]; then
  [ "${1:-}" = "--force" ] || die ".env 已存在；确需重建请加 --force（会先备份，且会生成新的主密钥）"
  cp -p .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  c_warn "已备份旧 .env；如已有租户数据，请把旧的 INSPECTOR_MASTER_KEY 拷回新 .env"
fi

rand_hex() { od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'; }
rand_b64() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }
rand_pwd() { rand_hex "$((($1 + 1) / 2))"; }

umask 077
tmp=$(mktemp .env.new.XXXXXX)
trap 'rm -f "$tmp"' EXIT
sed -e "s|__GEN_DB_PASSWORD__|$(rand_pwd 24)|" \
    -e "s|__GEN_HEX_32__|$(rand_hex 32)|" \
    -e "s|__GEN_HEX_24__|$(rand_hex 24)|" \
    -e "s|__GEN_HEX_16__|$(rand_hex 16)|" \
    -e "s|__GEN_MASTER_KEY__|$(rand_b64)|" \
    -e "s|__GEN_ADMIN_PASSWORD__|Hi$(rand_pwd 14)|" \
    .env.example > "$tmp"
# 新装默认用当前主机地址；已有 .env 从不自动修改。
host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
base_url="${INSPECTOR_BASE_URL:-http://${host_ip:-127.0.0.1}:${WEB_PORT:-80}}"
case "$base_url" in http://*|https://*) ;; *) die "INSPECTOR_BASE_URL 必须是 http(s) 地址";; esac
[[ "$base_url" != *$'\n'* && "$base_url" != *$'\r'* && "$base_url" != *'|'* && "$base_url" != *'&'* && "$base_url" != *'\'* ]] || die "访问地址格式无效"
port="${WEB_PORT:-80}"
[[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1 && 10#$port <= 65535)) || die "WEB_PORT 必须为 1–65535"
sed -i -e "s|^INSPECTOR_BASE_URL=.*|INSPECTOR_BASE_URL=$base_url|" -e "s|^WEB_PORT=.*|WEB_PORT=$port|" "$tmp"
chmod 600 "$tmp"
mv "$tmp" .env
c_ok "已生成正式环境 .env（权限 600），初始管理员口令保存在其中"
c_warn "请把 .env 中的 INSPECTOR_MASTER_KEY 离线备份（丢失后已保存的租户凭据无法解密）"
