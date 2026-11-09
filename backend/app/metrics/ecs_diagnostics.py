"""Safe ECS collection explanations shared by task results and instance views."""

METRIC_KEYS = ("cpu_util", "mem_util", "disk_util", "boot_time")
REASONS = {
    "manageone_disabled": "ManageOne 性能接入未开启，请检查本租户的 ManageOne 配置",
    "manageone_not_configured": "ManageOne 北向地址或认证凭据未配置完整，请检查本租户的 ManageOne 配置",
    "no_manageone_mapping": "ManageOne 未找到本项目 ECS 的唯一资源映射，请检查原生实例 ID 与项目 ID",
    "manageone_source_unverified": "ManageOne ECS 资源来源信息不完整，暂不能确认指标归属",
    "manageone_not_selected": "当前采集任务未选择 ManageOne 来源",
    "manageone_metric_unsupported": "ManageOne 当前不提供开机时间指标",
    "aom_disabled": "AOM 已停用，当前租户仍选择旧 AOM 来源；请核实实际可用的监控来源",
    "prometheus_not_configured": "租户 Prometheus 地址未配置，请在租户接入中配置已有监控服务",
    "legacy_ces_disabled": "CES 兼容开关未开启；仅选择 CES 不会发起查询，须先核实局点接口",
    "metrics_source_disabled": "租户选择了仅服务原生字段；ECS 清单、状态和配额不含 CPU、内存、磁盘使用率",
    "source_not_configured": "未配置可用指标源；AUTO 不会自动发现监控服务，请核实租户已有的监控来源",
    "no_unambiguous_ip": "所选实例没有可唯一匹配的私网 IP，请核对台账地址及同租户重复 IP",
    "no_matching_series": "监控查询未匹配到实例序列，请核对采集端、实例标签及私网 IP",
    "stale_samples": "返回的采样已过期，请检查监控端采集时间和任务周期",
    "incomplete_fresh_coverage": "部分实例或指标缺少有效期内的采样，请核对监控覆盖范围",
    "metric_query_missing": "指标查询表达式为空，请检查 ECS 采集策略中的映射",
    "legacy_metric_unsupported": "所选指标不受当前 CES 兼容接口支持",
    "unverified_query_unit": "自定义查询未声明有效单位，请检查 ECS 采集策略中的映射",
    "invalid_prometheus_source": "Prometheus 来源配置无效，请检查租户接入配置",
    "invalid_prometheus_mapping": "Prometheus 映射配置无效，请检查 ECS 采集策略",
    "invalid_timestamp_query": "源时间查询配置无效，请检查 ECS 采集策略",
    "invalid_query_window": "指标查询时间窗口配置无效",
    "scope_mismatch": "指标查询的租户或项目范围不匹配，本次未请求",
    "query_failed": "指标查询失败，请查看该次任务的上游请求日志",
}
STATUSES = {
    "no_source": "未接入指标源",
    "mapping_missing": "指标映射不完整",
    "no_series": "未匹配到指标序列",
    "no_data": "本次未返回实际采样，请检查监控端数据和查询窗口",
    "partial": "部分指标缺失，已保留实际返回的采样",
    "error": "指标查询失败，请查看该次任务的上游请求日志",
    "ready": "查询结果可用",
    "no_selected_resources": "当前采样范围没有实例",
}
INCOMPLETE = frozenset(("no_source", "mapping_missing", "no_series", "no_data", "partial", "error"))


def unavailable_reason(tenant: object) -> str:
    mode = str(getattr(tenant, "metrics_source", "AUTO") or "AUTO").upper()
    if mode == "MANAGEONE":
        config = getattr(tenant, "manageone_config", None)
        return ("manageone_disabled" if not isinstance(config, dict) or config.get("enabled") is not True
                else "manageone_not_configured")
    return {"AOM": "aom_disabled", "PROMETHEUS": "prometheus_not_configured",
            "CES": "legacy_ces_disabled", "NONE": "metrics_source_disabled"}.get(mode, "source_not_configured")


def diagnostic_reasons(diagnostic: dict) -> list[str]:
    metrics = diagnostic.get("metrics")
    if not isinstance(metrics, dict):
        return []
    reasons = {entry.get("reason") for key in METRIC_KEYS
               if isinstance(entry := metrics.get(key), dict) and isinstance(entry.get("reason"), str)}
    return sorted(reason for reason in reasons if reason in REASONS)


def diagnostic_message(diagnostic: dict) -> str:
    """Never echo endpoint URLs, arbitrary provider errors, labels or PromQL."""
    reasons = diagnostic_reasons(diagnostic)
    return "；".join(REASONS[reason] for reason in reasons) if reasons else STATUSES.get(
        str(diagnostic.get("status", "")), STATUSES["no_data"])


def log_summary(stats: dict) -> dict:
    diagnostic = stats.get("metrics_diagnostic")
    if not isinstance(diagnostic, dict):
        return {}
    metrics = diagnostic.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}

    def count(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    source, status = stats.get("metrics_source"), diagnostic.get("status")
    return {
        "metrics_source": source if source in ("NONE", "PROMETHEUS", "CES", "MANAGEONE") else "unknown",
        "metrics_status": status if isinstance(status, str) and status in STATUSES else "unknown",
        "metrics_reasons": diagnostic_reasons(diagnostic),
        "metrics_requested_resources": count(diagnostic.get("requested_resources", stats.get("servers"))),
        "with_metrics": count(stats.get("with_metrics")),
        "sample_points": count(stats.get("sample_points")),
        **{f"metrics_{field}": sum(count(entry.get(field)) for key in METRIC_KEYS
                                  if isinstance(entry := metrics.get(key), dict))
           for field in ("fresh_points", "stale_points", "rejected_points")},
    }
