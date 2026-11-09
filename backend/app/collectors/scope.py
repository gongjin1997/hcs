"""采集范围的匹配引擎：纯函数，不依赖数据库。

采集器在调用云接口之前用它裁剪对象，巡检流水线用它做最终判定；规则的存取、同步与接口见 app.services.collect_scope。

一个范围由若干规则组成：{"effect": "include" | "exclude", "match": {维度: [模式, ...]}}

- 模式是精确值，或含通配符的字符串（* 匹配任意长度，? 匹配单个字符），区分大小写；空值不匹配任何模式（包括 *）
- 同一规则内各维度取“且”，同一维度内的多个模式取“或”
- 命中任一排除规则 → 范围外；存在包含规则时必须至少命中其中一条，否则范围外；没有规则 = 全部在范围内
- 判定是三值的：subject 里缺少某个维度表示“还不知道”。已有的维度足以判定时返回 True / False，否则返回 None。
  采集器在调用云接口之前只知道一部分维度（例如只知道工作空间、还不知道作业名），None 一律保留，
  由流水线拿到完整信息后做最终判定
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 维度取值：字符串；元组表示“任一取值命中即可”（例如集群名称或集群 ID）；None 表示还不知道
Value = str | tuple[str, ...] | None
Subject = Mapping[str, Value]

EFFECTS = ("include", "exclude")
MAX_RULES = 100
MAX_PATTERNS = 200          # 每条规则每个维度
MAX_PATTERN_LEN = 200


def glob_match(pattern: str, value: str) -> bool:
    """* 与 ? 通配，其余字符（包括 [ ]）按字面比较。双指针回溯，最坏 O(len(pattern) * len(value))，不会出现正则的灾难性回溯。"""
    p = v = 0
    star, mark = -1, 0
    n, m = len(value), len(pattern)
    while v < n:
        if p < m and pattern[p] == "*":
            star, mark = p, v
            p += 1
        elif p < m and (pattern[p] == "?" or pattern[p] == value[v]):
            p += 1
            v += 1
        elif star != -1:
            p = star + 1
            mark += 1
            v = mark
        else:
            return False
    while p < m and pattern[p] == "*":
        p += 1
    return p == m


def pattern_matches(pattern: str, value: str) -> bool:
    if not value:
        return False
    if "*" not in pattern and "?" not in pattern:
        return pattern == value
    return glob_match(pattern, value)


@dataclass(frozen=True)
class ScopeRule:
    effect: str
    match: tuple[tuple[str, tuple[str, ...]], ...]       # ((维度, (模式, ...)), ...)

    def result(self, subject: Subject) -> bool | None:
        unknown = False
        for dim, patterns in self.match:
            value = subject.get(dim)
            if value is None:
                unknown = True
                continue
            values = (value,) if isinstance(value, str) else value
            if not any(pattern_matches(p, v) for p in patterns for v in values):
                return False
        return None if unknown else True


@dataclass(frozen=True)
class ScopeFilter:
    rules: tuple[ScopeRule, ...] = ()

    @property
    def open(self) -> bool:
        """没有任何规则：全部在范围内。"""
        return not self.rules

    @property
    def dims(self) -> frozenset[str]:
        return frozenset(dim for rule in self.rules for dim, _ in rule.match)

    def check(self, subject: Subject) -> bool | None:
        """True = 在范围内；False = 确定在范围外；None = 现有信息还不能判定。"""
        unknown_exclude = unknown_include = has_include = included = False
        for rule in self.rules:
            result = rule.result(subject)
            if rule.effect == "exclude":
                if result is True:
                    return False
                unknown_exclude |= result is None
            else:
                has_include = True
                if result is True:
                    included = True
                unknown_include |= result is None
        if has_include and not included:
            return None if unknown_include else False
        return None if unknown_exclude else True

    def keep(self, subject: Subject) -> bool:
        """采集器裁剪用：只丢弃确定在范围外的对象。"""
        return self.check(subject) is not False


OPEN = ScopeFilter()


def _compile_rule(item: object) -> ScopeRule | None:
    if not isinstance(item, Mapping) or item.get("effect") not in EFFECTS or not isinstance(item.get("match"), Mapping):
        return None
    dims: list[tuple[str, tuple[str, ...]]] = []
    for dim, patterns in item["match"].items():
        if not isinstance(dim, str) or not isinstance(patterns, list):
            return None
        cleaned = tuple(dict.fromkeys(p for p in patterns if isinstance(p, str) and p != ""))
        if not cleaned:
            return None
        dims.append((dim, cleaned))
    return ScopeRule(item["effect"], tuple(sorted(dims))) if dims else None


def compile_scope(raw: object) -> ScopeFilter:
    """存储格式 {"rules": [...]} → 过滤器。保存时已校验；这里只防手工改库，丢弃无法识别的规则并记日志。"""
    items = raw.get("rules") if isinstance(raw, Mapping) else None
    rules: list[ScopeRule] = []
    for item in items if isinstance(items, list) else []:
        rule = _compile_rule(item)
        if rule is None:
            log.warning("collect_scope_rule_ignored", extra={"event": "collect_scope_rule_ignored"})
            continue
        rules.append(rule)
    return ScopeFilter(tuple(rules))


# ---------------------------------------------------------------- 对象 → 维度取值
def _text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def subject_of(resource_type: str, resource_id: str, name: str | None, attrs: Mapping | None) -> dict[str, Value]:
    """对象在各采集维度上的取值。没有该值时给空串（已知为空，不匹配任何模式），不给 None。

    维度与 catalog.SCOPE_DIMENSIONS 对应；观测（Observation）与台账记录（Resource）用同一个函数，
    采集器裁剪、流水线判定和范围同步因此不会各算各的。
    """
    attrs = attrs or {}
    if resource_type in ("dgc_realtime_job", "dgc_batch_job"):
        if "workspace" in attrs:
            workspace = _text(attrs["workspace"])
        else:
            workspace = resource_id.split("/", 1)[0] if "/" in resource_id else ""
        return {"workspace": workspace, "job": name or ""}
    if resource_type == "dgc_mrs_node" or (resource_type == "mrs_job"
                                           and attrs.get("source") == "dataarts_instance_detail"):
        return {"workspace": _text(attrs.get("workspace")), "job": _text(attrs.get("job_name")),
                "node": _text(attrs.get("node_name"))}
    if resource_type == "mrs_job":
        return {"cluster": (_text(attrs.get("cluster_id")), _text(attrs.get("cluster"))),
                "job": _text(attrs.get("job_name")) or name or "", "id": (resource_id, _text(attrs.get("job_id")))}
    if resource_type == "cce_workload":
        parts = resource_id.split("/")            # 集群ID/命名空间/类型/名称
        cid, namespace, workload = (parts[0], parts[1], parts[3]) if len(parts) == 4 else ("", "", "")
        return {"cluster": (_text(attrs.get("cluster_id")) or cid, _text(attrs.get("cluster"))),
                "namespace": _text(attrs.get("namespace")) or namespace,
                "workload": _text(attrs.get("workload")) or workload}
    if resource_type == "cce_cluster":
        return {"cluster": (resource_id, name or "")}
    return {"id": resource_id, "name": name or ""}


def ledger_subject(env_type: str | None = None, business: str | None = None, owner: str | None = None,
                   critical: bool | None = False) -> dict[str, Value]:
    """台账管理字段（页面维护）。资源还没有台账记录时按“都未填写”处理。"""
    return {"env": env_type or "", "business": business or "", "owner": owner or "",
            "critical": "true" if critical else "false"}
