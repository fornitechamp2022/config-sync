/**
 * Netlify Edge Function: public, range-capable MP4 hosting.
 *
 * MP4 bytes live in the Netlify Blobs store `reels-media`. CI writes them with
 *   netlify blobs:set reels-media <key>.mp4 --input <file>
 * and this function streams them back to Instagram/YouTube over a public URL:
 *   https://bollywood-reel-generator.netlify.app/r/<key>.mp4
 *
 * The source is written in plain JavaScript syntax (valid TypeScript as-is) so
 * the same bytes can be checked with `node --check` and driven by the offline
 * Python unit tests without a TypeScript toolchain.
 */
import { getStore } from "@netlify/blobs";

export const STORE_NAME = "reels-media";
export const SITE_ORIGIN = "https://bollywood-reel-generator.netlify.app";
export const PUBLIC_PREFIX = "/r/";
const EDGE_DIRECT = "/.netlify/edge-functions/video";

const VIDEO_HEADERS = {
  "Accept-Ranges": "bytes",
  "Content-Type": "video/mp4",
  "Cache-Control": "public, max-age=86400",
};

const TEXT_HEADERS = {
  "Content-Type": "text/plain; charset=utf-8",
  "Cache-Control": "no-store",
};

/** Build the stable public URL publish.py should hand to Instagram/YouTube. */
export function publicVideoUrl(key, origin = SITE_ORIGIN) {
  const clean = String(key == null ? "" : key).replace(/^\/+/, "");
  if (!clean) return "";
  const base = String(origin || SITE_ORIGIN).replace(/\/+$/, "");
  return base + PUBLIC_PREFIX + clean;
}

/**
 * Recover the blob key from either supported URL form:
 *   /r/<key>                                  (clean path, declared in netlify.toml)
 *   /.netlify/edge-functions/video?key=<key>  (direct/query fallback)
 */
export function keyFromUrl(url) {
  let parsed;
  try {
    parsed = new URL(String(url));
  } catch (_) {
    return null;
  }

  let key = parsed.searchParams.get("key") || "";
  if (!key) {
    let path;
    try {
      path = decodeURIComponent(parsed.pathname);
    } catch (_) {
      return null;
    }
    for (const marker of [PUBLIC_PREFIX, EDGE_DIRECT + "/"]) {
      const at = path.indexOf(marker);
      if (at !== -1) {
        key = path.slice(at + marker.length);
        break;
      }
    }
  }

  key = key.replace(/^\/+/, "");
  if (!key || key.includes("\\") || key.includes("\0")) return null;
  if (key.split("/").some((part) => part === "..")) return null;
  return key;
}

/**
 * Parse a single-range `Range` header against the full object size.
 * Returns null (no header -> 200), {start, end} (-> 206) or {error: true}
 * (unsatisfiable/malformed -> 416).
 */
export function parseRange(header, size) {
  const total = Number(size);
  if (!Number.isFinite(total) || total < 0 || !Number.isInteger(total)) {
    return { error: true };
  }
  if (header === null || header === undefined || String(header).trim() === "") {
    return null;
  }
  const value = String(header).trim();
  if (!value.startsWith("bytes=") || value.includes(",")) {
    return { error: true }; // multi-range is intentionally unsupported
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match) return { error: true };
  const first = match[1];
  const last = match[2];
  if (first === "" && last === "") return { error: true };
  if (total === 0) return { error: true };

  if (first === "") {
    const suffix = Number(last);
    if (!Number.isInteger(suffix) || suffix <= 0) return { error: true };
    return { start: Math.max(0, total - suffix), end: total - 1 };
  }

  const start = Number(first);
  if (!Number.isInteger(start) || start < 0 || start >= total) {
    return { error: true };
  }
  let end = last === "" ? total - 1 : Number(last);
  if (!Number.isInteger(end) || end < start) return { error: true };
  if (end >= total) end = total - 1;
  return { start, end };
}

/** Build a plain {status, headers, body} descriptor; the handler wraps it in Response. */
export function videoResponse(body, range) {
  const bytes = body instanceof Uint8Array ? body : new Uint8Array(body);
  const size = bytes.byteLength;

  if (range && range.error) {
    return {
      status: 416,
      headers: {
        ...VIDEO_HEADERS,
        "Content-Range": `bytes */${size}`,
      },
      body: new Uint8Array(0),
    };
  }

  if (range) {
    const slice = bytes.slice(range.start, range.end + 1);
    return {
      status: 206,
      headers: {
        ...VIDEO_HEADERS,
        "Content-Range": `bytes ${range.start}-${range.end}/${size}`,
        "Content-Length": String(slice.byteLength),
      },
      body: slice,
    };
  }

  return {
    status: 200,
    headers: { ...VIDEO_HEADERS, "Content-Length": String(size) },
    body: bytes,
  };
}

export const config = { path: "/r/*" };

export default async function handler(request, context) {
  const url = new URL(request.url);
  const key = keyFromUrl(url);
  if (!key) {
    return new Response("Missing or invalid video key", {
      status: 400, headers: TEXT_HEADERS,
    });
  }

  // Site ID and token are injected automatically inside Netlify Edge Functions.
  const store = getStore(STORE_NAME);
  const data = await store.get(key, { type: "arrayBuffer" });
  if (data === null || data === undefined) {
    return new Response("Video not found", { status: 404, headers: TEXT_HEADERS });
  }

  const bytes = new Uint8Array(data);
  const header = request.headers ? request.headers.get("range") : null;
  const range = parseRange(header, bytes.byteLength);
  const out = videoResponse(bytes, range);
  return new Response(out.body, { status: out.status, headers: out.headers });
}
