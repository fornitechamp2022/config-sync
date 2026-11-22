"""Offline tests for the Netlify Edge Function that serves public MP4s.

The edge function is JavaScript-compatible TypeScript, so the exact same bytes
are copied to ``video.mjs`` and executed with Node. ``@netlify/blobs`` is
replaced with an in-memory stub: no network calls and no credentials are used.
Every range/response/URL helper is exercised through the real source file.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EDGE_TS = ROOT / "netlify" / "edge-functions" / "video.ts"
TOML = ROOT / "netlify.toml"
NODE = shutil.which("node")

SITE = "https://bollywood-reel-generator.netlify.app"
BLOB_BYTES = b"0123456789"


# --------------------------------------------------------------------------
# Node harness
# --------------------------------------------------------------------------
STUB_PACKAGE = {
    "name": "@netlify/blobs",
    "version": "0.0.0-stub",
    "type": "module",
    "main": "index.mjs",
}

STUB_SOURCE = """
export function getStore(name) {
  return {
    name,
    async get(key, options) {
      const blobs = globalThis.__NETLIFY_BLOBS__ || {};
      if (!Object.prototype.hasOwnProperty.call(blobs, key)) return null;
      if (!options || options.type !== "arrayBuffer") {
        throw new Error("unexpected get() options: " + JSON.stringify(options));
      }
      const binary = atob(blobs[key]);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return bytes.buffer;
    },
  };
}
"""

RUNNER_SOURCE = """
import { readFileSync } from "node:fs";
import * as mod from "./video.mjs";

const req = JSON.parse(readFileSync(0, "utf8"));
if (req.blobs) globalThis.__NETLIFY_BLOBS__ = req.blobs;

function decode(encoded) {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

let out;
if (req.call === "handler") {
  const request = new Request(req.args[0], { headers: req.args[1] || {} });
  const response = await mod.default(request, {});
  out = {
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    body: Array.from(new Uint8Array(await response.arrayBuffer())),
  };
} else {
  const args = Array.isArray(req.args) ? req.args.slice() : [];
  if (req.call === "videoResponse") args[0] = decode(args[0]);
  out = await mod[req.call](...args);
  if (out && typeof out === "object" && out.body instanceof Uint8Array) {
    out = { ...out, body: Array.from(out.body) };
  }
}
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture(scope="module")
def js(tmp_path_factory):
    """Copy the real edge source into a Node workspace with a mocked Blob SDK."""
    if NODE is None:
        pytest.skip("node is required to execute the edge function source")

    source = EDGE_TS.read_text(encoding="utf-8")
    work = tmp_path_factory.mktemp("netlify-video")
    (work / "video.mjs").write_text(source, encoding="utf-8")
    (work / "runner.mjs").write_text(RUNNER_SOURCE, encoding="utf-8")
    pkg_dir = work / "node_modules" / "@netlify" / "blobs"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps(STUB_PACKAGE), encoding="utf-8")
    (pkg_dir / "index.mjs").write_text(STUB_SOURCE, encoding="utf-8")

    check = subprocess.run(
        [NODE, "--check", str(work / "video.mjs")],
        capture_output=True, text=True, timeout=30,
    )
    return {"work": work, "source": source, "check": check}


