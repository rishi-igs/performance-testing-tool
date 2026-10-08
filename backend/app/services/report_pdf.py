"""The run report as a PDF, written with the standard library only.

Text uses the PDF standard fonts (Helvetica), so nothing is embedded; charts are vector paths.
Characters outside Windows-1252 are replaced with '?'.
"""
from __future__ import annotations

import zlib
from typing import Any

from .report import COLORS, _verdict_text, chart_geometry, fmt_number
from .scenario_analyzer import METRIC_LABELS

W, H, MARGIN = 595.0, 842.0, 40.0             # A4 in points
CONTENT_W = W - 2 * MARGIN

# Helvetica advance widths (per 1000 em) for ASCII 32..126, from the standard font metrics.
_WIDTHS = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556,
           556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
           722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
           278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
           556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]


def text_width(text: str, size: float, bold: bool = False) -> float:
    w = sum(_WIDTHS[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return w * size / 1000 * (1.05 if bold else 1.0)


def _rgb(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return " ".join(f"{int(h[i:i + 2], 16) / 255:.3f}" for i in (0, 2, 4))


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Pdf:
    def __init__(self) -> None:
        self.pages: list[list[bytes]] = []

    def add_page(self) -> None:
        self.pages.append([])

    def _op(self, op: str | bytes) -> None:
        self.pages[-1].append(op.encode("latin-1") if isinstance(op, str) else op)

    def text(self, x: float, y: float, text: str, size: float = 10, bold: bool = False, color: str = "#16222e") -> None:
        self._op(f"BT /{'F2' if bold else 'F1'} {size:g} Tf {_rgb(color)} rg {x:.2f} {H - y:.2f} Td (".encode() +
                 _escape(text) + b") Tj ET")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = "#d3dae1", width: float = 0.6) -> None:
        self._op(f"{_rgb(color)} RG {width:g} w {x1:.2f} {H - y1:.2f} m {x2:.2f} {H - y2:.2f} l S")

    def polyline(self, points: list[tuple[float, float]], color: str, width: float = 1.5) -> None:
        if len(points) < 2:
            return
        path = " ".join(f"{x:.2f} {H - y:.2f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(points))
        self._op(f"{_rgb(color)} RG {width:g} w 1 j {path} S")

    def rect(self, x: float, y: float, w: float, h: float, fill: str) -> None:
        self._op(f"{_rgb(fill)} rg {x:.2f} {H - y - h:.2f} {w:.2f} {h:.2f} re f")

    def output(self, title: str) -> bytes:
        objects: list[bytes] = []
        font_ids = (3, 4)
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{5 + 2 * i} 0 R" for i in range(len(self.pages)))
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode())
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        for n, ops in enumerate(self.pages):
            footer = (f"BT /F1 8 Tf {_rgb('#5a6b7b')} rg {MARGIN:.2f} 22 Td (".encode() + _escape(title[:80]) +
                      f") Tj ET BT /F1 8 Tf {W - MARGIN - 50:.2f} 22 Td (Page {n + 1} of {len(self.pages)}) Tj ET".encode())
            stream = zlib.compress(b"\n".join([*ops, footer]))
            objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:g} {H:g}] /Resources << /Font << "
                           f"/F1 {font_ids[0]} 0 R /F2 {font_ids[1]} 0 R >> >> /Contents {6 + 2 * n} 0 R >>".encode())
            objects.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info << /Title (".encode() + _escape(title) + \
            f") /Producer (Load test console) >> >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


