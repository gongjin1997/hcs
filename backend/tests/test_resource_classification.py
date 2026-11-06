"""分类映射仅使用合成资源；覆盖 SQL 分页 / 租户范围、配置校验及 Excel 一致性。"""
import io
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from app.api.v1.resources import router
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.ledger.excel import export_ledger
from app.main import app as production_app
from app.models import AuditLog, HcsAccount, Resource, ResourceChange, SysSetting
from app.schemas.resource import ClassificationConfig
from app.services import resource_classification as classification
from app.services.catalog import RESOURCE_TYPES

API = "/api/v1/resources"


def _rule(**values):
    return {"id": "synthetic-rule", "resource_type": "ecs_server", "field": "os", "pattern": "MRS",
            "category": "计算集群", "enabled": True, **values}


def _config(rules=None, fallback="其他"):
    return ClassificationConfig.model_validate({"rules": rules if rules is not None else [_rule()], "fallback": fallback})


def _resource(db, rid, os=None, **values):
    resource_type = values.pop("resource_type", "ecs_server")
    row = Resource(**{"resource_type": resource_type, "service": RESOURCE_TYPES[resource_type]["service"],
                      "tenant_id": 101, "resource_id": rid, "name": rid, "attrs": {"os": os},
                      "business": "人工维护的业务系统", **values})
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def classification_api(mem_db):
    for tid in (101, 202):
        mem_db.add(HcsAccount(id=tid, code=f"classification-{tid}", name=f"合成租户{tid}",
                              iam_endpoint="https://example.invalid", domain_name="test-only"))
    mem_db.commit()
    actor = CurrentUser(id=9001, username="classification-editor", display_name="分类管理员",
                        permissions=["ledger:view", "ledger:edit", "ledger:export"], tenant_ids=None)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.exception_handlers = production_app.exception_handlers.copy()
    app.dependency_overrides[get_db] = lambda: mem_db
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as client:
        yield client, actor


@pytest.fixture()
def catalog_rows(mem_db):
    rows = {key: _resource(mem_db, key, os, **values) for key, os, values in (
        ("mrs", "MRS_synthetic_image", {}),
        ("cce", "cCe_synthetic_image", {}),
        ("his", "his-synthetic-image", {}),
        ("other", "generic-linux", {}),
        ("empty", None, {}),
        ("outside", "MRS_synthetic_image", {"tenant_id": 202}),
        ("global", "CCE_synthetic_image", {"tenant_id": None}),
        ("deleted", "his-synthetic-image", {"lifecycle": "DELETED"}),
        ("vpc", "MRS_synthetic_image", {"resource_type": "vpc"}),
    )}
    mem_db.commit()
    return rows


@pytest.mark.parametrize("os,expected,source", [
    ("MRS_synthetic_image", "MRS", "ecs-os-mrs"),
    ("mrs_synthetic_image", "MRS", "ecs-os-mrs"),
    ("CCE_synthetic_image", "CCE", "ecs-os-cce"),
    ("his-synthetic-image", "HIS", "ecs-os-his"),
    ("CCE_then_MRS", "MRS", "ecs-os-mrs"),
    ("generic-linux", "其他", None),
    (None, "其他", None),
    (123, "其他", None),
    ({"os": "MRS"}, "其他", None),
])
def test_defaults_classify_os_images_only(mem_db, os, expected, source):
    row = _resource(mem_db, "synthetic", os, name="HIS 名称不能覆盖 OS 映射", business="CCE 业务")
    assert classification.classify(row, classification.defaults()) == (expected, source)
    assert row.resource_type == "ecs_server" and row.service == "ecs" and row.business == "CCE 业务"
    assert "classification" not in row.attrs


def test_defaults_do_not_reclassify_cloud_resource_types(mem_db):
    row = _resource(mem_db, "vpc", "MRS_synthetic_image", resource_type="vpc")
    assert classification.classify(row, classification.defaults()) == ("其他", None)


