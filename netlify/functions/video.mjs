import { getStore } from "@netlify/blobs";

const STORE = "reels-media";
const TEXT = { "Content-Type": "text/plain; charset=utf-8" };

export function parseRange(header, size) {
  if (!header || !header.startsWith("bytes=")) return null;
  const spec = header.slice(6).trim();
  if (spec.includes(",")) return { invalid: true };
  if (spec.startsWith("-")) {
    const suffix = Number(spec.slice(1));
    if (!Number.isInteger(suffix) || suffix <= 0) return { invalid: true };
    const start = Math.max(0, size - suffix);
    return { start, end: size - 1 };
  }
  const [a, b] = spec.split("-");
  const start = Number(a);
  if (!Number.isInteger(start) || start < 0 || start >= size) return { invalid: true };
  let end = b === "" || b === undefined ? size - 1 : Number(b);
  if (!Number.isInteger(end) || end < start) return { invalid: true };
  end = Math.min(end, size - 1);
  return { start, end };
}

function textResponse(message, status) {
  return new Response(message, { status, headers: TEXT });
}

export default async (request) => {
  try {
    const url = new URL(request.url);
    let key = url.searchParams.get("key");
    if (!key) {
      const match = url.pathname.match(/^\/r\/(.+)$/);
      if (match) key = decodeURIComponent(match[1]);
    }
    if (!key || key.includes("..") || key.startsWith("/")) {
      return textResponse("Invalid key", 400);
    }

    const store = getStore({
      name: STORE,
      siteID: process.env.NETLIFY_SITE_ID,
      token: process.env.NETLIFY_AUTH_TOKEN,
    });
    const data = await store.get(key, { type: "arrayBuffer" });
    if (!data) return textResponse("Not found", 404);

    const bytes = new Uint8Array(data);
    const total = bytes.byteLength;
    const range = parseRange(request.headers.get("range"), total);
    const base = {
      "Content-Type": "video/mp4",
      "Accept-Ranges": "bytes",
      "Cache-Control": "public, max-age=3600",
    };

    if (range?.invalid) {
      return new Response(null, { status: 416, headers: { ...TEXT, "Content-Range": `bytes */${total}` } });
    }
    if (range) {
      const slice = bytes.slice(range.start, range.end + 1);
      return new Response(slice, {
        status: 206,
        headers: {
          ...base,
          "Content-Range": `bytes ${range.start}-${range.end}/${total}`,
          "Content-Length": String(slice.byteLength),
        },
      });
    }
    return new Response(bytes, { status: 200, headers: { ...base, "Content-Length": String(total) } });
  } catch (err) {
    return textResponse("video error", 500);
  }
};
