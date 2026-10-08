"""Accounts, roles, projects, sessions and API tokens."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings
from tests.test_scenarios_api import simple_har

CONSOLE = {"X-Requested-With": "console"}
PASSWORD = "correct horse battery"


def setup_admin(client, name="admin"):
    r = client.post("/auth/setup", json={"username": name, "password": PASSWORD}, headers=CONSOLE)
    assert r.status_code == 201, r.text
    return r.json()


def login(client, name, password=PASSWORD):
    return client.post("/auth/login", json={"username": name, "password": password}, headers=CONSOLE)


def add_user(client, name, role, projects=("p_default",)):
    r = client.post("/users", json={"username": name, "password": PASSWORD, "role": role, "projects": list(projects)},
                    headers=CONSOLE)
    assert r.status_code == 201, r.text
    return r.json()


def signed_in(client, name):
    """Another browser: its own cookies, the same running app."""
    other = TestClient(client.app)
    assert login(other, name).status_code == 200
    return other


def import_har(client, raw, project=None):
    params = {"name": "Simple"} | ({"project": project} if project else {})
    return client.post("/scripts/import-har", content=raw, params=params, headers=CONSOLE)


def test_open_until_the_first_account_is_created(client):
    me = client.get("/auth/me").json()
    assert me["authenticated"] and me["mode"] == "open" and me["role"] == "admin" and me["setup_needed"]
    assert client.get("/health").json()["auth_required"] is False
    assert client.post("/auth/setup", json={"username": "admin", "password": PASSWORD}).status_code == 403
    weak = client.post("/auth/setup", json={"username": "admin", "password": "short"}, headers=CONSOLE)
    assert weak.status_code == 422 and "Password" in weak.json()["detail"]
    bad_name = client.post("/auth/setup", json={"username": "a b", "password": PASSWORD}, headers=CONSOLE)
    assert bad_name.status_code == 422 and "User name" in bad_name.json()["detail"]

    me = setup_admin(client, "Admin")
    assert me["mode"] == "session" and me["name"] == "admin" and me["role"] == "admin" and not me["setup_needed"]
    assert client.cookies.get("session")
    assert client.get("/health").json()["auth_required"] is True
    assert client.post("/auth/setup", json={"username": "x2", "password": PASSWORD}, headers=CONSOLE).status_code == 409

    stranger = TestClient(client.app)
    assert stranger.get("/scripts").status_code == 401
    anonymous = stranger.get("/auth/me").json()
    assert not anonymous["authenticated"] and not anonymous["setup_needed"] and anonymous["projects"] == []
    assert stranger.post("/auth/setup", json={"username": "x3", "password": PASSWORD}, headers=CONSOLE).status_code == 401


def test_sign_in_and_out_and_session_writes_need_the_console_header(client, target_url):
    setup_admin(client)
    assert client.post("/auth/logout", headers=CONSOLE).status_code == 204
    assert client.get("/scripts").status_code == 401

    assert login(client, "admin", "wrong password!").status_code == 401
    unknown = login(client, "nobody")
    assert unknown.status_code == 401 and unknown.json()["detail"] == "Wrong user name or password"
    assert client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
    assert login(client, "ADMIN").status_code == 200, "user names are not case-sensitive"

    raw = simple_har(target_url)
    forged = client.post("/scripts/import-har", content=raw, params={"name": "x"})
    assert forged.status_code == 403 and "X-Requested-With" in forged.json()["detail"]
    assert import_har(client, raw).status_code == 201
    assert client.get("/scripts").status_code == 200, "reads need no extra header"


def test_failed_sign_ins_are_throttled(client):
    setup_admin(client)
    browser = TestClient(client.app)
    for _ in range(5):
        assert login(browser, "admin", "not the password").status_code == 401
    blocked = login(browser, "admin")
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert login(browser, "someone-else").status_code == 429, "the address is blocked too"


def test_roles_decide_what_each_user_may_do(client, target_url):
    setup_admin(client)
    add_user(client, "vera", "viewer")
    add_user(client, "tom", "tester")
    viewer, tester = signed_in(client, "vera"), signed_in(client, "tom")
    raw = simple_har(target_url)
    generator = {"name": "lg", "host": "127.0.0.1", "port": 1099}

    assert viewer.get("/scripts").status_code == 200
    denied = import_har(viewer, raw)
    assert denied.status_code == 403 and "tester" in denied.json()["detail"]
    assert viewer.get("/users").status_code == 403

    script = import_har(tester, raw)
    assert script.status_code == 201
    assert tester.post("/generators", json=generator, headers=CONSOLE).status_code == 403
    assert tester.post("/users", json={"username": "eve", "password": PASSWORD, "role": "admin"},
                       headers=CONSOLE).status_code == 403
    scenario = {"name": "S", "groups": [{"name": "G", "script_id": script.json()["id"]}]}
    sid = tester.post("/scenarios", json=scenario, headers=CONSOLE).json()["id"]
    assert viewer.put(f"/scenarios/{sid}/baseline", json={"run_id": None}, headers=CONSOLE).status_code == 403
    assert viewer.post(f"/scenarios/{sid}/run", headers=CONSOLE).status_code == 403

    gid = client.post("/generators", json=generator, headers=CONSOLE)
    assert gid.status_code == 201
    assert tester.post(f"/generators/{gid.json()['id']}/check", headers=CONSOLE).status_code == 200
    assert viewer.post(f"/generators/{gid.json()['id']}/check", headers=CONSOLE).status_code == 403


def test_projects_keep_teams_apart(client, target_url):
    setup_admin(client)
    team_b = client.post("/projects", json={"name": "Team B"}, headers=CONSOLE).json()["id"]
    assert client.post("/projects", json={"name": "Team B"}, headers=CONSOLE).status_code == 409
    add_user(client, "tom", "tester")                       # Default only
    add_user(client, "bob", "tester", projects=[team_b])    # Team B only
    tom, bob = signed_in(client, "tom"), signed_in(client, "bob")
    raw = simple_har(target_url)

    in_default = import_har(client, raw).json()
    in_b = import_har(bob, raw).json()
    assert in_default["project_id"] == "p_default" and in_b["project_id"] == team_b, "bob's only project is the default"
    assert [s["id"] for s in tom.get("/scripts").json()] == [in_default["id"]]
    assert [s["id"] for s in bob.get("/scripts").json()] == [in_b["id"]]
    assert tom.get(f"/scripts/{in_b['id']}").status_code == 404
    assert import_har(tom, raw, project=team_b).status_code == 404
    assert {s["id"] for s in client.get("/scripts").json()} == {in_default["id"], in_b["id"]}
    assert [s["id"] for s in client.get("/scripts", params={"project": team_b}).json()] == [in_b["id"]]

    cross = tom.post("/scenarios", json={"name": "X", "groups": [{"name": "G", "script_id": in_b["id"]}]}, headers=CONSOLE)
    assert cross.status_code == 422 and "does not exist in this project" in cross.json()["detail"]
    sid = bob.post("/scenarios", json={"name": "B", "groups": [{"name": "G", "script_id": in_b["id"]}]},
                   headers=CONSOLE).json()["id"]
    assert client.get(f"/scenarios/{sid}").json()["project_id"] == team_b
    assert tom.get(f"/scenarios/{sid}").status_code == 404 and tom.get("/scenarios").json() == []

    quick = {"name": "Q", "target_url": f"{target_url}/fixed", "users": 1, "duration_seconds": 1}
    run = tom.post("/tests", json=quick, headers=CONSOLE).json()["id"]
    assert bob.get(f"/tests/{run}").status_code == 404 and bob.get("/tests").json() == []
    assert bob.get("/auth/me").json()["projects"] == [{"id": team_b, "name": "Team B"}]
    assert [p["name"] for p in tom.get("/projects").json()] == ["Default"]

    assert client.delete(f"/projects/{team_b}", headers=CONSOLE).status_code == 409, "it still holds work"
    assert client.delete("/projects/p_default", headers=CONSOLE).status_code == 409


def test_api_tokens_passwords_and_disabled_accounts(client, target_url):
    setup_admin(client)
    tom_id = add_user(client, "tom", "tester")["id"]
    tom, laptop = signed_in(client, "tom"), signed_in(client, "tom")
    created = tom.post("/auth/tokens", json={"name": "ci"}, headers=CONSOLE)
    assert created.status_code == 201 and created.json()["token"].startswith("pt_")
    token = {"X-API-Key": created.json()["token"]}
    listed = tom.get("/auth/tokens").json()
    assert [t["name"] for t in listed] == ["ci"] and "token" not in listed[0] and "token_hash" not in listed[0]

    pipeline = TestClient(client.app)
    me = pipeline.get("/auth/me", headers=token).json()
    assert me["mode"] == "token" and me["name"] == "tom" and me["role"] == "tester"
    assert pipeline.post("/scripts/import-har", content=simple_har(target_url), params={"name": "ci"},
                         headers=token).status_code == 201, "tokens need no console header"
    assert pipeline.post("/auth/tokens", json={"name": "more"}, headers=token).status_code == 400
    assert pipeline.get("/scripts", headers={"X-API-Key": "pt_" + "0" * 64}).status_code == 401

    change = {"current_password": "not it at all", "new_password": "a brand new password"}
    assert tom.post("/auth/password", json=change, headers=CONSOLE).status_code == 403
    change["current_password"] = PASSWORD
    assert tom.post("/auth/password", json=change | {"new_password": "short"}, headers=CONSOLE).status_code == 422
    assert tom.post("/auth/password", json=change, headers=CONSOLE).status_code == 204
    assert tom.get("/auth/me").json()["name"] == "tom", "this browser stays signed in"
    assert laptop.get("/scripts").status_code == 401, "other sessions are signed out"
    assert login(TestClient(client.app), "tom", "a brand new password").status_code == 200
    assert pipeline.get("/scripts", headers=token).status_code == 200, "tokens survive a password change"

    assert client.put(f"/users/{tom_id}", json={"disabled": True}, headers=CONSOLE).status_code == 200
    assert tom.get("/scripts").status_code == 401 and pipeline.get("/scripts", headers=token).status_code == 401
    disabled = login(TestClient(client.app), "tom", "a brand new password")
    assert disabled.status_code == 403 and "disabled" in disabled.json()["detail"]
    client.put(f"/users/{tom_id}", json={"disabled": False}, headers=CONSOLE)
    assert tom.get("/auth/tokens").status_code == 401, "disabling ended tom's sessions for good"
    assert pipeline.get("/scripts", headers=token).status_code == 200, "the token works again"
    assert pipeline.delete(f"/auth/tokens/{listed[0]['id']}", headers=token).status_code == 204
    assert pipeline.get("/scripts", headers=token).status_code == 401


def test_the_last_administrator_cannot_be_removed(client):
    admin_id = setup_admin(client)["user_id"]
    assert client.put(f"/users/{admin_id}", json={"role": "tester"}, headers=CONSOLE).status_code == 409
    assert client.put(f"/users/{admin_id}", json={"disabled": True}, headers=CONSOLE).status_code == 409
    assert client.delete(f"/users/{admin_id}", headers=CONSOLE).status_code == 409
    assert client.post("/users", json={"username": "admin", "password": PASSWORD}, headers=CONSOLE).status_code == 409

    ann = add_user(client, "ann", "admin")
    assert ann["projects"] == ["p_default"]
    assert client.put(f"/users/{admin_id}", json={"role": "viewer"}, headers=CONSOLE).status_code == 200
    assert client.get("/users").status_code == 403, "a role change applies to the next request"
    ann_browser = signed_in(client, "ann")
    assert ann_browser.delete(f"/users/{admin_id}", headers=CONSOLE).status_code == 204
    assert [u["username"] for u in ann_browser.get("/users").json()] == ["ann"]


def test_the_api_key_still_works(tmp_path):
    settings = make_settings(tmp_path, api_key="a-long-random-api-key")
    with TestClient(create_app(settings)) as c:
        key = {"X-API-Key": "a-long-random-api-key"}
        assert c.get("/scripts").status_code == 401
        assert c.get("/scripts", headers=key).status_code == 200
        me = c.get("/auth/me", headers=key).json()
        assert me["mode"] == "api_key" and me["role"] == "admin" and me["setup_needed"]
        assert c.get("/auth/me").json()["authenticated"] is False
        assert c.post("/auth/setup", json={"username": "admin", "password": PASSWORD}, headers=key).status_code == 201
        assert c.get("/scripts", headers=key).status_code == 200
