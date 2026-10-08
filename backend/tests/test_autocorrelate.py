"""Automatic correlation from a replay, and parameterization at import (services/autocorrelate)."""
import uuid

from app.services import autocorrelate as ac


def req(index, path, *, body=None, args=(), headers=()):
    return {"index": index, "method": "POST" if body or args else "GET", "path": path, "body": body,
            "args": list(args), "headers": [list(h) for h in headers]}


def resp(body=None, mime="text/html", headers=(), ok=True, sent=""):
    return {"ok": ok, "status": "200" if ok else "403", "sent": sent, "body": body, "mime": mime,
            "headers": [list(h) for h in headers]}


def test_named_values_reads_forms_meta_json_links_headers_and_cookies():
    html = ("<meta content='m3ta5678tok' name='csrf-token'><input value=\"v1a2l3u4e5\" type=hidden name=\"_token\">"
            "<a href=\"/next?sid=abc123def456&x=1\">next</a><script>var appKey = \"k3y9k3y9k3y9\";</script>")
    pairs = ac.named_values(resp(html, headers=[("Set-Cookie", "XSRF-TOKEN=c00k1e99; Path=/"),
                                                ("Location", "/cb?code=c0de1234&state=s")]))
    assert ("csrf-token", "m3ta5678tok") in pairs and ("_token", "v1a2l3u4e5") in pairs
    assert ("sid", "abc123def456") in pairs and ("appKey", "k3y9k3y9k3y9") in pairs
    assert ("XSRF-TOKEN", "c00k1e99") in pairs and ("code", "c0de1234") in pairs
    json_pairs = ac.named_values(resp('{"data": {"access_token": "eyJ.a1.b2", "id": 42}}', "application/json"))
    assert ("access_token", "eyJ.a1.b2") in json_pairs and ("id", "42") in json_pairs


def test_names_match_across_the_usual_spellings():
    assert ac.name_matches("X-CSRF-Token", "csrf-token") and ac.name_matches("_csrf", "csrfmiddlewaretoken")
    assert ac.name_matches("x_xsrf_token", "XSRF-TOKEN")
    assert ac.name_matches("token", "access_token") and not ac.name_matches("token", "refresh_token")
    assert ac.name_matches("orders_id", "id") and ac.name_matches("orders_id", "order_id")
    assert ac.name_matches("productId", "id") and not ac.name_matches("page", "id")
    assert not ac.name_matches("session", "csrf")


def test_shapes_must_match():
    assert ac.similar("ab12cd34ef56", "99ff00aa11bb") and ac.similar(str(uuid.uuid4()), str(uuid.uuid4()))
    assert ac.similar("12345678", "123456789") and not ac.similar("12345678", "ab12cd34")
    assert not ac.similar(str(uuid.uuid4()), "ab12cd34ef56") and not ac.similar("ab12cd34", "ab12cd34" * 4)


def test_scan_finds_fresh_values_by_name_and_static_ones_by_value():
    requests = [
        req(0, "/form"),
        req(1, "/submit", body="csrf=ab12aaaa1111&item=book", headers=[("Content-Type", "application/x-www-form-urlencoded")]),
        req(2, "/login"),
        req(3, "/account/user9876543210/orders", headers=[("Authorization", "Bearer ab12bbbb2222cccc")]),
    ]
    replay = {
        0: resp('<form><input type="hidden" name="csrf" value="ab12ffff9999"></form>'),
        1: resp('{"ok": true}', "application/json"),
        2: resp('{"token": "ab12eeee8888dddd", "user": "user9876543210"}', "application/json"),
        3: resp('{"orders": []}', "application/json"),
    }
    taken = set()
    found = ac.scan(requests, replay, taken)
    rules = {r["variable"]: r for r in found["correlations"]}
    assert set(rules) == {"csrf", "token", "user"}
    assert rules["csrf"]["source"] == 0 and rules["csrf"]["extractor"] == "boundary" and rules["csrf"]["replace"] == "ab12aaaa1111"
    assert rules["token"]["source"] == 2 and rules["token"]["expression"] == "$.token" and rules["token"]["used_in"] == [3]
    assert rules["user"]["expression"] == "$.user", "the recorded value came back unchanged: found by value"
    assert found["generated"] == [] and found["unknown"] == 0 and taken == {"csrf", "token", "user"}


