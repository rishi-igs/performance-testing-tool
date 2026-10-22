"""Local target application for testing the tool (requirements section 15).

Run:  uvicorn tests.sample_app:app --port 9000      (from the backend/ folder)

Endpoints
  GET  /fixed               fixed JSON response
  GET  /slow?ms=300         deliberately slow response
  GET  /error?rate=0.2      returns HTTP 500 for that fraction of requests
  GET  /status/{code}       returns exactly that status code
  POST /echo                echoes the request body
  GET  /soft-error          HTTP 200 with an error in the body (for content checks)

Dynamic values that a replay must correlate (each is issued fresh and checked):
  POST /login               returns {"token": ...}
  GET  /private             needs that token in "Authorization: Bearer ..."
  GET  /page/form           HTML form with a hidden one-time CSRF token
  POST /page/submit         form post that needs csrf=<token>
  GET  /api/orders/new      returns {"order": {"id": <uuid>}}
  GET  /api/orders/{id}     404 unless the id was issued
  GET  /redirect            302 to /callback?code=<one-time code>
  GET  /callback?code=      400 unless the code was issued
  GET  /xsrf                sets the XSRF-TOKEN cookie
  POST /xsrf/submit         needs the X-XSRF-TOKEN header to equal that cookie
  POST /reset               forgets every issued value (tests use it between recording and replay)
  GET  /prom-metrics        Prometheus text shaped like node_exporter (for monitor tests)

For recording in a real browser (the Phase 3 acceptance check):
  GET  /demo                a small shop page: sign in, then place an order. Its script calls the
                            endpoints above, so the recording holds a login token, a CSRF token,
                            a new order id and an XSRF cookie, as a real application would.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time
import uuid
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

app = FastAPI(title="Sample target")
_tokens: set[str] = set()
_csrf: set[str] = set()
_orders: set[str] = set()
_codes: set[str] = set()
_xsrf: set[str] = set()


def _token() -> str:
    """A random value with letters and digits, like the session values real applications issue."""
    return f"ab12{secrets.token_hex(10)}"


@app.get("/fixed")
def fixed():
    return {"message": "hello", "items": [1, 2, 3]}


@app.get("/slow")
async def slow(ms: int = 300):
    await asyncio.sleep(min(max(ms, 0), 10_000) / 1000)
    return {"slept_ms": ms}


@app.get("/error")
def error(rate: float = 0.2):
    if random.random() < rate:
        raise HTTPException(500, "controlled failure")
    return {"ok": True}


@app.get("/status/{code}")
def status(code: int):
    return Response(status_code=code, content=f"status {code}")


@app.post("/echo")
async def echo(request: Request):
    return Response(content=await request.body(), media_type=request.headers.get("content-type", "text/plain"))


@app.get("/soft-error")
def soft_error():
    return {"status": "error", "message": "something failed"}


@app.post("/login")
def login():
    token = _token()
    _tokens.add(token)
    return {"token": token}


@app.get("/private")
def private(authorization: str | None = Header(default=None)):
    if not authorization or authorization.removeprefix("Bearer ") not in _tokens:
        raise HTTPException(401, "invalid token")
    return {"secret": "data"}


@app.get("/page/form", response_class=HTMLResponse)
def form_page():
    token = _token()
    _csrf.add(token)
    return (f"<html><head><title>Order form</title><meta name='csrf-token' content='{token}'></head>"
            f"<body><form method='post' action='/page/submit'><input type=\"hidden\" name=\"csrf\" "
            f"value=\"{token}\"><input name='item'></form></body></html>")


@app.post("/page/submit")
async def form_submit(request: Request):
    form = parse_qs((await request.body()).decode())
    token = (form.get("csrf") or [""])[0]
    if token not in _csrf:
        raise HTTPException(403, "csrf token invalid")
    _csrf.discard(token)                  # one-time token
    return {"ok": True, "item": (form.get("item") or [""])[0]}


@app.get("/api/orders/new")
def new_order():
    order_id = str(uuid.uuid4())
    _orders.add(order_id)
    return {"order": {"id": order_id, "status": "new"}}


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    if order_id not in _orders:
        raise HTTPException(404, "no such order")
    return {"id": order_id, "status": "new"}


@app.get("/redirect")
def redirect():
    code = _token()
    _codes.add(code)
    return RedirectResponse(f"/callback?code={code}&state=x1", status_code=302)


@app.get("/callback")
def callback(code: str = ""):
    if code not in _codes:
        raise HTTPException(400, "invalid code")
    _codes.discard(code)
    return {"ok": True}


@app.get("/xsrf")
def xsrf(response: Response):
    token = _token()
    _xsrf.add(token)
    response.set_cookie("XSRF-TOKEN", token)
    return {"ok": True}


@app.post("/xsrf/submit")
def xsrf_submit(request: Request):
    header = request.headers.get("x-xsrf-token", "")
    if not header or header != request.cookies.get("XSRF-TOKEN") or header not in _xsrf:
        raise HTTPException(403, "xsrf check failed")
    return {"ok": True}


_DEMO = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo shop</title><meta name="csrf-token" content="{csrf}">
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; background: #f4f6f8; color: #16222e; }}
  main {{ max-width: 420px; margin: 8vh auto; background: #fff; border: 1px solid #d3dae1; border-radius: 10px; padding: 24px; }}
  label {{ display: grid; gap: 4px; margin: 0 0 12px; font-size: 14px; }}
  input, select {{ font: inherit; padding: 8px; border: 1px solid #c4ccd4; border-radius: 6px; }}
  button {{ font: inherit; font-weight: 600; padding: 9px 14px; border: 0; border-radius: 6px; background: #0f6e8c; color: #fff; cursor: pointer; }}
  [hidden] {{ display: none; }} #log {{ font-size: 14px; color: #5a6b7b; }}
</style></head>
<body><main>
  <h1>Demo shop</h1>
  <form id="signin"><label>User name <input name="username" value="demo.user" autocomplete="username"></label>
    <label>Password (any) <input name="password" type="password" required autocomplete="current-password"></label>
    <button>Sign in</button></form>
  <form id="order" hidden><label>Item <select name="item"><option>book</option><option>lamp</option></select></label>
    <button>Place order</button></form>
  <p id="log" role="status"></p>
</main>
<script>
  const log = (t) => {{ document.getElementById("log").textContent = t; }};
  let token = null;
  document.getElementById("signin").addEventListener("submit", async (e) => {{
    e.preventDefault();
    const f = e.target.elements;
    const r = await fetch("/login", {{ method: "POST", headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ username: f.username.value, password: f.password.value }}) }});
    token = (await r.json()).token;
    const me = await fetch("/private", {{ headers: {{ Authorization: "Bearer " + token }} }});
    if (!me.ok) {{ log("Sign-in failed"); return; }}
    await fetch("/xsrf");
    document.getElementById("signin").hidden = true;
    document.getElementById("order").hidden = false;
    log("Signed in as " + f.username.value);
  }});
  document.getElementById("order").addEventListener("submit", async (e) => {{
    e.preventDefault();
    const item = e.target.elements.item.value;
    const created = await (await fetch("/api/orders/new", {{ headers: {{ Authorization: "Bearer " + token }} }})).json();
    await fetch("/api/orders/" + created.order.id, {{ headers: {{ Authorization: "Bearer " + token }} }});
    const csrf = document.querySelector("meta[name=csrf-token]").content;
    await fetch("/page/submit", {{ method: "POST", headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
      body: "csrf=" + encodeURIComponent(csrf) + "&item=" + encodeURIComponent(item) }});
    const xsrf = (document.cookie.match(/XSRF-TOKEN=([^;]+)/) || [])[1] || "";
    const done = await fetch("/xsrf/submit", {{ method: "POST", headers: {{ "Content-Type": "application/json", "X-XSRF-TOKEN": xsrf }},
      body: JSON.stringify({{ order: created.order.id }}) }});
    log(done.ok ? "Order " + created.order.id + " placed" : "The order failed");
  }});
</script></body></html>"""


