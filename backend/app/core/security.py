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
    return jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGO])


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
