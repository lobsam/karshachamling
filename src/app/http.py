"""Outbound HTTP. Uses the Workers fetch() API in the Worker, urllib elsewhere (tests, scripts)."""

import asyncio
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

IN_WORKER = sys.platform == "emscripten"


@dataclass
class HttpResponse:
    status: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


class HttpError(Exception):
    pass


async def post(
    url: str,
    *,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    basic_auth: tuple[str, str] | None = None,
) -> HttpResponse:
    h = dict(headers or {})
    body: str | None = None
    if json_body is not None:
        body = json.dumps(json_body)
        h["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form)
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if basic_auth:
        token = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
        h["Authorization"] = f"Basic {token}"
    try:
        if IN_WORKER:
            return await _worker_fetch(url, body, h)
        return await asyncio.to_thread(_urllib_post, url, body, h)
    except HttpError:
        raise
    except Exception as e:  # network failures
        raise HttpError(str(e)) from e


async def _worker_fetch(url: str, body: str | None, headers: dict[str, str]) -> HttpResponse:
    from js import Object, fetch
    from pyodide.ffi import to_js

    opts = {"method": "POST", "headers": headers}
    if body is not None:
        opts["body"] = body
    resp = await fetch(url, to_js(opts, dict_converter=Object.fromEntries))
    return HttpResponse(int(resp.status), await resp.text())


def _urllib_post(url: str, body: str | None, headers: dict[str, str]) -> HttpResponse:
    req = urllib.request.Request(
        url, data=body.encode() if body is not None else b"", headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return HttpResponse(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, e.read().decode())
