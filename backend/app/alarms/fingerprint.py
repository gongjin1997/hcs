import hashlib


def fingerprint(account: str, project: str, resource_type: str, resource_id: str, rule_id: str,
                sub_object: str = "") -> str:
    """告警指纹：同一账号 / 项目 / 资源 / 规则 / 子对象只会有一条活动告警（数据库部分唯一索引兜底）。"""
    raw = "|".join([account, project, resource_type, resource_id, rule_id, sub_object or ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
