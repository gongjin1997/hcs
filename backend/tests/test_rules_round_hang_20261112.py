"""验证 round() 精度参数不会导致进程挂起（CPython 在巨大负数时会挂死）。"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.rules.expr import ExprError, evaluate


def test_round_with_huge_negative_ndigits_raises_expr_error():
    """round(整数, 巨大负数) 在 CPython 里会挂死，求值器必须拒绝超出范围的精度参数。"""
    with pytest.raises(ExprError, match=r"round 精度参数必须是 -100 到 100 之间的整数"):
        evaluate("round(n, -999)", {"n": 7})

    with pytest.raises(ExprError, match=r"round 精度参数必须是 -100 到 100 之间的整数"):
        evaluate("round(n, ndigits)", {"n": 7, "ndigits": -99999999999})

    with pytest.raises(ExprError, match=r"round 精度参数必须是 -100 到 100 之间的整数"):
        evaluate("round(n, 101)", {"n": 7})


def test_round_within_allowed_range_works():
    """范围内的精度参数正常工作。"""
    assert evaluate("round(n, -2)", {"n": 12345}) == 12300
    assert evaluate("round(n, 2)", {"n": 3.14159}) == 3.14
    assert evaluate("round(n, 0)", {"n": 2.7}) == 3
    assert evaluate("round(n, -100)", {"n": 12345}) == 0  # 边界值


def test_round_does_not_hang_in_subprocess():
    """用子进程验证超范围的 round 调用在 5 秒内返回错误而不是挂起。"""
    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, {!r})
        from app.rules.expr import evaluate, ExprError
        try:
            # 这个调用在修复前会让 CPython 挂死
            result = evaluate("round(n, -99999999999)", {{"n": 7}})
            print(f"UNEXPECTED_SUCCESS: {{result}}")
            sys.exit(1)
        except ExprError as e:
            print(f"EXPECTED_ERROR: {{e}}")
            sys.exit(0)
        except Exception as e:
            print(f"UNEXPECTED_EXCEPTION: {{type(e).__name__}}: {{e}}")
            sys.exit(2)
    """).format(str(Path(__file__).resolve().parents[1]))

    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=5,
                            env={**os.environ, "PYTHONPATH": ""})

    assert result.returncode == 0, f"子进程返回 {result.returncode}:\n{result.stdout}\n{result.stderr}"
    assert "EXPECTED_ERROR" in result.stdout, f"输出不符合预期:\n{result.stdout}"
