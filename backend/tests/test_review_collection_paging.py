"""分页必须证明完整；异常响应不能当作空快照释放已有台账。"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.hcs.errors import HcsError
from app.hcs.pager import page_all
from app.k8s.kube import KubeApi
from app.ledger.fetchers import marker_all
from app.ledger.sync import sync_ledger
from app.models import Resource
from app.services.tenancy import Target


def test_offset_pagination_follows_reported_total_when_server_caps_page_size():
    rows = [{"id": str(i)} for i in range(5)]
    offsets = []
    def fetch(params):
        offsets.append(params["offset"])
        return {"items": rows[params["offset"]:params["offset"] + 2], "total": len(rows)}
    assert list(page_all(fetch, "items", limit=100)) == rows
    assert offsets == [0, 2, 4]


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": {}}, {"items": "not-a-list"}, {"items": [None]}])
def test_invalid_list_response_is_not_a_successful_empty_snapshot(body):
    with pytest.raises(HcsError):
        list(page_all(lambda _: body, "items"))


def test_offset_page_budget_exhaustion_is_an_error_not_a_partial_success():
    with pytest.raises(HcsError):
        list(page_all(lambda _: {"items": [{"id": "first"}], "total": 2}, "items", limit=1, max_pages=1))


def test_empty_page_before_reported_total_is_not_a_complete_snapshot():
    def fetch(params):
        return {"items": [{"id": "first"}], "total": 2} if params["offset"] == 0 else {"items": [], "total": 2}
    with pytest.raises(HcsError):
        list(page_all(fetch, "items", limit=1))


def test_marker_page_budget_and_repeated_marker_cannot_silently_truncate():
    with pytest.raises(HcsError):
        list(marker_all(lambda _: {"items": [{"id": "same"}]}, "items", limit=1, max_pages=2))


def test_kubernetes_continue_token_budget_exhaustion_does_not_return_partial_pods():
    client = SimpleNamespace(get=lambda *_a, **_k: {"items": [{"metadata": {"name": "first"}}],
                                                   "metadata": {"continue": "more"}})
    with pytest.raises(HcsError):
        KubeApi(client, "cluster", page_size=1)._list("/api/v1/pods", max_pages=1)


def test_malformed_cloud_list_does_not_mark_existing_resource_deleted(mem_db):
    row = Resource(service="dcs", resource_type="dcs_instance", resource_id="retained", name="retained",
                   tenant_id=101, project_ref=1001)
    mem_db.add(row)
    mem_db.commit()
    client = SimpleNamespace(get=lambda *_a, **_k: {}, configure_qps=lambda *_a: None,
                             metrics_snapshot=lambda: {}, calls=1)
    target = Target(101, "review", "review", "review", 1001, "project", "project", "region")
    run = sync_ledger(mem_db, client=client, target=target, types=["dcs_instance"])
    assert run.status == "FAILED"
    assert mem_db.scalar(select(Resource)).lifecycle == "ACTIVE"
