"""显式选择已核对接口族；不靠失败请求猜版本，也不继承裸公有云 /v3。"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.hcs.gaussdb_api import gaussdb_instances_path


@pytest.mark.parametrize("variant,path", [
    ("v3.3", "/gaussdb/v3.3/{project_id}/instances"),
    ("v3.2", "/gaussdb/v3.2/{project_id}/instances"),
    ("v3.1", "/gaussdb/v3.1/{project_id}/instances"),
    ("opengauss_v3", "/opengauss/v3/{project_id}/instances"),
])
def test_only_explicitly_selected_hcs_interface_is_returned(monkeypatch, variant, path):
    monkeypatch.setattr(get_settings(), "hcs_gaussdb_api_variant", variant)
    assert gaussdb_instances_path() == path


def test_hcs_defaults_to_documented_v33_and_rejects_arbitrary_or_bare_uri(monkeypatch):
    monkeypatch.delenv("INSPECTOR_HCS_GAUSSDB_API_VARIANT", raising=False)
    assert Settings(_env_file=None).hcs_gaussdb_api_variant == "v3.3"
    for value in ("v3", "../v3", "https://example.invalid", "AUTO"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, hcs_gaussdb_api_variant=value)
