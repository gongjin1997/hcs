"""现场只读诊断：容器 DNS / TCP / TLS / HTTP；不读取或发送租户凭据，不修改配置。"""

from __future__ import annotations

import argparse
import errno
import json
import os
import queue
import socket
import ssl
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import certifi
import httpx

DEFAULT_SERVICES = ("iam", "ecs", "cce", "dcs", "gaussdb", "mrs", "dataarts", "evs", "vpc", "elb", "dws")


def failure(error: BaseException) -> dict:
    """仅输出类型和固定分类；原异常可能包含 URL / 代理口令，禁止回显。"""
    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 16:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for item in (current.__context__, current.__cause__):
            if item is not None:
                pending.append(item)
    reason = "UNKNOWN"
    for item in chain:
        if isinstance(item, ssl.SSLCertVerificationError):
            reason = "TLS_CERTIFICATE"
            break
        if isinstance(item, socket.gaierror):
            reason = "DNS"
            break
        if isinstance(item, OSError):
            code = getattr(item, "errno", None)
            reason = {
                errno.ECONNREFUSED: "TCP_REFUSED",
                errno.ENETUNREACH: "NO_ROUTE",
                errno.EHOSTUNREACH: "NO_ROUTE",
                errno.ETIMEDOUT: "TIMEOUT",
            }.get(code, reason)
        if isinstance(item, (httpx.TimeoutException, TimeoutError)):
            reason = "TIMEOUT"
    if reason == "UNKNOWN" and any(isinstance(item, ssl.SSLError) for item in chain):
        reason = "TLS_HANDSHAKE"
    code = next(
        (
            getattr(item, "verify_code", None)
            for item in chain
            if isinstance(item, ssl.SSLCertVerificationError)
        ),
        None,
    )
    result = {
        "ok": False,
        "reason": reason,
        "exception_types": [type(item).__name__ for item in chain],
    }
    if isinstance(code, int):
        result["certificate_verify_code"] = code
    return result


def resolve(host: str, port: int, timeout: float = 5) -> list:
    result: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            value = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            value = error
        result.put(value)

    threading.Thread(target=run, daemon=True).start()
    try:
        value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError() from None
    if isinstance(value, BaseException):
        raise value
    return list(
        {
            (family, kind, protocol, address): (family, kind, protocol, name, address)
            for family, kind, protocol, name, address in value
        }.values()
    )[:4]


def tls_context(verify: bool, ca_file: str | None) -> ssl.SSLContext:
    # 与 HTTPX verify=True / 显式 CA 的信任来源一致；不擅自关闭生产证书校验。
    if not verify:
        return ssl._create_unverified_context()
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    if os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    return ssl.create_default_context(cafile=certifi.where())


