"""分页总数与实际记录矛盾时，不把部分清单当作完整快照。"""
import pytest

from app.hcs import HcsError
from app.hcs.pager import page_all


@pytest.mark.parametrize("total,items,start", [(0, [{"id": "one"}], 0),
                                            (1, [{"id": "one"}, {"id": "two"}], 0),
                                            (10, [{"id": "one"}], 10)])
def test_total_cannot_be_less_than_returned_records_and_start_offset(total, items, start):
    with pytest.raises(HcsError, match="矛盾清单"):
        list(page_all(lambda _params: {"items": items, "total": total}, "items", start=start))


def test_total_shrinking_between_pages_fails_instead_of_applying_incomplete_snapshot():
    calls = []
    def fetch(params):
        calls.append(params)
        return {"items": [{"id": "one" if len(calls) == 1 else "two"}], "total": 2 if len(calls) == 1 else 1}
    with pytest.raises(HcsError, match="矛盾清单"):
        list(page_all(fetch, "items", limit=1))
    assert len(calls) == 2


def test_valid_nonzero_offset_and_last_page_remain_supported():
    assert list(page_all(lambda _params: {"items": [{"id": "last"}], "total": 11}, "items", start=10)) == [
        {"id": "last"}]