class Layout:
    """Top-down flow layout: headings, paragraphs, tables and charts with page breaks."""

    def __init__(self) -> None:
        self.pdf = Pdf()
        self.pdf.add_page()
        self.y = MARGIN

    def ensure(self, height: float) -> None:
        if self.y + height > H - MARGIN - 14:
            self.pdf.add_page()
            self.y = MARGIN

    def heading(self, text: str, size: float = 14) -> None:
        self.ensure(size + 40)
        self.y += size + 10
        self.pdf.text(MARGIN, self.y, text, size=size, bold=True)
        self.y += 6

    def paragraph(self, text: str, size: float = 10, color: str = "#16222e", bold: bool = False) -> None:
        words, line = text.split(), ""
        lines = []
        for word in words:
            candidate = f"{line} {word}".strip()
            if text_width(candidate, size, bold) > CONTENT_W and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        for ln in lines:
            self.ensure(size + 4)
            self.y += size + 3
            self.pdf.text(MARGIN, self.y, ln, size=size, bold=bold, color=color)
        self.y += 3

    def stats(self, pairs: list[tuple[str, str]]) -> None:
        self.ensure(46)
        self.pdf.line(MARGIN, self.y + 4, W - MARGIN, self.y + 4)
        col = CONTENT_W / len(pairs)
        for i, (label, value) in enumerate(pairs):
            x = MARGIN + i * col
            self.pdf.text(x, self.y + 24, value, size=14, bold=True)
            self.pdf.text(x, self.y + 37, label, size=8, color="#5a6b7b")
        self.y += 44
        self.pdf.line(MARGIN, self.y, W - MARGIN, self.y)
        self.y += 6

    def _fit(self, text: str, width: float, size: float, bold: bool = False) -> str:
        if text_width(text, size, bold) <= width:
            return text
        while text and text_width(text + "…", size, bold) > width:
            text = text[:-1]
        return text + "…"

    def table(self, headers: list[str], rows: list[list[Any]], widths: list[float], numeric_from: int = 1) -> None:
        size = 8
        cols = [w * CONTENT_W / sum(widths) for w in widths]

        def draw_row(cells: list[Any], bold: bool, color: str) -> None:
            x = MARGIN
            for i, (cell, w) in enumerate(zip(cells, cols)):
                text = self._fit(str(cell), w - 6, size, bold)
                tx = x + w - 3 - text_width(text, size, bold) if i >= numeric_from and not bold else x + 3
                self.pdf.text(tx, self.y + 11, text, size=size, bold=bold, color=color)
                x += w
            self.y += 15
            self.pdf.line(MARGIN, self.y, W - MARGIN, self.y, width=0.4)

        self.ensure(40)
        draw_row(headers, True, "#5a6b7b")
        for row in rows:
            if self.y + 15 > H - MARGIN - 14:
                self.pdf.add_page()
                self.y = MARGIN
                draw_row(headers, True, "#5a6b7b")
            draw_row(row, False, "#16222e")
        self.y += 4

    def chart(self, chart: dict[str, Any]) -> None:
        width, height = CONTENT_W, 190.0
        legend_rows = (len(chart["lines"]) + 2) // 3
        self.ensure(height + 16 * legend_rows + 30)
        top = self.y
        g = chart_geometry(chart["lines"], width, height, left=44)
        ox = MARGIN
        for i in range(6):
            v = g["y_max"] * i / 5
            _, y = g["at"](0, v)
            self.pdf.line(ox + g["left"], top + y, ox + g["left"] + g["plot_w"], top + y)
            label = fmt_number(v)
            self.pdf.text(ox + g["left"] - 4 - text_width(label, 7), top + y + 2.5, label, size=7, color="#5a6b7b")
        for i in range(7):
            t = g["x_max"] * i / 6
            x, _ = g["at"](t, 0)
            label = f"{fmt_number(round(t))}s"
            self.pdf.text(ox + x - text_width(label, 7) / 2, top + g["top"] + g["plot_h"] + 12, label, size=7, color="#5a6b7b")
        for n, line in enumerate(chart["lines"]):
            color = COLORS[n % len(COLORS)]
            self.pdf.polyline([(ox + x, top + y) for x, y in (g["at"](p["t"], p["v"] or 0) for p in line["points"])], color)
            lx = ox + (n % 3) * (width / 3)
            ly = top + height + 6 + (n // 3) * 14
            self.pdf.rect(lx, ly, 10, 3, color)
            self.pdf.text(lx + 14, ly + 4, self._fit(line["label"], width / 3 - 20, 7.5), size=7.5)
        self.y = top + height + 14 * legend_rows + 10


def render_pdf(d: dict[str, Any]) -> bytes:
    lay = Layout()
    lay.y += 8
    lay.pdf.text(MARGIN, lay.y + 14, d["title"], size=20, bold=True)
    lay.y += 26
    subtitle = d["target"] or " · ".join(f"{g['name']}: {g['users']} × {g.get('script', '')}" for g in d["group_plan"])
    lay.paragraph(subtitle, size=9, color="#5a6b7b")
    lay.paragraph(f"{d['started_at'] or ''} to {d['finished_at'] or ''} · run {d['id']} · generated {d['generated_at']}",
                  size=8, color="#5a6b7b")
    lay.paragraph(_verdict_text(d), size=13, bold=True, color="#1f7a4d" if d["verdict"] == "pass" else "#b3261e")
    r = d["requests"] or {}
    rt = r.get("response_time_ms") or {}
    lay.stats([("Requests", f"{r.get('total_requests', 0):,}"), ("Errors", f"{r.get('error_rate_percent', 0)}%"),
               ("Requests/s", str(r.get("throughput_rps", 0))), ("p95", f"{rt.get('p95', '–')} ms"),
               ("Peak users", str(r.get("peak_users", 0))), ("Duration", f"{d['duration_seconds']} s")])
    if d["goal"]:
        g = d["goal"]
        lay.paragraph(f"Goal: {g['target']:g}/s, reached {g['actual']:g}/s ({'met' if g['reached'] else 'not met'}).")
    for chart in d["charts"]:
        lay.heading(chart["title"], 12)
        lay.chart(chart)
    if d["rows"]:
        lay.heading(f"{d['rows_kind']} summary", 12)
        lay.table([d["rows_kind"], "Count", "Pass", "Fail", "Min", "Avg", "Max", "90%", "95%", "/s"],
                  [[t["display"], f"{t['count']:,}", f"{t['passed']:,}", f"{t['failed']:,}", t["min"], t["avg"], t["max"],
                    t["p90"], t["p95"], t["tps"]] for t in d["rows"]], [4, 1.1, 1, 1, 1, 1.1, 1.1, 1, 1, 1])
    if d["sla"]:
        lay.heading("Service level agreements", 12)
        lay.table(["Business function", "Measure", "Limit", "Actual", "Result"],
                  [[s["transaction"], METRIC_LABELS[s["metric"]], s["limit"], "–" if s["actual"] is None else s["actual"],
                    {"pass": "met", "fail": "MISSED", "no_data": "no data"}[s["status"]]] for s in d["sla"]],
                  [3, 2.2, 1, 1, 1], numeric_from=2)
    if d["groups"]:
        lay.heading("Groups", 12)
        lay.table(["Group", "Peak users", "Requests", "Error rate"],
                  [[g["name"], g["peak_users"], f"{g['requests']:,}", f"{g['error_rate_percent']}%"] for g in d["groups"]],
                  [3, 1, 1, 1])
    if d["errors"]:
        lay.heading("Errors", 12)
        lay.table(["Code", "Message", "Count", "First at"],
                  [[x["code"], x["message"], f"{x['count']:,}", f"{x['first_seconds']} s"] for x in d["errors"]],
                  [1.6, 4, 0.8, 0.8], numeric_from=2)
    if d["warnings"]:
        lay.heading("Findings", 12)
        for w in d["warnings"]:
            lay.paragraph(f"• {w['message']}", size=9)
    return lay.pdf.output(d["title"])