def check(url: str, *, verify: bool = True, ca_file: str | None = None) -> dict:
    try:
        target = urlsplit(url)
        port = target.port or (443 if target.scheme == "https" else 80)
        if (
            target.scheme not in ("http", "https")
            or not target.hostname
            or target.username is not None
            or target.password is not None
        ):
            raise ValueError()
        if target.query or target.fragment:
            raise ValueError()
        host = target.hostname
    except ValueError:
        return {
            "ok": False,
            "configuration_error": "IAM 地址须为不含账号、密码、查询参数的 HTTP(S) 地址",
        }
    report = {
        "endpoint": urlunsplit((target.scheme, target.netloc, "", "", "")),
        "container_probe": True,
        "tls_verify": verify,
        "ca_file_configured": bool(ca_file),
        "proxy_environment_present": [
            name
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
            )
            if os.environ.get(name)
        ],
        "tenant_credentials_sent": False,
        "configuration_modified": False,
    }
    if ca_file:
        report["ca_file_exists"] = Path(ca_file).is_file()
        report["ca_file_readable"] = os.access(ca_file, os.R_OK)
    try:
        addresses = resolve(host, port)
        report["dns"] = {
            "ok": True,
            "addresses": [address[4][0] for address in addresses],
        }
    except (OSError, TimeoutError) as error:
        addresses = []
        report["dns"] = failure(error)
    context = None
    try:
        context = tls_context(verify, ca_file) if target.scheme == "https" else None
    except (OSError, ValueError) as error:
        report["tls_configuration"] = {**failure(error), "reason": "CA_CONFIGURATION"}
    attempts = []
    for family, kind, protocol, _, address in addresses:
        attempt = {"address": address[0], "port": port}
        try:
            with socket.socket(family, kind, protocol) as connection:
                connection.settimeout(3)
                connection.connect(address)
                attempt["tcp"] = {"ok": True}
                if target.scheme == "https" and context is not None:
                    try:
                        with context.wrap_socket(
                            connection, server_hostname=host
                        ) as secure:
                            attempt["tls"] = {
                                "ok": True,
                                "version": secure.version(),
                                "certificate_verified": verify,
                            }
                    except (OSError, ValueError) as error:
                        attempt["tls"] = failure(error)
        except (OSError, ValueError) as error:
            attempt["tcp"] = failure(error)
        attempts.append(attempt)
        if attempt.get("tcp", {}).get("ok") and (
            target.scheme == "http" or attempt.get("tls", {}).get("ok")
        ):
            break
    report["direct_connections"] = attempts
    if target.scheme == "https" and context is None:
        report["http"] = {"ok": False, "reason": "CA_CONFIGURATION"}
    else:
        try:
            # 只读取状态码；无认证、无重定向、无响应体，不验证 AK/SK 权限。
            with (
                httpx.Client(
                    verify=context if context is not None else verify,
                    timeout=httpx.Timeout(5, connect=3),
                    follow_redirects=False,
                ) as client,
                client.stream("HEAD", url.rstrip("/") + "/") as response,
            ):
                report["http"] = {
                    "ok": True,
                    "status": response.status_code,
                    "authentication_tested": False,
                }
        except (httpx.HTTPError, OSError, ValueError) as error:
            report["http"] = failure(error)
    report["ok"] = report["http"]["ok"]
    report["conclusion"] = (
        "HTTP 链路可达，未测试 AK/SK 或用户权限"
        if report["ok"]
        else "连接失败，请按 DNS、TCP、TLS、HTTP 各阶段结果处理"
    )
    if not verify:
        report["warning"] = (
            "当前部署已关闭 TLS 证书校验；本工具未修改此设置，正式环境应配置内部 CA 后启用校验"
        )
    return report


def configuration_warnings(
    region: str, domain: str, project_name: str | None
) -> list[str]:
    warnings = []
    if (
        domain.lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .startswith("console.")
    ):
        warnings.append(
            "外部域名填成了控制台主机；默认服务域名不应包含 console. 前缀，请核对局点 external_global_domain_name"
        )
    if project_name and region.endswith("_" + project_name):
        warnings.append(
            "区域可能使用了资源空间名称，请核对局点 region0_id；不能根据名称自动推断区域"
        )
    return warnings


def check_services(targets: list[dict], *, verify: bool, ca_file: str | None) -> dict:
    """只对解析后的服务地址做无认证连通性检查，相同网关只检查一次。"""
    reports, cache = [], {}
    for target in targets:
        if "url" not in target:
            reports.append({**target, "ok": False})
            continue
        url = target["url"]
        if url not in cache:
            cache[url] = check(url, verify=verify, ca_file=ca_file)
        reports.append({"service": target["service"], "endpoint_source": target["endpoint_source"], **cache[url]})
    return {"ok": bool(reports) and all(item["ok"] for item in reports), "services": reports,
            "tenant_credentials_sent": False, "configuration_modified": False,
            "authentication_tested": False}


