"""Small HAR and JMX recordings shared by the import tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def entry(url, method="GET", status=200, mime="application/json", rtype="xhr", start=0, time=50,
          page="page_1", headers=None, body=None, error=None):
    request = {"method": method, "url": url,
               "headers": [{"name": k, "value": v} for k, v in (headers or {}).items()]}
    if body is not None:
        request["postData"] = {"mimeType": "application/json", "text": body}
    response = {"status": status, "content": {"size": 10, "mimeType": mime}}
    if error:
        response["_error"] = error
    started = (T0 + timedelta(milliseconds=start)).isoformat().replace("+00:00", "Z")
    return {"startedDateTime": started, "time": time, "pageref": page, "_resourceType": rtype,
            "request": request, "response": response}


def har(*entries, pages=None) -> bytes:
    pages = pages if pages is not None else [{"id": "page_1", "title": "https://shop.test/"}]
    return json.dumps({"log": {"version": "1.2", "pages": pages, "entries": list(entries)}}).encode()


UA = {"User-Agent": "test-browser"}

# Home page with static files and analytics, a pause, login, a pause, search; plus browser noise.
SHOP_ENTRIES = [
    entry("https://shop.test/", mime="text/html", rtype="document", start=0, headers=UA),                     # 0
    entry("https://shop.test/static/app.js", mime="text/javascript", rtype="script", start=100, headers=UA),  # 1
    entry("https://shop.test/logo.png", mime="image/png", rtype="image", start=120, headers=UA),              # 2
    entry("https://www.google-analytics.com/collect", rtype="ping", start=150, headers=UA),                    # 3
    entry("https://shop.test/api/v1/auth/login", method="OPTIONS", start=9_990, headers=UA),                  # 4
    entry("https://shop.test/api/v1/auth/login", method="POST", start=10_000,                                 # 5
          headers={**UA, "Cookie": "session=s3cret-cookie", "Content-Type": "application/json"},
          body='{"user": "demo", "password": "demo-pass"}'),
    entry("https://shop.test/api/v1/auth/session", start=10_100,                                              # 6
          headers={**UA, "Authorization": "Bearer s3cret-token"}),
    entry("https://shop.test/api/products/search?q=phone", start=20_000,                                      # 7
          headers={**UA, "Authorization": "Bearer s3cret-token"}),
    entry("https://shop.test/api/products/12345", start=20_200, headers=UA),                                  # 8
    entry("chrome-extension://abcdef/inject.js", rtype="script", start=20_300, headers=UA),                   # 9
    entry("https://shop.test/api/cart", status=0, start=20_400, headers=UA, error="net::ERR_ABORTED"),        # 10
]
SHOP_HAR = har(*SHOP_ENTRIES)


def _sampler(name, path, method="GET", enabled=True, children=""):
    return (
        f'<HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{name}" '
        f'enabled="{str(enabled).lower()}">'
        f'<stringProp name="HTTPSampler.path">{path}</stringProp>'
        f'<stringProp name="HTTPSampler.method">{method}</stringProp>'
        f'</HTTPSamplerProxy><hashTree>{children}</hashTree>'
    )


SHOP_JMX = f"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Shop flow" enabled="true"/>
    <hashTree>
      <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
        <stringProp name="HTTPSampler.domain">shop.test</stringProp>
        <stringProp name="HTTPSampler.protocol">https</stringProp>
      </ConfigTestElement>
      <hashTree/>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Users" enabled="true">
        <stringProp name="ThreadGroup.num_threads">7</stringProp>
      </ThreadGroup>
      <hashTree>
        <HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="Thread group headers" enabled="true"/>
        <hashTree/>
        <RecordingController guiclass="RecordController" testclass="RecordingController" testname="Recording Controller" enabled="true"/>
        <hashTree>
          <GenericController guiclass="LogicControllerGui" testclass="GenericController" testname="Home page" enabled="true"/>
          <hashTree>
            {_sampler("home", "/")}
            {_sampler("logo", "/logo.png")}
          </hashTree>
          <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="Login" enabled="true"/>
          <hashTree>
            <ConstantTimer guiclass="ConstantTimerGui" testclass="ConstantTimer" testname="Login think time" enabled="true">
              <stringProp name="ConstantTimer.delay">300</stringProp>
            </ConstantTimer>
            <hashTree/>
            {_sampler("login", "/api/login", "POST", children='<JSONPostProcessor guiclass="JSONPostProcessorGui" testclass="JSONPostProcessor" testname="Extract token" enabled="true"/><hashTree/>')}
          </hashTree>
          <IfController guiclass="IfControllerPanel" testclass="IfController" testname="Maybe promo" enabled="true"/>
          <hashTree>
            {_sampler("promo", "/promo")}
          </hashTree>
          {_sampler("old page", "/old", enabled=False)}
        </hashTree>
      </hashTree>
      <ResultCollector guiclass="SummaryReport" testclass="ResultCollector" testname="Summary Report" enabled="true"/>
      <hashTree/>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode()
