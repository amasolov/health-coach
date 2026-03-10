"""
mitmproxy addon to capture Hevy API traffic.

Run with:
    mitmweb -s scripts/hevy_mitm_capture.py -p 8888

All requests to hevyapp.com domains are logged. Interesting ones (routines,
exercises, workouts, auth) are saved in full to .hevy_capture/.

The mitmweb UI will be at http://localhost:8081 for live inspection.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from mitmproxy import http

CAPTURE_DIR = Path(__file__).resolve().parent.parent / ".hevy_capture"
CAPTURE_DIR.mkdir(exist_ok=True)

HEVY_DOMAINS = {
    "hevyapp.com",
    "api.hevyapp.com",
    "hevy.com",
    "api.hevy.com",
}

INTERESTING_PATTERNS = [
    r"routine",
    r"exercise",
    r"workout",
    r"template",
    r"folder",
    r"user",
    r"auth",
    r"login",
    r"token",
    r"account",
    r"v\d+/",
]

_counter = 0


def _is_hevy(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in HEVY_DOMAINS)


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

    if req.content:
        try:
            entry["request_body"] = json.loads(req.content)
        except Exception:
            body = req.content.decode("utf-8", errors="replace")
            entry["request_body"] = body[:2000] if len(body) > 2000 else body

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

    print(f"  -> Saved: {filename}")


def response(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    url = flow.request.pretty_url

    if not _is_hevy(host):
        return

    status = flow.response.status_code if flow.response else "?"
    method = flow.request.method

    interesting = _is_interesting(url)
    marker = " *** " if interesting else ""

    # Highlight DELETE requests — these are what we're hunting for
    if method == "DELETE":
        marker = " !!! DELETE !!! "

    print(f"{marker}[{status}] {method} {url}{marker}")

    # Save all Hevy API requests (the traffic volume is low)
    path_slug = re.sub(r"[^a-zA-Z0-9]", "_", flow.request.path[:60])
    _save(path_slug, flow)
