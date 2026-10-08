"""API tests for importing, reviewing, grouping and exporting recorded scripts."""
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.recordings import SHOP_HAR, SHOP_JMX


def import_har(client, name=None):
    r = client.post("/scripts/import-har", content=SHOP_HAR, params={"name": name} if name else None)
    assert r.status_code == 201, r.text
    return r.json()


def test_har_import_review_edit_and_export(client):
    script = import_har(client, "Checkout flow")
    assert script["name"] == "Checkout flow" and script["source"] == "har"
    assert script["summary"] == {"captured": 11, "kept": 5, "excluded": 6, "transactions": 3}
    assert [t["name"] for t in script["transactions"]] == ["Home", "Auth Login", "Products Search"]
    assert "headers" not in script["items"][0] and script["items"][5]["has_body"]
    for secret in ("s3cret-token", "s3cret-cookie", "demo-pass"):
        assert secret not in client.get(f"/scripts/{script['id']}").text
    assert [s["id"] for s in client.get("/scripts").json()] == [script["id"]]

    # Re-include a static file, rename one request's business function, then undo the include
    sid = script["id"]
    r = client.put(f"/scripts/{sid}/filter", json={"changes": [
        {"index": 2, "include": True}, {"index": 8, "transaction": "Product details"}]})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["summary"]["kept"] == 6
    assert edited["items"][2]["include_source"] == "manual"
    assert [t["name"] for t in edited["transactions"]][-1] == "Product details"
    edited = client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 2, "include": None}]}).json()
    assert edited["summary"]["kept"] == 5 and edited["items"][2]["include_source"] == "auto"
    assert edited["items"][8]["transaction"] == "Product details"          # other edits are kept

    # New rules re-run the automatic choices without losing manual edits
    rules = {**edited["rules"], "transaction_rules": [{"name": "Login", "url_pattern": "*/auth/*"}]}
    edited = client.put(f"/scripts/{sid}/filter", json={"rules": rules}).json()
    assert [t["name"] for t in edited["transactions"]] == ["Home", "Login", "Products Search", "Product details"]

    r = client.get(f"/scripts/{sid}/export-jmx", params={"users": 5, "loops": 3})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert 'filename="Checkout-flow.jmx"' in r.headers["content-disposition"]
    root = ET.fromstring(r.content)
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == [
        "01 Home", "02 Login", "03 Products Search", "04 Product details"]
    assert root.find(".//stringProp[@name='ThreadGroup.num_threads']").text == "5"


def test_edit_validation_and_export_limits(client):
    sid = import_har(client)["id"]
    assert client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 99, "include": True}]}).status_code == 422
    assert client.put(f"/scripts/{sid}/filter", json={"changes": [{"index": 0, "surprise": 1}]}).status_code == 422
    assert client.get(f"/scripts/{sid}/export-jmx", params={"users": 500}).status_code == 422
    everything_out = [{"index": i, "include": False} for i in range(11)]
    client.put(f"/scripts/{sid}/filter", json={"changes": everything_out})
    r = client.get(f"/scripts/{sid}/export-jmx")
    assert r.status_code == 422 and "every request is excluded" in r.json()["detail"]


def test_jmx_import_and_regrouped_export(client):
    r = client.post("/scripts/import-jmx", content=SHOP_JMX)
    assert r.status_code == 201, r.text
    script = r.json()
    assert script["name"] == "Shop flow" and script["source"] == "jmx"
    assert [t["name"] for t in script["transactions"]] == ["Home page", "Login"]
    root = ET.fromstring(client.get(f"/scripts/{script['id']}/export-jmx").content)
    assert [tc.get("testname") for tc in root.iter("TransactionController")] == ["01 Home page", "02 Login"]
    assert root.find(".//JSONPostProcessor") is not None


def test_bad_uploads_are_rejected(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, max_upload_bytes=1024))) as c:
        assert c.post("/scripts/import-har", content=b"").status_code == 422
        r = c.post("/scripts/import-har", content=b"{not json")
        assert r.status_code == 422 and "JSON" in r.json()["detail"]
        assert c.post("/scripts/import-jmx", content=b"<x/>").status_code == 422
        assert c.post("/scripts/import-har", content=b"x" * 2048).status_code == 413
        assert c.get("/scripts") .json() == []


def test_scripts_need_the_api_key_and_can_be_deleted(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, api_key="k3y"))) as c:
        assert c.post("/scripts/import-har", content=SHOP_HAR).status_code == 401
        assert c.get("/scripts").status_code == 401
        key = {"X-API-Key": "k3y"}
        sid = c.post("/scripts/import-har", content=SHOP_HAR, headers=key).json()["id"]
        assert c.get(f"/scripts/{sid}/export-jmx").status_code == 401
        assert c.delete(f"/scripts/{sid}", headers=key).status_code == 204
        assert c.get(f"/scripts/{sid}", headers=key).status_code == 404
        assert c.delete(f"/scripts/{sid}", headers=key).status_code == 404
