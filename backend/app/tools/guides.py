"""生成资源与指标指南：python -m app.tools.guides > ../docs/资源与指标指南.md（仓库根目录 make guides）。

文档由 services/resource_guides.py 的元数据渲染，tests/test_resource_guides.py 逐字校验；改了说明源后重新生成。
"""
import sys

from app.services.resource_guides import render_resource_guides_markdown


def main() -> int:
    sys.stdout.write(render_resource_guides_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