@pytest.mark.parametrize("field", ["os", "name", "business", "spec"])
def test_configurable_fields_and_non_ecs_catalog_types(mem_db, field):
    config = _config([_rule(resource_type="vpc", field=field, pattern="synthetic", category="自定义网络")])
    values = {field: "SYNTHETIC-value"} if field != "os" else {}
    row = _resource(mem_db, "vpc", "SYNTHETIC-value", resource_type="vpc", **values)
    assert classification.classify(row, config) == ("自定义网络", "synthetic-rule")
    expression = classification.expression(mem_db, config)
    assert mem_db.scalar(select(expression).where(Resource.id == row.id)) == "自定义网络"


def test_disabled_rules_are_skipped_and_order_controls_first_match(mem_db):
    row = _resource(mem_db, "overlap", "mrs CCE")
    rules = [_rule(id="disabled", category="禁用分类", enabled=False),
             _rule(id="first", category="首个分类"), _rule(id="second", pattern="CCE", category="第二分类")]
    first = _config(rules)
    second = _config([rules[0], rules[2], rules[1]])
    for config, expected in ((first, ("首个分类", "first")), (second, ("第二分类", "second"))):
        assert classification.classify(row, config) == expected
        assert mem_db.scalar(select(classification.expression(mem_db, config)).where(Resource.id == row.id)) == expected[0]


@pytest.mark.parametrize("value,pattern", [("prefix M%_/RS suffix", "m%_/rs"),
                                         ("prefix [a-z].* suffix", "[a-z].*"),
                                         ("prefix a\\b suffix", "A\\B")])
def test_patterns_are_literal_not_regex_or_sql_wildcards(mem_db, value, pattern):
    matching = _resource(mem_db, "matching", value)
    _resource(mem_db, "non-matching", "mrs abc axb a/b")
    config = _config([_rule(pattern=pattern)])
    expression = classification.expression(mem_db, config)
    selected = mem_db.scalars(select(Resource).where(expression == "计算集群")).all()
    assert selected == [matching]
    assert classification.classify(matching, config) == ("计算集群", "synthetic-rule")


@pytest.mark.parametrize("value,pattern", [("STRAßE", "strasse"), ("İMAGE", "i"), ("ſerver", "server"),
                                         ("ΣΥΣΤΗΜΑ", "συστημα")])
def test_sql_and_python_casefold_agree_for_unicode(mem_db, value, pattern):
    matching = _resource(mem_db, "matching", value)
    _resource(mem_db, "non-matching", "zzz")
    config = _config([_rule(pattern=pattern)])
    assert classification.classify(matching, config)[0] == "计算集群"
    selected = mem_db.scalars(select(Resource.id).where(classification.expression(mem_db, config) == "计算集群")).all()
    assert selected == [matching.id]


def test_numeric_os_does_not_become_a_text_match_in_sql(mem_db):
    row = _resource(mem_db, "numeric", 123)
    config = _config([_rule(pattern="123")])
    assert classification.classify(row, config) == ("其他", None)
    assert mem_db.scalar(select(classification.expression(mem_db, config)).where(Resource.id == row.id)) == "其他"


def test_sql_case_compiles_for_postgresql_and_sqlite(mem_db):
    config = _config([_rule(pattern="M%_RS")])
    expression = classification.expression(mem_db, config)
    sqlite_sql = str(select(Resource.id).where(expression == "计算集群").compile(dialect=sqlite.dialect()))
    assert "CASE WHEN" in sqlite_sql and "JSON_EXTRACT" in sqlite_sql and "ESCAPE" in sqlite_sql
    db = Mock()
    db.get_bind.return_value.dialect = postgresql.dialect()
    db.scalars.return_value = []
    expression = classification.expression(db, config)
    pg_sql = str(select(Resource.id).where(expression == "计算集群").compile(dialect=postgresql.dialect()))
    assert "CASE WHEN" in pg_sql and "->>" in pg_sql and "jsonb_typeof" in pg_sql and "ESCAPE" in pg_sql
    assert "translate" in pg_sql