def test_values_no_response_holds_are_generated_or_unknown():
    request_id, twice = str(uuid.uuid4()), str(uuid.uuid4())
    requests = [req(0, f"/a?requestId={request_id}&ref={twice}"), req(1, f"/b?ref={twice}"), req(2, "/c?ticket=ab12cd34ef56")]
    found = ac.scan(requests, {0: resp("<p>a</p>"), 1: resp("<p>b</p>", ok=False), 2: resp("<p>c</p>")}, set())
    generated = {g["name"]: g for g in found["generated"]}
    assert generated["requestId"]["kind"] == "uuid" and generated["requestId"]["uses"] == 1
    assert generated["ref"]["uses"] == 2
    assert found["unknown"] == 1, "request #1 failed, so where #2's ticket comes from cannot be told yet"
    parameters, replacements, left = ac.generated_parameters(found["generated"], set())
    assert parameters == [{"name": "requestId", "type": "uuid"}]
    assert replacements == [{"find": request_id, "variable": "requestId"}] and [v["name"] for v in left] == ["ref"]


def test_a_recorded_login_becomes_a_data_file():
    form = [("Content-Type", "application/x-www-form-urlencoded")]
    requests = [req(0, "/home"), req(1, "/login", body="username=alice.w&password=S3cret%21pass", headers=form),
                req(2, "/search?q=red+shoes")]
    login = ac.find_login(requests)
    assert login == {"index": 1, "username": "alice.w", "password": "S3cret!pass", "fields": ["username", "password"]}
    data_file, content, replacements = ac.login_data_file(login, set())
    assert data_file["columns"] == ["username", "password"] and content == b"username,password\nalice.w,S3cret!pass\n"
    assert replacements == [{"find": "alice.w", "variable": "username"}, {"find": "S3cret!pass", "variable": "password"}]
    assert ac.search_suggestions(requests, set())[0] | {"first_used": 2} == {
        "name": "q", "preview": "red shoes", "first_used": 2, "kind": "search", "value": "red shoes"}

    elsewhere = requests + [req(3, "/profile/alice.w")]
    assert ac.find_login(elsewhere)["unsafe"], "the user name also appears in a URL, so it is not replaced everywhere"
    assert ac.login_data_file(ac.find_login(elsewhere), set()) is None
    assert ac.login_data_file(login, {"username"}) is None, "a variable of that name exists already"


def test_merge_skips_what_clashes_with_the_design():
    design = {"correlations": [{"variable": "token", "source": 0, "extractor": "json", "expression": "$.token",
                                "replace": "ab12old"}],
              "parameters": [{"name": "city", "type": "list", "values": ["Oslo"]}]}
    merged, added = ac.merge(
        design,
        correlations=[{"variable": "token", "source": 1, "extractor": "json", "expression": "$.t", "replace": "x1y2z3w4"},
                      {"variable": "other", "source": 1, "extractor": "json", "expression": "$.t", "replace": "ab12old"},
                      {"variable": "bad", "source": 1, "extractor": "regex", "expression": "no group", "replace": "q1w2e3r4"},
                      {"variable": "fresh", "source": 1, "extractor": "json", "expression": "$.f", "replace": "f1f2f3f4",
                       "origin": "auto"}],
        parameters=[{"name": "city", "type": "uuid"}, {"name": "rid", "type": "uuid"}],
        replacements=[{"find": "0000-1111", "variable": "rid"}, {"find": "zzz", "variable": "nowhere"}])
    assert added == {"correlations": ["fresh"], "parameters": ["rid"], "data_files": []}
    assert [c["variable"] for c in merged["correlations"]] == ["token", "fresh"]
    assert merged["replacements"] == [{"find": "0000-1111", "variable": "rid"}]
