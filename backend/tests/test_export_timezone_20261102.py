"""导出文件名和单元格日期在午夜/DST边界使用相同系统时区。"""
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.xlsx import cell_value, filename_stamp


@pytest.mark.parametrize('zone,expected', [
    ('Asia/Shanghai', '20261103_073000'),
    ('Asia/Tokyo', '20261103_083000'),
    ('America/New_York', '20261102_183000'),
    ('UTC', '20261102_233000'),
])
def test_export_filename_uses_configured_timezone(monkeypatch, zone, expected):
    monkeypatch.setattr(get_settings(), 'timezone', zone)
    at = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    assert filename_stamp(at) == expected
    assert filename_stamp(at, '%Y%m%d') == expected[:8]
    assert cell_value(at).strftime('%Y%m%d_%H%M%S') == expected
