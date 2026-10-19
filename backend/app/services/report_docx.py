"""The run report as a Word document (.docx), written with the standard library only.

It holds the same text and tables as the HTML and PDF reports. Charts are not included in
Word (they need an image renderer); the document points to the PDF or HTML report for them.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

from .report import _verdict_text
from .scenario_analyzer import METRIC_LABELS

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""
ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""
DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_BORDER = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="BFC8D0"/>'
STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="120"/></w:pPr><w:rPr><w:b/><w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="280" w:after="100"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="16222E"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>
<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="80" w:type="dxa"/><w:bottom w:w="40" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:tblPr><w:tblBorders>{''.join(_BORDER.format(b) for b in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))}</w:tblBorders></w:tblPr></w:style>
</w:styles>"""
PAGE_WIDTH_TWIPS = 11906 - 2 * 1134        # A4 minus 2 cm margins


def _run(text: str, *, bold: bool = False, color: str | None = None, size: int | None = None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "") + \
        (f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>' if size else "")
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _p(text: str = "", *, style: str | None = None, bold: bool = False, color: str | None = None,
       size: int | None = None, align: str | None = None) -> str:
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + (f'<w:jc w:val="{align}"/>' if align else "")
    return f'<w:p>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{_run(text, bold=bold, color=color, size=size) if text else ""}</w:p>'


def _table(headers: list[str], rows: list[list[Any]], widths: list[float], numeric_from: int = 1) -> str:
    cols = [round(w * PAGE_WIDTH_TWIPS / sum(widths)) for w in widths]
    grid = "".join(f'<w:gridCol w:w="{c}"/>' for c in cols)

    def cell(text: Any, width: int, *, header: bool, right: bool) -> str:
        shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEF1F4"/>' if header else ""
        return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}</w:tcPr>'
                f'{_p(str(text), bold=header, size=18, align="right" if right else None)}</w:tc>')

    head = "<w:tr><w:trPr><w:tblHeader/></w:trPr>" + "".join(
        cell(h, w, header=True, right=False) for h, w in zip(headers, cols)) + "</w:tr>"
    body = "".join("<w:tr><w:trPr><w:cantSplit/></w:trPr>" + "".join(
        cell(c, w, header=False, right=i >= numeric_from) for i, (c, w) in enumerate(zip(row, cols))) + "</w:tr>"
        for row in rows)
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{sum(cols)}" w:type="dxa"/>'
            f'<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{head}{body}</w:tbl>{_p()}')


def render_docx(d: dict[str, Any]) -> bytes:
    body = [_p(d["title"], style="Title")]
    subtitle = d["target"] or " · ".join(f"{g['name']}: {g['users']} × {g.get('script', '')}" for g in d["group_plan"])
    body.append(_p(subtitle, color="5A6B7B"))
    body.append(_p(f"{d['started_at'] or ''} to {d['finished_at'] or ''} · run {d['id']} · generated {d['generated_at']}",
                   color="5A6B7B", size=18))
    body.append(_p(_verdict_text(d), bold=True, size=26, color="1F7A4D" if d["verdict"] == "pass" else "B3261E"))
    r = d["requests"] or {}
    rt = r.get("response_time_ms") or {}
    body.append(_table(["Requests", "Errors", "Requests/s", "p95", "Peak users", "Duration"],
                       [[f"{r.get('total_requests', 0):,}", f"{r.get('error_rate_percent', 0)}%", r.get("throughput_rps", 0),
                         f"{rt.get('p95', '–')} ms", r.get("peak_users", 0), f"{d['duration_seconds']} s"]],
                       [1] * 6, numeric_from=0))
    if d["goal"]:
        g = d["goal"]
        body.append(_p(f"Goal: {g['target']:g}/s, reached {g['actual']:g}/s ({'met' if g['reached'] else 'not met'})."))
    body.append(_p("Charts are in the PDF and HTML versions of this report.", color="5A6B7B", size=18))
    if d["rows"]:
        body.append(_p(f"{d['rows_kind']} summary", style="Heading1"))
        body.append(_table([d["rows_kind"], "Count", "Pass", "Fail", "Min ms", "Avg ms", "Max ms", "90% ms", "95% ms", "Per s"],
                           [[t["display"], f"{t['count']:,}", f"{t['passed']:,}", f"{t['failed']:,}", t["min"], t["avg"],
                             t["max"], t["p90"], t["p95"], t["tps"]] for t in d["rows"]], [3.4] + [1] * 9))
    if d["sla"]:
        body.append(_p("Service level agreements", style="Heading1"))
        body.append(_table(["Business function", "Measure", "Limit", "Actual", "Result"],
                           [[s["transaction"], METRIC_LABELS[s["metric"]], s["limit"], "–" if s["actual"] is None else s["actual"],
                             {"pass": "met", "fail": "MISSED", "no_data": "no data"}[s["status"]]] for s in d["sla"]],
                           [3, 2.2, 1, 1, 1], numeric_from=2))
    if d["groups"]:
        body.append(_p("Groups", style="Heading1"))
        body.append(_table(["Group", "Peak users", "Requests", "Error rate"],
                           [[g["name"], g["peak_users"], f"{g['requests']:,}", f"{g['error_rate_percent']}%"] for g in d["groups"]],
                           [3, 1, 1, 1]))
    if d["errors"]:
        body.append(_p("Errors", style="Heading1"))
        body.append(_table(["Code", "Message", "Count", "First at"],
                           [[x["code"], x["message"], f"{x['count']:,}", f"{x['first_seconds']} s"] for x in d["errors"]],
                           [1.6, 4, 0.8, 0.8], numeric_from=2))
    if d["warnings"]:
        body.append(_p("Findings", style="Heading1"))
        body.extend(_p(f"• {w['message']}") for w in d["warnings"])
    section = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
               'w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="{W_NS}">'
                f'<w:body>{"".join(body)}{section}</w:body></w:document>')
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<dc:title>{escape(d["title"])}</dc:title><dc:creator>Load test console</dc:creator>'
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created></cp:coreProperties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()