@app.get("/demo", response_class=HTMLResponse)
def demo():
    """The page to record in a browser: sign in, then place an order (see the module docstring)."""
    token = _token()
    _csrf.add(token)
    return _DEMO.format(csrf=token)


@app.post("/reset")
def reset():
    for issued in (_tokens, _csrf, _orders, _codes, _xsrf):
        issued.clear()
    return {"ok": True}


_started = time.time()


@app.get("/prom-metrics", response_class=PlainTextResponse)
def prom_metrics():
    """Prometheus text format, shaped like node_exporter: 2 CPUs 75% idle (25% busy), 75% memory used."""
    up = time.time() - _started
    lines = ["# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.",
             "# TYPE node_cpu_seconds_total counter"]
    for cpu in ("0", "1"):
        lines.append(f'node_cpu_seconds_total{{cpu="{cpu}",mode="idle"}} {100 + up * 0.75:.4f}')
        lines.append(f'node_cpu_seconds_total{{cpu="{cpu}",mode="user"}} {50 + up * 0.25:.4f}')
    lines += ["node_memory_MemTotal_bytes 8e+09", "node_memory_MemAvailable_bytes 2e+09", "node_load1 1.5",
              'app_requests_total{path="/a",note="say \\"hi\\""} 40', 'app_requests_total{path="/b"} 2']
    return "\n".join(lines) + "\n"
