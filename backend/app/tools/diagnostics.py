"""严格离线诊断：仅显式日志目录；不导入数据库/配置，不执行外部命令。"""

import argparse
import io
import json
import os
import platform
import re
import secrets
import stat
import sys
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import __version__

SERVICES = frozenset(
    {
        "api",
        "scheduler",
        "notifier",
        "mock",
        "web",
        "startup",
        "migrate",
        "bootstrap",
        "run_once",
        "build",
        "containers",
        "stop",
        "nginx-access",
        "nginx-error",
    }
)
LOG_NAME = re.compile(r"^(?P<service>[a-z_-]+)\.(?:jsonl|log)(?:\.[1-9][0-9]?)?$")
# 自由文本/异常堆栈可能夹带原始请求体或客户数据，默认不导出，仅保留定位与计时字段。
FIELDS = frozenset(
    {
        "ts",
        "time",
        "timestamp",
        "level",
        "logger",
        "service",
        "request_id",
        "client_request_id",
        "upstream_request_id",
        "tenant_id",
        "project_ref",
        "user_id",
        "run_id",
        "rule_id",
        "alarm_id",
        "notification_id",
        "report_id",
        "resource_type",
        "check_item",
        "phase",
        "reason",
        "protocol",
        "supervisor_pid",
        "child_pid",
        "exit_code",
        "expected_shutdown",
        "exit_signal",
        "dropped_records",
        "method",
        "path",
        "status",
        "cost_ms",
        "first_byte_ms",
        "response_bytes",
        "wait_ms",
        "rate_limit_ms",
        "duration_ms",
        "request_time_sec",
        "upstream_status",
        "upstream_code",
        "network_reason",
        "retryable",
        "sqlstate",
        "db_column_limit",
        "table_name",
        "column_name",
        "upstream_time_sec",
        "bytes_sent",
        "error_type",
        "exception_type",
        "error_code",
        "event",
        "task",
        "cloud_service",
        "logical_path",
        "attempt",
        "api_calls",
        "fetch_calls",
        "upstream_calls",
        "upstream_errors",
        "resource_count",
        "record_count",
        "requested_count",
        "enabled_count",
        "skipped_count",
        "created_count",
        "updated_count",
        "deleted_count",
        "change_count",
        "error_count",
        "failed_count",
        "affected_count",
        "unknown_event_count",
        "skipped_delete",
        "window_seconds",
        "max_name_chars",
        "max_id_chars",
        "max_status_chars",
        "max_az_chars",
        "max_ip_chars",
        "max_spec_chars",
        "abnormal_count",
        "cache_status",
        "instances",
        "window_days",
        "dependency",
        "kind",
        "error_name",
        "route",
    }
)
TRACE_FRAME = re.compile(
    r'^[ \t]*File "([^"\r\n]{1,2048})", line ([1-9][0-9]{0,8}), in ([A-Za-z_<>][A-Za-z0-9_.<>]{0,127})[ \t]*$',
    re.MULTILINE,
)
TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


class DiagnosticError(Exception):
    """用户可读的固定错误，不包含源路径、原始异常或内容。"""


def secure_directory(path: Path, *, create: bool = False) -> int:
    """逐组件打开目录，拒绝 symlink 和 ..；返回调用者须关闭的目录 fd。"""
    if ".." in path.parts or not hasattr(os, "O_NOFOLLOW"):
        raise DiagnosticError("需要无父级跳转路径及支持 O_NOFOLLOW 的本机 POSIX 文件系统")
    absolute = path.absolute()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise DiagnosticError("目录不可用、不是目录或包含符号链接") from None


def private_directory(path: Path) -> int:
    fd = secure_directory(path, create=True)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid():
        os.close(fd)
        raise DiagnosticError("输出目录必须属于当前用户")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise DiagnosticError("已有输出目录必须为 0700；请使用独立私有目录")
    return fd


