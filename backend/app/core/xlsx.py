"""Excel 导出小工具（openpyxl）：统一表头样式、列宽、冻结首行、自动筛选。"""
import io
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .config import get_settings

HEADER_FILL = PatternFill("solid", fgColor="1D63F0")
HEADER_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=14)
THIN = Side(style="thin", color="E5E6EB")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def filename_stamp(at: datetime, pattern: str = "%Y%m%d_%H%M%S") -> str:
    """文件名与工作簿使用同一系统时区，不依赖宿主机 TZ。"""
    return at.astimezone(ZoneInfo(get_settings().timezone)).strftime(pattern)


def cell_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(ZoneInfo(get_settings().timezone)).replace(tzinfo=None)
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, list | dict):
        return str(v)
    if isinstance(v, str):
        return ILLEGAL_CHARACTERS_RE.sub("", v)[:32767]
    return v


def write_table(ws: Worksheet, headers: Sequence[str], rows: Iterable[Sequence[Any]], widths: Sequence[int] | None = None,
                start_row: int = 1) -> None:
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=i, value=h)
        c.fill, c.font, c.alignment = HEADER_FILL, HEADER_FONT, Alignment(vertical="center")
    n = start_row
    for n, row in enumerate(rows, start_row + 1):
        for i, v in enumerate(row, 1):
            c = ws.cell(row=n, column=i, value=cell_value(v))
            if isinstance(c.value, str):
                c.data_type = "s"
            c.border = Border(bottom=THIN)
            if isinstance(c.value, datetime):
                c.number_format = "yyyy-mm-dd hh:mm"
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths[i - 1] if widths and i <= len(widths)
                                                            else max(10, len(str(h)) * 2 + 4))
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if n > start_row:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{n}"


def new_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def safe_title(name: str) -> str:
    for ch in '[]:*?/\\':
        name = name.replace(ch, " ")
    return name[:31]


def to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xlsx_response(content: bytes, filename: str) -> Response:
    return Response(content, media_type=XLSX_MIME,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})
