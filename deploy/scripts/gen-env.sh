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

rand_hex() { openssl rand -hex "$1" 2>/dev/null || python3 -c 'import secrets,sys;print(secrets.token_hex(int(sys.argv[1])))' "$1"; }
rand_b64() { openssl rand -base64 32 2>/dev/null || python3 -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())'; }
rand_pwd() { local p=""; while [ ${#p} -lt "$1" ]; do p="$p$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1")"; done; printf '%s' "${p:0:$1}"; }

umask 077
sed -e "s|__GEN_DB_PASSWORD__|$(rand_pwd 24)|" \
    -e "s|__GEN_HEX_32__|$(rand_hex 32)|" \
    -e "s|__GEN_HEX_24__|$(rand_hex 24)|" \
    -e "s|__GEN_HEX_16__|$(rand_hex 16)|" \
    -e "s|__GEN_MASTER_KEY__|$(rand_b64)|" \
    -e "s|__GEN_ADMIN_PASSWORD__|Hi$(rand_pwd 14)|" \
    .env.example > .env
chmod 600 .env
c_ok "已生成 .env（权限 600）。下一步：修改 INSPECTOR_BASE_URL / SMTP 等「必改」项，然后 make install"
c_warn "请把 .env 中的 INSPECTOR_MASTER_KEY 离线备份（丢失后已保存的租户凭据无法解密）"