def sanitize_record(value: dict[str, Any]) -> dict[str, Any]:  # noqa: C901  # T10
    # 延迟导入纯脱敏函数；不调用 Settings/get_settings/setup_logging。
    from app.core.logging import safe_path, sanitize_log

    selected: dict[str, Any] = {
        key: item for key, item in value.items() if key in FIELDS and isinstance(item, (str, int, float, bool))
    }
    path = selected.get("path")
    if isinstance(path, str):
        selected["path"] = safe_path(path)
    for key in ("tenant_id", "project_ref", "user_id", "run_id", "alarm_id", "notification_id", "report_id"):
        item = selected.get(key)
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
            selected.pop(key)
    for key in ("rule_id", "resource_type", "check_item", "phase", "protocol"):
        item = selected.get(key)
        if item is not None and (not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item)):
            selected.pop(key)
    for key in ("upstream_code", "error_code"):
        item = selected.get(key)
        if item is not None and (not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)):
            selected.pop(key)
    for key in ("table_name", "column_name"):
        item = selected.get(key)
        if item is not None and (not isinstance(item, str)
                                 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", item)
                                 or re.search(r"password|passwd|authorization|cookie|secret|token|credential|key", item, re.I)):
            selected.pop(key)
    if "sqlstate" in selected and (not isinstance(selected["sqlstate"], str)
                                     or not re.fullmatch(r"[A-Z0-9]{5}", selected["sqlstate"])):
        selected.pop("sqlstate")
    if "network_reason" in selected and (not isinstance(selected["network_reason"], str)
                                          or not re.fullmatch(r"[a-z_]{1,32}", selected["network_reason"])):
        selected.pop("network_reason")
    services = value.get("cloud_services")
    if isinstance(services, dict):
        counters = {}
        for name, values in list(services.items())[:32]:
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or not isinstance(values, dict):
                continue
            metrics = {key: number for key, number in values.items() if key in ("calls", "errors", "rate_limit_ms")
                       and isinstance(number, (int, float)) and not isinstance(number, bool) and 0 <= number <= 1e12}
            if metrics:
                counters[name] = metrics
        if counters:
            selected["cloud_services"] = counters
    for key in ("logical_path", "route"):
        if isinstance(selected.get(key), str):
            selected[key] = safe_path(selected[key])
    exception_type = selected.get("exception_type")
    if exception_type is not None and (not isinstance(exception_type, str) or not TYPE_NAME.fullmatch(exception_type)):
        selected.pop("exception_type")
    trace = value.get("exc")
    if not isinstance(trace, str) and value.get("logger") == "lan.stdout":
        trace = value.get("msg")  # 启动前 stderr 只提取技术 frames/type，仍不导出自由文本。
    if isinstance(trace, str):
        trace = trace[: 64 * 1024]
        frames = []
        for match in TRACE_FRAME.finditer(trace):
            basename = match[1].replace("\\", "/").rsplit("/", 1)[-1]
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,96}\.py", basename):
                frames.append({"file": basename, "line": int(match[2]), "function": match[3]})
        if frames:
            selected["frames"] = frames[-32:]
        lines = trace.rstrip().splitlines()
        name = lines[-1].split(":", 1)[0] if lines else ""
        if "exception_type" not in selected and TYPE_NAME.fullmatch(name) and name.endswith(
            ("Error", "Exception", "Timeout", "Interrupt", "Exit", "Warning", "ExceptionGroup")
        ):
            selected["exception_type"] = name
    return sanitize_log(selected)


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024
    max_files: int = 30
    max_line_bytes: int = 64 * 1024

    def validate(self) -> None:
        if not (
            1 <= self.max_file_bytes <= 10 * 1024 * 1024
            and self.max_file_bytes <= self.max_total_bytes <= 50 * 1024 * 1024
            and 1 <= self.max_files <= 100
            and 1 <= self.max_line_bytes <= 64 * 1024
        ):
            raise DiagnosticError("大小限制无效：单文件≤10MiB、总量≤50MiB、文件数1–100、单行≤64KiB")


def _encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()


def _collect_file(directory: int, name: str, input_budget: int, output_budget: int, limits: Limits) -> tuple[dict, bytes]:
    report: dict[str, Any] = {
        "name": name,
        "read_bytes": 0,
        "written_bytes": 0,
        "records": 0,
        "non_json_lines": 0,
        "omitted_lines": 0,
        "truncated": False,
    }
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return {**report, "skipped": "unreadable_or_symlink"}, b""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        return {**report, "skipped": "not_regular_or_hardlinked"}, b""
    with os.fdopen(fd, "rb", buffering=0) as source:
        offset = max(0, info.st_size - input_budget)
        source.seek(offset)
        data = source.read(input_budget)  # 最多读取 tail 预算，不扫描整个大文件。
    report["read_bytes"] = len(data)
    report["truncated"] = offset > 0 or len(data) < info.st_size
    if offset:
        # 舍弃 tail 起点的半行，不能把被截断的片段当成独立 JSON。
        _, separator, data = data.partition(b"\n")
        if not separator:
            data = b""
        report["omitted_lines"] += 1
    output = bytearray()
    for line in data.splitlines():
        if len(line) > limits.max_line_bytes:
            report["omitted_lines"] += 1
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeError, RecursionError):
            report["non_json_lines"] += 1
            continue
        try:
            clean = sanitize_record(record)
            encoded = _encode(clean)
        except (ValueError, TypeError, RecursionError):
            report["omitted_lines"] += 1
            continue
        if not clean:
            report["omitted_lines"] += 1
            continue
        if len(output) + len(encoded) > output_budget:
            report["truncated"] = True
            break
        output.extend(encoded)
        report["records"] += 1
    report["written_bytes"] = len(output)
    return report, bytes(output)


