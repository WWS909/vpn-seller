"""
A small .xlsx writer — one sheet, no dependencies.

Excel, Google Sheets, Numbers and LibreOffice open it as a real table:
numbers stay numbers (sums work), dates are dates (sorting and filters
work), the header is bold and frozen, and there is a filter on it.

    build("Заказы", [("Заказ", "int", 9), ("Дата", "date", 17), ("Сумма", "money", 11)],
          rows, totals={"Сумма": 1490.0})

Kinds: text, int, money (two decimals), date (a datetime, shown as
dd.mm.yyyy hh:mm). Text cells are never formulas.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from xml.sax.saxutils import escape

_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_EPOCH = dt.datetime(1899, 12, 30)
_STYLE = {"head": 1, "date": 2, "money": 3, "bold_money": 4}

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yyyy hh:mm"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="4" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(ref: str, value, style: int = 0) -> str:
    v = escape(_BAD.sub("", str(value)))[:32000]
    st = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{v}</t></is></c>'


def _cell(ref: str, kind: str, value) -> str:
    if value is None or value == "":
        return ""
    if kind == "date":
        if not isinstance(value, dt.datetime):
            return _text(ref, value)
        serial = (value - _EPOCH).total_seconds() / 86400
        return f'<c r="{ref}" s="{_STYLE["date"]}"><v>{serial:.10f}</v></c>'
    if kind in ("int", "money") and isinstance(value, (int, float)) and not isinstance(value, bool):
        st = f' s="{_STYLE["money"]}"' if kind == "money" else ""
        num = f"{round(float(value), 2):.2f}" if kind == "money" else str(int(value))
        return f'<c r="{ref}"{st}><v>{num}</v></c>'
    return _text(ref, value)


def build(sheet: str, columns: list[tuple[str, str, int]], rows: list[list],
          totals: dict[str, float] | None = None, total_label: str = "") -> bytes:
    n = len(columns)
    last = _col(n - 1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
           'state="frozen"/></sheetView></sheetViews>',
           "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                              for i, (_, _, w) in enumerate(columns)) + "</cols>", "<sheetData>"]
    out.append('<row r="1">' + "".join(_text(f"{_col(i)}1", title, _STYLE["head"])
                                       for i, (title, _, _) in enumerate(columns)) + "</row>")
    r = 1
    for row in rows:
        r += 1
        out.append(f'<row r="{r}">' + "".join(_cell(f"{_col(i)}{r}", columns[i][1], row[i] if i < len(row) else None)
                                             for i in range(n)) + "</row>")
    data_end = r
    if totals:
        r += 2
        cells = [_text(f"A{r}", total_label, _STYLE["head"])] if total_label else []
        for i, (title, _, _) in enumerate(columns):
            if title in totals and i:
                cells.append(f'<c r="{_col(i)}{r}" s="{_STYLE["bold_money"]}"><v>{round(totals[title], 2):.2f}</v></c>')
        out.append(f'<row r="{r}">' + "".join(cells) + "</row>")
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A1:{last}{max(1, data_end)}"/>')
    out.append("</worksheet>")
    name = escape(_BAD.sub("", sheet))[:31] or "Sheet1"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/styles.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets>'
                   f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
                   f"'{name.replace(chr(39), chr(39) * 2)}'!$A$1:${last}${max(1, data_end)}</definedName></definedNames></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", "".join(out))
    return buf.getvalue()
