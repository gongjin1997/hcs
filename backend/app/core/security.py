import base64
import hashlib
import hmac
import os
import time

import jwt

from .config import get_settings

ALGO = "HS256"
ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, iters, salt_b64, hash_b64 = encoded.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def create_token(sub: str, uid: int, remember: bool = False) -> str:
    """登录令牌只携带身份（用户名 + ID），是否管理员、是否停用在每次请求时查库，改了立即生效。"""
    s = get_settings()
    now = int(time.time())
    ttl = s.jwt_remember_days * 86400 if remember else s.jwt_expire_minutes * 60
    payload = {"sub": sub, "uid": uid, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> dict:
    payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGO])
    if payload.get("typ"):                       # SSE 票据等专用令牌不能当作登录令牌使用
        raise jwt.InvalidTokenError("not an access token")
    return payload


SSE_TTL = 60
_used_tickets: dict[str, float] = {}


def create_sse_ticket(sub: str, uid: int) -> str:
    """SSE 一次性票据：EventSource 不能设置请求头，用 60 秒有效、只能使用一次的票据代替把登录令牌放进 URL
    （登录令牌出现在 URL 中会被 Nginx 访问日志、浏览器历史记录下来）。"""
    now = int(time.time())
    payload = {"sub": sub, "uid": uid, "typ": "sse", "jti": base64.urlsafe_b64encode(os.urandom(12)).decode(),
               "iat": now, "exp": now + SSE_TTL}
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=ALGO)


def consume_sse_ticket(ticket: str) -> dict:
    """校验并作废票据（配置 Redis 时多进程共享已用记录）。失败抛 jwt.PyJWTError。"""
    payload = jwt.decode(ticket, get_settings().jwt_secret, algorithms=[ALGO])
    if payload.get("typ") != "sse" or not payload.get("jti"):
        raise jwt.InvalidTokenError("not an sse ticket")
    jti, now = str(payload["jti"]), time.time()
    url = get_settings().redis_url
    if url:
        try:
            import redis

            if not redis.Redis.from_url(url).set(f"inspector:sse:{jti}", 1, nx=True, ex=SSE_TTL * 2):
                raise jwt.InvalidTokenError("ticket already used")
            return payload
        except redis.RedisError:
            pass                                 # Redis 不可用时退回进程内记录
    for k in [k for k, exp in _used_tickets.items() if exp < now]:
        _used_tickets.pop(k, None)
    if jti in _used_tickets:
        raise jwt.InvalidTokenError("ticket already used")
    _used_tickets[jti] = now + SSE_TTL * 2
    return payload


def _mail_sig(msg: str) -> str:
    return hmac.new(get_settings().mail_link_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sign_mail_action(alarm_id: int, action: str, recipient: str, ttl: int = 86400) -> str:
    """邮件快捷操作链接签名：alarm_id + action + 收件人 + 过期时间。"""
    msg = "|".join([str(alarm_id), action, recipient, str(int(time.time()) + ttl)])
    return base64.urlsafe_b64encode(f"{msg}|{_mail_sig(msg)}".encode()).decode()


def verify_mail_action(token: str) -> dict | None:
    try:
        alarm_id, action, recipient, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
    except (ValueError, UnicodeDecodeError):
        return None
    msg = "|".join([alarm_id, action, recipient, exp])
    if not hmac.compare_digest(sig, _mail_sig(msg)) or int(exp) < time.time():
        return None
    return {"alarm_id": int(alarm_id), "action": action, "recipient": recipient}