DEFAULT_LIMITS = Limits()


def collect(
    log_dir: Path, limits: Limits = DEFAULT_LIMITS, *, include_logs: bool = True
) -> tuple[dict, list[tuple[str, bytes]]]:
    limits.validate()
    fd = secure_directory(log_dir)
    reports, logs = [], []
    read_remaining = write_remaining = limits.max_total_bytes
    try:
        names = []
        # 扫描本身也有界，不递归遍历任意目录树。
        with os.scandir(fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 10000:
                    raise DiagnosticError("目录条目过多，请指定专用日志目录")
                match = LOG_NAME.fullmatch(entry.name)
                if match and match["service"] in SERVICES:
                    names.append(entry.name)

        def order(name: str) -> tuple[int, str]:
            suffix = name.rsplit(".", 1)[-1]
            return (int(suffix) if suffix.isdigit() else 0, name)

        for name in sorted(names, key=order)[: limits.max_files]:
            if read_remaining <= 0 or write_remaining <= 0:
                break
            report, content = _collect_file(
                fd, name, min(limits.max_file_bytes, read_remaining), min(limits.max_file_bytes, write_remaining), limits
            )
            read_remaining -= report["read_bytes"]
            write_remaining -= len(content)
            reports.append(report)
            if include_logs and content:
                logs.append((f"logs/{name}.jsonl", content))
    finally:
        os.close(fd)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "offline": True,
        "version": __version__,
        "runtime": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine()},
        "limits": {
            "max_file_bytes": limits.max_file_bytes,
            "max_total_bytes": limits.max_total_bytes,
            "max_files": limits.max_files,
            "max_line_bytes": limits.max_line_bytes,
        },
        "candidate_files": len(names),
        "collected_files": len(reports),
        "files": reports,
        "read_bytes": limits.max_total_bytes - read_remaining,
        "log_bytes": limits.max_total_bytes - write_remaining,
        "notes": [
            "只读指定目录的已知服务日志，不读取环境配置、数据库、容器配置或网络。",
            "非 JSON 行只统计；自由文本消息、异常堆栈、请求体与非允许字段不打包。",
            "这是离线文件概况，不表示 API、数据库或后台进程健康。",
        ],
    }
    return manifest, logs


def bundle(log_dir: Path, output_dir: Path, limits: Limits = DEFAULT_LIMITS) -> Path:
    manifest, logs = collect(log_dir, limits)
    fd = private_directory(output_dir)
    name = f"hcs-diagnostics-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}.tar.gz"
    try:
        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            with os.fdopen(output, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
                for member, data in [("manifest.json", _encode(manifest)), *logs]:
                    entry = tarfile.TarInfo(member)
                    entry.size, entry.mode, entry.mtime = len(data), 0o600, 0
                    archive.addfile(entry, io.BytesIO(data))
        except Exception:
            os.unlink(name, dir_fd=fd)
            raise
    finally:
        os.close(fd)
    return output_dir / name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("overview", "bundle"))
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-file-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--max-total-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-files", type=int, default=30)
    args = parser.parse_args(argv)
    if args.action == "bundle" and args.output_dir is None:
        parser.error("bundle 必须显式提供 --output-dir")
    limits = Limits(args.max_file_bytes, args.max_total_bytes, args.max_files)
    try:
        if args.action == "overview":
            report, _ = collect(args.log_dir, limits, include_logs=False)
            print(json.dumps(report, ensure_ascii=False))
        else:
            output = bundle(args.log_dir, args.output_dir, limits)
            print(json.dumps({"bundle": str(output), "offline": True}, ensure_ascii=False))
        return 0
    except (DiagnosticError, OSError):
        print("离线诊断失败：请核对专用日志/输出目录权限、符号链接及大小限制", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
