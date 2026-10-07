"""Local target application for testing the tool (requirements section 15).

Run:  uvicorn tests.sample_app:app --port 9000      (from the backend/ folder)

Endpoints
  GET  /fixed           fixed JSON response
  GET  /slow?ms=300     deliberately slow response
  GET  /error?rate=0.2  returns HTTP 500 for that fraction of requests
  GET  /status/{code}   returns exactly that status code
  POST /echo            echoes the request body
  POST /login           returns a session token (for the later HAR/correlation phase)
  GET  /private         needs the token from /login in an Authorization header
"""
from __future__ import annotations

import asyncio
import random
import secrets

from fastapi import FastAPI, Header, HTTPException, Request, Response

app = FastAPI(title="Sample target")
_tokens: set[str] = set()


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


@app.post("/login")
def login():
    token = secrets.token_hex(16)
    _tokens.add(token)
    return {"token": token}


@app.get("/private")
def private(authorization: str | None = Header(default=None)):
    if not authorization or authorization.removeprefix("Bearer ") not in _tokens:
        raise HTTPException(401, "invalid token")
    return {"secret": "data"}
