"""凭据加密：AES-256-GCM，密文格式 ENC(AES256GCM:base64(nonce + ciphertext))。

生产必须通过 INSPECTOR_MASTER_KEY 注入 32 字节主密钥（base64）；开发 / 测试环境未配置时由 jwt_secret 派生一个本地密钥，
保证演示数据里的租户密码 / SK 也是密文存储。
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

PREFIX = "ENC(AES256GCM:"


def _key() -> bytes:
    s = get_settings()
    if not s.master_key:
        if s.env == "prod":
            raise RuntimeError("未配置 INSPECTOR_MASTER_KEY")
        return hashlib.sha256(f"hcs-inspector-dev-master-key:{s.jwt_secret}".encode()).digest()
    key = base64.b64decode(s.master_key)
    if len(key) != 32:
        raise RuntimeError("主密钥必须是 32 字节")
    return key


def encrypt(plain: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plain.encode(), None)
    return f"{PREFIX}{base64.b64encode(nonce + ct).decode()})"


def decrypt(value: str) -> str:
    if not value.startswith(PREFIX):
        return value  # 兼容明文（仅限开发环境）
    raw = base64.b64decode(value[len(PREFIX):-1])
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode()


def mask(value: str | None, head: int = 4, tail: int = 4) -> str:
    """界面展示用脱敏：HPUA************0B26。"""
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * (len(value) - head - tail)}{value[-tail:]}"
