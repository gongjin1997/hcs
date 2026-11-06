"""EVS(for 云块存储)25.3.0 §5.6.28，实际 PDF 第222–227页。"""
from types import SimpleNamespace

import pytest

from app.hcs.errors import HcsError
from app.ledger.fetchers import fetch_evs
from app.tools.probe import PROBES, Runner


def test_evs_probe_does_not_send_invalid_zero_offset_or_expect_undocumented_count():
    descriptor = next(item for item in PROBES if item.code == "evs.volumes")
    assert descriptor.params == {} and descriptor.total is None
    calls = []

    def get(service, path, **kwargs):
        calls.append((service, path, kwargs))
        return {"volumes": []}

    result = Runner(SimpleNamespace(get=get), SimpleNamespace(), 1, False).run(descriptor)
    assert result["result"] == "OK" and "total_missing" not in result
    assert calls == [("evs", "/v2/{project_id}/cloudvolumes/detail", {"params": {"limit": 1}})]


@pytest.mark.parametrize("link", ["https://unfollowed.example.invalid/next",
                                  {"href": "https://unfollowed.example.invalid/next", "rel": "next"}])
def test_cbs_partial_short_page_uses_last_id_marker_without_following_links(link):
    pages = iter([{"volumes": [{"id": "vol-a", "name": "disk-a"}], "volumes_links": [link]},
                  {"volumes": [{"id": "vol-b", "name": "disk-b"}], "volumes_links": []}])
    calls = []

    def get(service, path, *, params):
        calls.append((service, path, params))
        return next(pages)

    records = fetch_evs(SimpleNamespace(get=get), {})
    assert [item.resource_id for item in records] == ["vol-a", "vol-b"]
    assert calls == [("evs", "/v2/{project_id}/cloudvolumes/detail", {"limit": 100}),
                     ("evs", "/v2/{project_id}/cloudvolumes/detail", {"limit": 100, "marker": "vol-a"})]


@pytest.mark.parametrize("reported", [False, True, "true", None])
def test_cbs_unsupported_encrypted_field_does_not_become_a_security_claim(reported):
    body = {"volumes": [{"id": "vol-a", "name": "disk-a", "encrypted": reported}]}
    record = fetch_evs(SimpleNamespace(get=lambda *args, **kwargs: body), {})[0]
    assert record.attrs["encrypted"] is None
    assert "encrypted" in record.clear_attrs


def test_cbs_empty_partial_response_cannot_be_used_as_an_empty_snapshot():
    body = {"volumes": [], "volumes_links": ["https://unfollowed.example.invalid/next"]}
    with pytest.raises(HcsError) as error:
        fetch_evs(SimpleNamespace(get=lambda *args, **kwargs: body), {})
    assert error.value.code == "LOCAL.INCOMPLETE_PAGE"
