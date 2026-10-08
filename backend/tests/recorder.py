"""Record real requests against the sample app into a HAR, the way a browser's DevTools would."""
from __future__ import annotations

import http.client
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit


class Recorder:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/") + "/"
        self.cookies: dict[str, str] = {}
        self.entries: list[dict] = []
        self.clock = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: str | None = None,
                content_type: str | None = None, pause_ms: int = 100) -> tuple[int, dict[str, str], str]:
        url = urljoin(self.base, path.lstrip("/"))
        parts = urlsplit(url)
        sent = {"User-Agent": "test-recorder", "Accept": "*/*", **(headers or {})}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None and content_type:
            sent["Content-Type"] = content_type
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        started = time.time()
        conn.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), body=body, headers=sent)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        elapsed = (time.time() - started) * 1000
        received = resp.getheaders()
        conn.close()
        for name, value in received:
            if name.lower() == "set-cookie":
                key, _, rest = value.partition("=")
                self.cookies[key.strip()] = rest.split(";")[0]
        self.clock += timedelta(milliseconds=pause_ms)
        request = {"method": method, "url": url, "headers": [{"name": k, "value": v} for k, v in sent.items()]}
        if body is not None:
            request["postData"] = {"mimeType": content_type or "text/plain", "text": body}
        self.entries.append({
            "startedDateTime": self.clock.isoformat().replace("+00:00", "Z"), "time": elapsed,
            "pageref": "page_1", "_resourceType": "xhr", "request": request,
            "response": {"status": resp.status, "headers": [{"name": k, "value": v} for k, v in received],
                         "content": {"size": len(data), "mimeType": resp.getheader("content-type") or "",
                                     "text": data}},
        })
        self.clock += timedelta(milliseconds=elapsed)
        return resp.status, dict(received), data

    def har(self) -> bytes:
        return json.dumps({"log": {"version": "1.2", "pages": [{"id": "page_1", "title": self.base}],
                                   "entries": self.entries}}).encode()


def record_correlation_flow(base_url: str) -> bytes:
    """A flow with five values the server issues: CSRF, login token, order id, redirect code, XSRF cookie."""
    rec = Recorder(base_url)
    status, _, page = rec.request("GET", "/page/form")
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    rec.request("POST", "/page/submit", body=f"csrf={csrf}&item=book",
                content_type="application/x-www-form-urlencoded", pause_ms=6000)
    _, _, login = rec.request("POST", "/login", body='{"user": "demo"}', content_type="application/json", pause_ms=6000)
    rec.request("GET", "/private", headers={"Authorization": f"Bearer {json.loads(login)['token']}"})
    _, _, order = rec.request("GET", "/api/orders/new", pause_ms=6000)
    rec.request("GET", f"/api/orders/{json.loads(order)['order']['id']}")
    _, headers, _ = rec.request("GET", "/redirect", pause_ms=6000)
    rec.request("GET", headers["location"])
    rec.request("GET", "/xsrf", pause_ms=6000)
    rec.request("POST", "/xsrf/submit", headers={"X-XSRF-TOKEN": rec.cookies["XSRF-TOKEN"]},
                body="{}", content_type="application/json")
    assert all(e["response"]["status"] < 400 for e in rec.entries), [e["response"]["status"] for e in rec.entries]
    return rec.har()
