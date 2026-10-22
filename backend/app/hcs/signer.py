"""华为云 / HCS API 网关 AK/SK 签名（SDK-HMAC-SHA256），与官方 APIG SDK 的签名算法一致。

签名步骤：
1. 规范请求 CanonicalRequest =
       HTTPMethod \\n CanonicalURI \\n CanonicalQueryString \\n CanonicalHeaders \\n SignedHeaders \\n HexEncode(SHA256(Body))
   - CanonicalURI：路径先 URL 解码再按段编码（保留 ~），结尾补 “/”
   - CanonicalQueryString：参数名排序，key=value 用 & 连接（同名参数按值排序）
   - CanonicalHeaders：参与签名的请求头，名称小写、值去首尾空格，每行 name:value，末尾带换行
2. 待签字符串 StringToSign = "SDK-HMAC-SHA256" \\n X-Sdk-Date \\n HexEncode(SHA256(CanonicalRequest))
3. 签名 Signature = HexEncode(HMAC-SHA256(SK, StringToSign))
4. 请求头 Authorization: SDK-HMAC-SHA256 Access={AK}, SignedHeaders={h1;h2}, Signature={sig}

服务端（以及本仓库的 Mock HCS）按同样步骤计算后比对；X-Sdk-Date 与服务端时间相差超过 15 分钟会被拒绝。
"""
import hashlib
import hmac
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, unquote, urlsplit

ALGORITHM = "SDK-HMAC-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"
HEADER_DATE = "X-Sdk-Date"
MAX_SKEW_SECONDS = 15 * 60


def _enc(s: str) -> str:
    return quote(s, safe="~")


def canonical_uri(path: str) -> str:
    uri = "/".join(_enc(seg) for seg in unquote(path or "/").split("/"))
    return uri if uri.endswith("/") else uri + "/"


def canonical_query(items: Iterable[tuple[str, str]]) -> str:
    grouped: dict[str, list[str]] = {}
    for k, v in items:
        grouped.setdefault(k, []).append(v)
    out = []
    for k in sorted(grouped):
        for v in sorted(grouped[k]):
            out.append(f"{_enc(k)}={_enc(str(v))}")
    return "&".join(out)


def canonical_request(method: str, path: str, query: Iterable[tuple[str, str]], headers: dict[str, str],
                      signed_headers: list[str], body: bytes) -> str:
    lower = {k.lower(): v.strip() for k, v in headers.items()}
    canon_headers = "".join(f"{h}:{lower.get(h, '')}\n" for h in signed_headers)
    return "\n".join([method.upper(), canonical_uri(path), canonical_query(query), canon_headers,
                      ";".join(signed_headers), hashlib.sha256(body or b"").hexdigest()])


def string_to_sign(canonical: str, sdk_date: str) -> str:
    return f"{ALGORITHM}\n{sdk_date}\n{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def signature(sts: str, secret_key: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), sts.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(method: str, url: str, headers: dict[str, str], body: bytes, access_key: str, secret_key: str,
         now: datetime | None = None) -> dict[str, str]:
    """计算签名，返回需要追加到请求上的头（X-Sdk-Date、Authorization）。

    headers 为参与签名的请求头（会自动补 host、X-Sdk-Date）；url 需包含完整查询串。
    """
    parts = urlsplit(url)
    sdk_date = (now or datetime.now(UTC)).strftime(DATE_FORMAT)
    hdrs = {k.lower(): v for k, v in headers.items()}
    hdrs.setdefault("host", parts.netloc)
    hdrs["x-sdk-date"] = sdk_date
    signed = sorted(hdrs)
    canonical = canonical_request(method, parts.path, parse_qsl(parts.query, keep_blank_values=True), hdrs, signed,
                                  body)
    sig = signature(string_to_sign(canonical, sdk_date), secret_key)
    return {HEADER_DATE: sdk_date,
            "Authorization": f"{ALGORITHM} Access={access_key}, SignedHeaders={';'.join(signed)}, Signature={sig}"}


def parse_authorization(value: str | None) -> dict[str, str] | None:
    """解析 Authorization 头：{"access", "signed_headers", "signature"}；格式不对返回 None。"""
    if not value or not value.startswith(ALGORITHM + " "):
        return None
    fields: dict[str, str] = {}
    for item in value[len(ALGORITHM) + 1:].split(","):
        k, _, v = item.strip().partition("=")
        fields[k] = v
    if not {"Access", "SignedHeaders", "Signature"} <= fields.keys():
        return None
    return {"access": fields["Access"], "signed_headers": fields["SignedHeaders"], "signature": fields["Signature"]}


def verify(method: str, path: str, query: Iterable[tuple[str, str]], headers: dict[str, str], body: bytes,
           secret_lookup, now: datetime | None = None) -> str:
    """服务端校验（Mock HCS / 单元测试用）：成功返回 AK，失败抛 ValueError(原因)。secret_lookup(ak) -> sk | None。"""
    lower = {k.lower(): v for k, v in headers.items()}
    auth = parse_authorization(lower.get("authorization"))
    if auth is None:
        raise ValueError("Authorization 格式不正确")
    sk = secret_lookup(auth["access"])
    if not sk:
        raise ValueError("AK 不存在或已停用")
    sdk_date = lower.get("x-sdk-date", "")
    try:
        ts = datetime.strptime(sdk_date, DATE_FORMAT).replace(tzinfo=UTC)
    except ValueError as e:
        raise ValueError("缺少或无法解析 X-Sdk-Date") from e
    if abs(((now or datetime.now(UTC)) - ts).total_seconds()) > MAX_SKEW_SECONDS:
        raise ValueError("X-Sdk-Date 与服务端时间相差超过 15 分钟")
    signed = auth["signed_headers"].split(";")
    expected = signature(string_to_sign(canonical_request(method, path, query, lower, signed, body), sdk_date), sk)
    if not hmac.compare_digest(expected, auth["signature"]):
        raise ValueError("签名不匹配")
    return auth["access"]
