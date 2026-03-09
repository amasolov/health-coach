"""
mitmproxy addon to capture iFit API traffic.

Run with:
    mitmweb -s scripts/ifit_mitm_capture.py -p 8888

All requests to ifit.com domains are logged. Interesting ones (auth, workouts,
library, activity_logs) are saved in full to .ifit_capture/.

The mitmweb UI will be at http://localhost:8081 for live inspection.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from mitmproxy import http

CAPTURE_DIR = Path(__file__).resolve().parent.parent / ".ifit_capture"
CAPTURE_DIR.mkdir(exist_ok=True)

IFIT_DOMAINS = {"ifit.com", "api.ifit.com", "www.ifit.com", "login.ifit.com",
                "content.ifit.com", "content-api.ifit.com"}

INTERESTING_PATTERNS = [
    r"oauth",
    r"token",
    r"login",
    r"auth",
    r"workout",
    r"activity",
    r"library",
    r"program",
    r"series",
    r"search",
    r"content",
    r"trainer",
    r"categor",
    r"favorit",
    r"user",
    r"family",
    r"profile",
    r"/me",
    r"v\d+/",
]

_counter = 0


def _is_ifit(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in IFIT_DOMAINS)


def _is_interesting(url: str) -> bool:
    path = url.split("?")[0].lower()
    return any(re.search(p, path) for p in INTERESTING_PATTERNS)


def _save(prefix: str, flow: http.HTTPFlow) -> None:
    global _counter
    _counter += 1

    req = flow.request
    resp = flow.response
    ts = time.strftime("%H%M%S")

    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "method": req.method,
        "url": req.pretty_url,
        "request_headers": dict(req.headers),
        "response_status": resp.status_code if resp else None,
        "response_headers": dict(resp.headers) if resp else None,
    }

    # Request body
    if req.content:
        try:
            entry["request_body"] = json.loads(req.content)
        except Exception:
            body = req.content.decode("utf-8", errors="replace")
            entry["request_body"] = body[:2000] if len(body) > 2000 else body

    # Response body
    if resp and resp.content:
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            try:
                entry["response_body"] = json.loads(resp.content)
            except Exception:
                entry["response_body"] = resp.content.decode("utf-8", errors="replace")[:5000]
        elif "html" in ct or "text" in ct:
            entry["response_body"] = resp.content.decode("utf-8", errors="replace")[:3000]
        else:
            entry["response_body"] = f"<binary {len(resp.content)} bytes, type={ct}>"

    filename = f"{ts}_{_counter:04d}_{prefix}_{req.method}.json"
    filepath = CAPTURE_DIR / filename
    with open(filepath, "w") as f:
        json.dump(entry, f, indent=2, default=str)


def response(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    url = flow.request.pretty_url

    if not _is_ifit(host):
        return

    status = flow.response.status_code if flow.response else "?"
    method = flow.request.method

    # Always log to console
    interesting = _is_interesting(url)
    marker = " *** " if interesting else ""
    print(f"{marker}[{status}] {method} {url}{marker}")

    # Save interesting requests
    if interesting:
        path_slug = re.sub(r"[^a-zA-Z0-9]", "_", flow.request.path[:60])
        _save(path_slug, flow)

    # Also save all auth-related responses
    if flow.response and flow.response.status_code == 200:
        ct = flow.response.headers.get("content-type", "")
        if "json" in ct:
            try:
                data = json.loads(flow.response.content)
                if isinstance(data, dict) and ("access_token" in data or "token" in data):
                    print(f"  *** TOKEN CAPTURED: {list(data.keys())} ***")
                    _save("TOKEN", flow)
            except Exception:
                pass