def call_js(js, name, *args, blobs=None):
    payload = {"call": name, "args": list(args)}
    if blobs is not None:
        payload["blobs"] = blobs
    proc = subprocess.run(
        [NODE, str(js["work"] / "runner.mjs")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"{name} failed under node: {proc.stderr}"
    result = json.loads(proc.stdout)
    # Node's Headers.entries() lowercases names; normalise for plain assertions.
    if isinstance(result, dict) and isinstance(result.get("headers"), dict):
        result["headers"] = {k.lower(): v for k, v in result["headers"].items()}
    return result


def call_handler(js, url, headers=None, blobs=None):
    return call_js(js, "handler", url, headers or {}, blobs=blobs or {})


# --------------------------------------------------------------------------
# source / config sanity
# --------------------------------------------------------------------------
def test_edge_source_is_valid_js_and_imports_only_netlify_blobs(js):
    assert js["check"].returncode == 0, js["check"].stderr
    source = js["source"]
    imports = re.findall(
        r'^\s*import\s+[^;\n]*?\bfrom\s+["\']([^"\']+)["\']', source, re.M
    )
    assert imports == ["@netlify/blobs"]
    assert "export default async function handler" in source
    assert 'getStore(STORE_NAME)' in source


def test_netlify_toml_declares_edge_function_without_touching_static_site():
    assert tomllib is not None, "tomllib (Python 3.11+) is required"
    data = tomllib.loads(TOML.read_text(encoding="utf-8"))

    edges = data.get("edge_functions", [])
    assert any(e.get("function") == "video" and e.get("path") == "/r/*"
               for e in edges)
    redirects = data.get("redirects", [])
    assert any(r.get("from") == "/video/*" and r.get("to") == "/r/:splat"
               and r.get("status") == 301 for r in redirects)
    # A [build] section would replace the deployed subscribe page + service worker.
    assert "build" not in data


# --------------------------------------------------------------------------
# URL construction helper
# --------------------------------------------------------------------------
def test_url_construction_helper(js):
    url = call_js(js, "publicVideoUrl", "abc.mp4")
    assert url == SITE + "/r/abc.mp4"
    assert call_js(js, "publicVideoUrl", "abc.mp4", "https://example.com/") == \
        "https://example.com/r/abc.mp4"
    assert call_js(js, "publicVideoUrl", "") == ""
    assert call_js(js, "keyFromUrl", url) == "abc.mp4"
    assert call_js(js, "keyFromUrl", SITE + "/r/reels/abc.mp4") == "reels/abc.mp4"
    assert call_js(js, "keyFromUrl", SITE + "/r/../secret.mp4") is None
    assert call_js(js, "keyFromUrl", "not a url") is None


# --------------------------------------------------------------------------
# range parsing
# --------------------------------------------------------------------------
def test_parse_range_no_header_means_full_body(js):
    assert call_js(js, "parseRange", None, 10) is None
    assert call_js(js, "parseRange", "", 10) is None


def test_parse_range_valid_forms(js):
    assert call_js(js, "parseRange", "bytes=2-5", 10) == {"start": 2, "end": 5}
    assert call_js(js, "parseRange", "bytes=7-", 10) == {"start": 7, "end": 9}
    assert call_js(js, "parseRange", "bytes=-4", 10) == {"start": 6, "end": 9}
    # An oversized end is clipped but Content-Range still reports the real total.
    assert call_js(js, "parseRange", "bytes=8-99", 10) == {"start": 8, "end": 9}


@pytest.mark.parametrize("header,size", [
    ("bytes=10-12", 10),   # start past EOF
    ("bytes=5-2", 10),     # inverted
    ("bytes=abc", 10),     # malformed
    ("bytes=-", 10),       # empty
    ("bytes=-0", 10),      # zero suffix
    ("items=0-1", 10),     # wrong unit
    ("bytes=0-1,3-4", 10),  # multi-range unsupported
    ("bytes=0-0", 0),      # empty object
])
def test_parse_range_invalid_is_unsatisfiable(js, header, size):
    assert call_js(js, "parseRange", header, size) == {"error": True}


# --------------------------------------------------------------------------
# response building
# --------------------------------------------------------------------------
def test_full_response_headers(js):
    payload = _b64(BLOB_BYTES)
    resp = call_js(js, "videoResponse", payload, None)
    assert resp["status"] == 200
    assert resp["headers"]["content-type"] == "video/mp4"
    assert resp["headers"]["accept-ranges"] == "bytes"
    assert resp["headers"]["content-length"] == "10"
    assert "content-range" not in resp["headers"]
    assert resp["body"] == list(BLOB_BYTES)


def test_206_range_response(js):
    range_spec = call_js(js, "parseRange", "bytes=2-5", 10)
    resp = call_js(js, "videoResponse", _b64(BLOB_BYTES), range_spec)
    assert resp["status"] == 206
    assert resp["headers"]["content-range"] == "bytes 2-5/10"
    assert resp["headers"]["content-length"] == "4"
    assert resp["headers"]["content-type"] == "video/mp4"
    assert resp["body"] == list(BLOB_BYTES[2:6])


def test_open_ended_and_suffix_ranges(js):
    for header, expected_range, expected_body in (
        ("bytes=7-", {"start": 7, "end": 9}, BLOB_BYTES[7:]),
        ("bytes=-3", {"start": 7, "end": 9}, BLOB_BYTES[-3:]),
    ):
        spec = call_js(js, "parseRange", header, 10)
        assert spec == expected_range
        resp = call_js(js, "videoResponse", _b64(BLOB_BYTES), spec)
        assert resp["status"] == 206
        assert resp["headers"]["content-range"] == "bytes 7-9/10"
        assert resp["body"] == list(expected_body)


def test_invalid_range_builds_416(js):
    spec = call_js(js, "parseRange", "bytes=99-100", 10)
    resp = call_js(js, "videoResponse", _b64(BLOB_BYTES), spec)
    assert resp["status"] == 416
    assert resp["headers"]["content-range"] == "bytes */10"
    assert resp["headers"]["accept-ranges"] == "bytes"
    assert resp["body"] == []


# --------------------------------------------------------------------------
# handler end-to-end with the in-memory Blob store
# --------------------------------------------------------------------------
def test_handler_serves_full_video_from_clean_path(js):
    blobs = {"abc.mp4": _b64(BLOB_BYTES)}
    resp = call_handler(js, SITE + "/r/abc.mp4", blobs=blobs)
    assert resp["status"] == 200
    assert resp["headers"]["content-type"] == "video/mp4"
    assert resp["headers"]["content-length"] == "10"
    assert resp["body"] == list(BLOB_BYTES)


def test_handler_serves_range_via_query_fallback(js):
    blobs = {"abc.mp4": _b64(BLOB_BYTES)}
    url = SITE + "/.netlify/edge-functions/video?key=abc.mp4"
    resp = call_handler(js, url, {"range": "bytes=2-5"}, blobs=blobs)
    assert resp["status"] == 206
    assert resp["headers"]["content-range"] == "bytes 2-5/10"
    assert resp["body"] == list(BLOB_BYTES[2:6])


def test_handler_returns_404_for_missing_blob(js):
    resp = call_handler(js, SITE + "/r/missing.mp4", blobs={})
    assert resp["status"] == 404
    text = "".join(chr(code) for code in resp["body"]).lower()
    assert "not found" in text


def test_handler_rejects_bad_key(js):
    resp = call_handler(js, SITE + "/r/%2e%2e/secret.mp4", blobs={})
    assert resp["status"] == 400