def service_targets(resolver, services: list[str], *, overrides: dict) -> list[dict]:
    targets = []
    for service in services:
        source = "tenant" if service == "iam" or service in overrides else "global"
        try:
            url = resolver.base(service)
        except (KeyError, ValueError):
            targets.append({"service": service, "endpoint_source": source,
                            "configuration_error": "服务 Endpoint 缺失或模板不完整"})
        else:
            targets.append({"service": service, "endpoint_source": source, "url": url})
    return targets


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=int)
    target.add_argument("--url")
    parser.add_argument("--services", help=f"all 或 {','.join(DEFAULT_SERVICES)} 的子集；不发送租户凭据")
    parser.add_argument("--project-ref", type=int, help="选择该租户下启用的项目记录编号，默认首个启用项目")
    args = parser.parse_args(argv)
    if (args.services is not None or args.project_ref is not None) and args.tenant is None:
        parser.error("--services 和 --project-ref 必须与 --tenant 一起使用")
    if args.project_ref is not None and args.project_ref <= 0:
        parser.error("项目记录编号必须大于 0")
    services = list(DEFAULT_SERVICES) if args.services == "all" else list(dict.fromkeys(
        item.strip() for item in (args.services or "").split(",") if item.strip()))
    if args.services is not None and (not services or any(item not in DEFAULT_SERVICES for item in services)):
        parser.error(f"--services 只支持 all 或 {','.join(DEFAULT_SERVICES)}")
    from app.core.config import get_settings

    settings = get_settings()
    warnings = []
    url = args.url
    targets = []
    project = None
    if args.tenant is not None:
        if args.tenant <= 0:
            parser.error("租户编号必须大于 0")
        from app.core.db import SessionLocal
        from app.models import HcsAccount, HcsProject
        from sqlalchemy import select

        with SessionLocal() as db:
            # 只读取端点及项目配置；不查询 access_key、secret_cipher 或口令。
            account = db.execute(select(HcsAccount.iam_endpoint, HcsAccount.endpoints)
                                 .where(HcsAccount.id == args.tenant)).first()
            if account is None or not account.iam_endpoint:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "configuration_error": "找不到指定租户的 IAM 地址",
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
            url = account.iam_endpoint
            query = select(HcsProject.id, HcsProject.region_id, HcsProject.external_domain,
                           HcsProject.project_name).where(HcsProject.account_id == args.tenant,
                                                          HcsProject.enabled.is_(True))
            if args.project_ref is not None:
                query = query.where(HcsProject.id == args.project_ref)
            project = db.execute(query.order_by(HcsProject.id)).first()
            if (args.project_ref is not None or any(service != "iam" for service in services)) and not project:
                print(json.dumps({"ok": False, "configuration_error": "该租户下找不到指定的启用项目"},
                                 ensure_ascii=False))
                return 2
            if project:
                warnings = configuration_warnings(project.region_id, project.external_domain, project.project_name)
            if services:
                from app.hcs.endpoints import EndpointResolver

                resolver = EndpointResolver({**settings.hcs_endpoints, **(account.endpoints or {}), "iam": url},
                                            project.region_id if project else "", project.external_domain if project else "",
                                            settings.hcs_scheme)
                targets = service_targets(resolver, services, overrides=account.endpoints or {})
    report = (check_services(targets, verify=settings.hcs_verify, ca_file=settings.hcs_ca_file or None) if services
              else check(url, verify=settings.hcs_verify, ca_file=settings.hcs_ca_file or None))
    if project:
        report["project_ref"] = project.id
    report["configuration_warnings"] = warnings
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except Exception:  # noqa: BLE001 - CLI 最外层边界禁止把配置/数据库异常原文泄漏到诊断输出。
        # Settings / 数据库连接错误也可能带配置值；现场诊断不输出原异常。
        print(
            json.dumps(
                {
                    "ok": False,
                    "configuration_error": "读取容器配置或租户信息失败，请检查容器状态和租户编号",
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
