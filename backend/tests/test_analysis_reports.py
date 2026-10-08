"""Analysis graphs, error statistics, run comparison, and the HTML / PDF / Word reports."""
import io
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from app.services import analysis
from app.services.report import nice_max
from app.services.report_pdf import Pdf, text_width
from app.services.result_analyzer import Sample
from tests.conftest import wait_for
from tests.test_scenarios_api import imported, simple_har


def run_scenario(client, sid, timeout=60):
    rid = client.post(f"/scenarios/{sid}/run").json()["id"]
    return rid, wait_for(client, rid, timeout=timeout)


def make_scenario(client, target_url, sla_limit=5000):
    simple = imported(client, simple_har(target_url), "Simple")
    cfg = {"name": "Report scenario", "schedule": {"ramp_up_seconds": 1, "duration_seconds": 2},
           "groups": [{"name": "Browsers", "script_id": simple, "users": 2, "settings": {"think_time": {"mode": "ignore"}}}],
           "sla": [{"transaction": "*", "metric": "p90", "limit": sla_limit}]}
    return client.post("/scenarios", json=cfg).json()["id"]


def test_analysis_series_and_errors(client, target_url):
    rid, _ = run_scenario(client, make_scenario(client, target_url))
    a = client.get(f"/tests/{rid}/analysis", params={"bucket_seconds": 1}).json()
    ids = {s["id"] for s in a["series"]}
    assert {"users", "hits", "throughput", "errors", "rt:avg", "rt:p90", "users:Browsers", "code:200"} <= ids
    assert {"tx:01 Fixed:p90", "tx:02 Slow:tps", "req:GET /fixed:avg"} <= ids
    users = next(s for s in a["series"] if s["id"] == "users:Browsers")
    assert users["category"] == "Load" and max(p["v"] for p in users["points"]) == 2
    assert a["errors"] == []
    assert client.get(f"/tests/{rid}/analysis", params={"bucket_seconds": 0}).status_code == 422


def test_error_statistics_group_failures():
    t0 = 1_000_000
    samples = [Sample(ts=t0 + i * 10, elapsed=5, latency=5, code="500", message="Server Error", success=False,
                      failure="", users=1, label="GET /a") for i in range(3)]
    samples += [Sample(ts=t0 + 2000, elapsed=5, latency=5, code="200", message="OK", success=False,
                       failure="Text not found: welcome", users=1, label="GET /b")]
    out = analysis.errors(samples)
    assert [(e["code"], e["count"], e["first_seconds"]) for e in out] == [("500", 3, 0), ("200", 1, 2)]
    assert out[1]["message"] == "Text not found: welcome" and out[0]["requests"] == [{"label": "GET /a", "count": 3}]


def test_comparison_flags_regressions():
    def run(slow_ms, fail=False):
        return [Sample(ts=1000 + i * 100, elapsed=slow_ms, latency=slow_ms, code="200", message="Number of samples in transaction : 1",
                       success=not (fail and i % 4 == 0), failure="", users=2, label="01 Login", transaction=True)
                for i in range(20)] + [Sample(ts=1000 + i * 100, elapsed=10, latency=10, code="200", message="OK",
                                              success=True, failure="", users=2, label="POST /login") for i in range(20)]
    result = analysis.compare(run(200), run(400), bucket_seconds=1)
    [row] = result["rows"]
    assert row["name"] == "Login" and row["p90"] == {"a": 200, "b": 400, "change_percent": 100.0}
    assert row["verdict"] == "slower" and result["regressions"] == 1 and result["kind"] == "transactions"
    assert analysis.compare(run(200), run(205), bucket_seconds=1)["rows"][0]["verdict"] is None
    assert analysis.compare(run(200), run(200, fail=True), bucket_seconds=1)["rows"][0]["verdict"] == "more errors"
    assert set(result["series"]["a"]) == {"users", "hits", "rt:p90", "errors"}


def test_compare_two_runs_through_the_api(client, target_url):
    sid = make_scenario(client, target_url)
    first, _ = run_scenario(client, sid)
    second, _ = run_scenario(client, sid)
    r = client.get(f"/tests/{first}/compare/{second}", params={"bucket_seconds": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["a"]["id"] == first and body["b"]["id"] == second
    assert [row["name"] for row in body["rows"]] == ["Fixed", "Slow"]
    assert client.get(f"/tests/{first}/compare/t_missing").status_code == 404


def _pdf_text(pdf: bytes) -> bytes:
    streams = re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S)
    return b"".join(zlib.decompress(s) for s in streams)


def test_reports_in_html_pdf_and_word(client, target_url):
    rid, _ = run_scenario(client, make_scenario(client, target_url))
    html = client.get(f"/reports/{rid}/summary.html")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert html.text.count("<svg") == 3 and "Business function summary" in html.text and "Service level agreements" in html.text
    assert "<script" not in html.text            # self-contained and printable
    assert "attachment" in client.get(f"/reports/{rid}/summary.html", params={"download": True}).headers["content-disposition"]

    pdf = client.get(f"/reports/{rid}/summary.pdf")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF-1.4")
    body = pdf.content
    xref = int(re.search(rb"startxref\n(\d+)", body).group(1))
    assert body[xref:xref + 4] == b"xref"
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", body)]
    assert all(re.match(rb"\d+ 0 obj", body[o:o + 12]) for o in offsets)     # every xref entry points at its object
    text = _pdf_text(body)
    assert b"Business function summary" in text and b"Fixed" in text and b"Page 1 of" in text

    docx = client.get(f"/reports/{rid}/summary.docx")
    assert docx.headers["content-type"].endswith("wordprocessingml.document")
    with zipfile.ZipFile(io.BytesIO(docx.content)) as z:
        assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                                     "word/_rels/document.xml.rels", "word/styles.xml", "docProps/core.xml"}
        for name in z.namelist():
            ET.fromstring(z.read(name))                                     # every part is well-formed XML
        document = z.read("word/document.xml").decode()
    assert "Business function summary" in document and "Service level agreements" in document
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for tbl in ET.fromstring(document).iter(f"{{{ns['w']}}}tbl"):
        cols = len(tbl.findall("w:tblGrid/w:gridCol", ns))
        assert all(len(tr.findall("w:tc", ns)) == cols for tr in tbl.findall("w:tr", ns))


def test_quick_test_reports_too(client, target_url):
    test_id = client.post("/tests", json={"name": "quick (report)", "target_url": f"{target_url}/fixed",
                                          "users": 2, "duration_seconds": 2}).json()["id"]
    wait_for(client, test_id)
    html = client.get(f"/reports/{test_id}/summary.html").text
    assert "Request summary" in html and "GET /fixed" in html
    assert client.get(f"/reports/{test_id}/summary.pdf").status_code == 200


def test_pdf_helpers():
    assert nice_max(0) == 1 and nice_max(7) == 10 and nice_max(1300) == 2000 and nice_max(0.42) == 0.5
    assert text_width("ii", 10) < text_width("MM", 10)
    pdf = Pdf()
    pdf.add_page()
    pdf.text(10, 10, "a (b) c\\d – ✓")
    stream = pdf.pages[0][0]
    assert b"a \\(b\\) c\\\\d \x96 ?" in stream                           # escaped; en dash in WinAnsi; ✓ not encodable