def test_get_defaults_is_read_only_and_counts_are_scoped(mem_db, classification_api, catalog_rows):
    client, actor = classification_api
    actor.tenant_ids = [101]
    response = client.get(f"{API}/classifications", params={"resource_type": "ecs_server"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["categories"] == ["MRS", "CCE", "HIS", "其他"]
    assert data["counts"] == {"MRS": 1, "CCE": 1, "HIS": 1, "其他": 2} and data["total"] == 5
    assert data["customized"] is False and [r["id"] for r in data["rules"]] == ["ecs-os-mrs", "ecs-os-cce", "ecs-os-his"]
    assert mem_db.get(SysSetting, classification.KEY) is None
    assert mem_db.scalars(select(AuditLog)).all() == []


def test_list_detail_and_sql_filter_have_same_classification(classification_api, catalog_rows):
    client, actor = classification_api
    actor.tenant_ids = [101]
    response = client.get(API, params={"resource_type": "ecs_server", "classification": "MRS", "page_size": 1})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 1 and len(data["items"]) == 1
    row = data["items"][0]
    assert row["id"] == catalog_rows["mrs"].id and row["classification"] == "MRS"
    assert row["classification_source"] == "ecs-os-mrs"
    detail = client.get(f"{API}/{row['id']}").json()["data"]["resource"]
    assert detail["classification"] == row["classification"] and detail["classification_source"] == row["classification_source"]
    other = client.get(API, params={"resource_type": "ecs_server", "classification": "其他"}).json()["data"]
    assert other["total"] == 2 and all(r["classification_source"] is None for r in other["items"])
    assert client.get(f"{API}/{catalog_rows['outside'].id}").status_code == 404


def test_classification_filter_precedes_pagination_and_intersects_other_filters(mem_db, classification_api):
    client, actor = classification_api
    actor.tenant_ids = [101]
    rows = [_resource(mem_db, f"mrs-{i}", "mrs-image", name=f"server-{i}", business="筛选业务") for i in range(4)]
    _resource(mem_db, "cce", "cce-image", business="筛选业务")
    _resource(mem_db, "mrs-other-business", "mrs-image", business="其他业务")
    _resource(mem_db, "outside", "mrs-image", tenant_id=202, business="筛选业务")
    mem_db.commit()
    data = client.get(API, params={"classification": "MRS", "business": "筛选业务", "page": 2,
                                   "page_size": 2}).json()["data"]
    assert data["total"] == 4 and [r["id"] for r in data["items"]] == [r.id for r in rows[2:]]


def test_types_exposes_classification_options_without_changing_business(classification_api, catalog_rows):
    client, actor = classification_api
    actor.tenant_ids = [101]
    data = client.get(f"{API}/types", params={"resource_type": "ecs_server"}).json()["data"]
    assert data["classifications"] == {"categories": ["MRS", "CCE", "HIS", "其他"],
                                       "counts": {"MRS": 1, "CCE": 1, "HIS": 1, "其他": 2}, "total": 5}
    assert "人工维护的业务系统" in data["options"]["business"]
    assert set(data["dict"]) == {"business", "owners", "enforce"}
    vpc = client.get(f"{API}/types", params={"resource_type": "vpc"}).json()["data"]["classifications"]
    assert vpc == {"categories": ["其他"], "counts": {"其他": 1}, "total": 1}


def test_config_put_persists_order_and_audit_and_is_used_immediately(mem_db, classification_api, catalog_rows):
    client, _ = classification_api
    rules = [_rule(id="disabled", enabled=False), _rule(id="first", pattern="synthetic", category="新分类"),
             _rule(id="second", pattern="MRS", category="后续分类")]
    response = client.put(f"{API}/classifications", json={"rules": rules, "fallback": "未识别"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["rules"] == rules and data["fallback"] == "未识别" and data["customized"] is True
    assert data["counts"] == {"新分类": 5, "后续分类": 0, "未识别": 3}
    stored = mem_db.get(SysSetting, classification.KEY)
    assert stored.value == {"rules": rules, "fallback": "未识别"} and stored.updated_by == "分类管理员"
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "ledger.classification"))
    assert audit.target_id == classification.KEY and audit.detail["changes"]["rules"][1] == rules
    row = client.get(f"{API}/{catalog_rows['mrs'].id}").json()["data"]["resource"]
    assert row["classification"] == "新分类" and row["classification_source"] == "first"
    for resource in catalog_rows.values():
        mem_db.refresh(resource)
        assert resource.business == "人工维护的业务系统" and resource.resource_type in ("ecs_server", "vpc")
    assert mem_db.scalars(select(ResourceChange)).all() == []


@pytest.mark.parametrize("permission", ["ledger:view", "ledger:export"])
def test_config_write_requires_edit_permission(mem_db, classification_api, permission):
    client, actor = classification_api
    actor.permissions = [permission]
    assert client.put(f"{API}/classifications", json={"rules": [], "fallback": "其他"}).status_code == 403
    assert mem_db.get(SysSetting, classification.KEY) is None


def test_scoped_editor_cannot_change_global_rules(mem_db, classification_api):
    client, actor = classification_api
    actor.tenant_ids = [101]
    assert client.get(f"{API}/classifications").status_code == 200
    response = client.put(f"{API}/classifications", json={"rules": [], "fallback": "其他"})
    assert response.status_code == 403 and "全部租户" in response.json()["message"]
    assert mem_db.get(SysSetting, classification.KEY) is None


def test_view_permission_is_required_for_config_and_counts(classification_api):
    client, actor = classification_api
    actor.permissions = ["ledger:edit"]
    assert client.get(f"{API}/classifications").status_code == 403
    assert client.get(f"{API}/types").status_code == 403


@pytest.mark.parametrize("body", [
    {"rules": [_rule(pattern=" ")]}, {"rules": [_rule(pattern="x" * 129)]},
    {"rules": [_rule(category="")]}, {"rules": [_rule(category="x" * 65)]},
    {"rules": [_rule(field="ip")]}, {"rules": [_rule(resource_type="not-a-resource-type")]},
    {"rules": [_rule(id="duplicate"), _rule(id="duplicate")]},
    {"rules": [_rule(id=str(i)) for i in range(201)]},
    {"rules": [], "fallback": " "}, {"rules": [], "fallback": "x" * 65},
    {"rules": [_rule(regex=True)]}, {"rules": [], "overwrite_business": True},
])
def test_invalid_configs_do_not_write_or_audit(mem_db, classification_api, body):
    client, _ = classification_api
    response = client.put(f"{API}/classifications", json=body)
    assert response.status_code == 400, response.text
    assert mem_db.get(SysSetting, classification.KEY) is None
    assert mem_db.scalars(select(AuditLog)).all() == []


def test_config_limit_boundaries_and_empty_rules(mem_db):
    config = _config([_rule(id=str(i), pattern="x" * 128, category="分" * 64) for i in range(200)], fallback="他" * 64)
    assert len(config.rules) == 200
    row = _resource(mem_db, "no-rules", "mrs")
    config = _config([], fallback="未识别")
    assert classification.classify(row, config) == ("未识别", None)
    assert mem_db.scalar(select(classification.expression(mem_db, config)).where(Resource.id == row.id)) == "未识别"
    assert classification.summary(mem_db, config, tenant_ids=[]) == {
        "categories": ["未识别"], "counts": {"未识别": 0}, "total": 0}


def test_two_hundred_rules_can_be_saved_and_counted_through_api(mem_db, classification_api):
    client, _ = classification_api
    _resource(mem_db, "max-rules", "mrs-image")
    mem_db.commit()
    rules = [_rule(id=f"rule-{i}", pattern="MRS" if i == 199 else f"unmatched-{i}") for i in range(200)]
    response = client.put(f"{API}/classifications", json={"rules": rules, "fallback": "其他"})
    assert response.status_code == 200, response.text
    assert len(response.json()["data"]["rules"]) == 200
    assert response.json()["data"]["counts"] == {"计算集群": 1, "其他": 0}


def test_classification_save_does_not_modify_other_settings(mem_db):
    old_value = {"owners": [{"name": "历史负责人"}], "business": [{"name": "既有业务"}], "enforce": True}
    mem_db.add(SysSetting(key="ledger_dict", value=old_value, updated_by="原维护员"))
    mem_db.commit()
    classification.save(mem_db, _config(), "分类维护员")
    mem_db.commit()
    stored = mem_db.get(SysSetting, "ledger_dict")
    assert stored.value == old_value and stored.updated_by == "原维护员"


def test_manual_business_patch_recomputes_classification_and_retains_manual_audit(mem_db, classification_api):
    client, _ = classification_api
    resource = _resource(mem_db, "business-rule", "generic-linux")
    classification.save(mem_db, _config([_rule(field="business", pattern="new-business")]), "test")
    mem_db.commit()
    response = client.patch(f"{API}/{resource.id}", json={"business": "new-business", "critical": True})
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["classification"] == "计算集群" and result["classification_source"] == "synthetic-rule"
    assert result["business"] == "new-business" and result["attrs"]["os"] == "generic-linux"
    changes = mem_db.scalar(select(ResourceChange).where(ResourceChange.resource_ref == resource.id))
    assert {change["field"] for change in changes.changes} == {"business", "critical"}


def test_invalid_stored_config_falls_back_without_overwriting_history(mem_db):
    raw = {"rules": [{"invalid": "legacy"}], "fallback": "legacy"}
    mem_db.add(SysSetting(key=classification.KEY, value=raw))
    mem_db.commit()
    assert classification.load(mem_db) == classification.defaults()
    assert mem_db.get(SysSetting, classification.KEY).value == raw
    with pytest.raises(ValidationError):
        ClassificationConfig.model_validate(raw)


def test_excel_appends_classification_and_filter_matches_list(mem_db, classification_api, catalog_rows):
    client, actor = classification_api
    actor.tenant_ids = [101]
    response = client.get(f"{API}/export", params={"resource_type": "ecs_server", "classification": "MRS"})
    assert response.status_code == 200, response.text
    wb = load_workbook(io.BytesIO(response.content), data_only=True)
    ws = wb[RESOURCE_TYPES["ecs_server"]["name"]]
    headers = [cell.value for cell in ws[1]]
    assert headers[-1] == "资源分类" and headers[-2] == "备注"
    assert ws.max_row == 2 and ws.cell(2, headers.index("资源 ID") + 1).value == catalog_rows["mrs"].resource_id
    assert ws.cell(2, headers.index("资源分类") + 1).value == "MRS"
    assert ws.cell(2, headers.index("业务系统") + 1).value == "人工维护的业务系统"
    assert "共 1 个资源" in wb["汇总"]["A2"].value and "资源分类：MRS" in wb["汇总"]["A2"].value
    wb.close()
    audit = mem_db.scalar(select(AuditLog).where(AuditLog.action == "ledger.export"))
    assert audit.detail["classification"] == "MRS" and audit.tenant_id == 101


def test_excel_uses_saved_rules_and_handles_empty_filtered_result(mem_db, catalog_rows):
    classification.save(mem_db, _config([_rule(category="已配置")], fallback="未识别"), "test")
    mem_db.commit()
    for category, expected in (("已配置", 1), ("不存在的分类", 0)):
        content = export_ledger(mem_db, ["ecs_server"], tenant_ids=[101], classification=category)
        wb = load_workbook(io.BytesIO(content), data_only=True)
        ws = wb[RESOURCE_TYPES["ecs_server"]["name"]]
        assert ws.max_row == expected + 1
        if expected:
            assert ws.cell(2, ws.max_column).value == category
        wb.close()


def test_empty_scope_returns_zero_counts_and_no_filtered_resources(classification_api, catalog_rows):
    client, actor = classification_api
    actor.tenant_ids = []
    assert client.get(API, params={"classification": "MRS"}).json()["data"]["total"] == 0
    data = client.get(f"{API}/classifications").json()["data"]
    assert data["total"] == 0 and all(count == 0 for count in data["counts"].values())


def test_global_resource_is_visible_only_in_full_scope(classification_api, catalog_rows):
    client, _ = classification_api
    full = client.get(API, params={"resource_type": "ecs_server", "classification": "CCE"}).json()["data"]
    assert {row["id"] for row in full["items"]} == {catalog_rows["cce"].id, catalog_rows["global"].id}
    scoped = client.get(API, params={"classification": "CCE"}, headers={"X-Tenant-Id": "101"}).json()["data"]
    assert {row["id"] for row in scoped["items"]} == {catalog_rows["cce"].id}
