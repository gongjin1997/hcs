"""GaussDB 实例接口族；版本明确配置，不逐请求试探不同 URI。

来源：用户上传 gaussdb-api.pdf，GaussDB 25.1.30，2025-03-30，
§4.3.4、§5.2.3、§5.2.4、§5.2.6。这些接口的数据库类型为 GaussDB，不能当作
GaussDB(for MySQL) 的契约。Endpoint 为现场服务地址，不能据公有云 API 删掉前缀。
"""
from app.core.config import get_settings

PATHS = {
    "v3.3": "/gaussdb/v3.3/{project_id}/instances",
    "v3.2": "/gaussdb/v3.2/{project_id}/instances",
    "v3.1": "/gaussdb/v3.1/{project_id}/instances",
    "opengauss_v3": "/opengauss/v3/{project_id}/instances",
}


def gaussdb_instances_path() -> str:
    return PATHS[get_settings().hcs_gaussdb_api_variant]
