"""Publishing: Vercel Blob (public video URL), YouTube Shorts, Instagram Reels.

Every platform step is optional and guarded by its own environment credentials.
Successful platform ids are written back to ``manifest["published"]`` so a rerun
never double-posts. No token or secret is ever stored in this repository.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

GRAPH = "https://graph.facebook.com/v19.0"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
PUBLIC_PREFIX = "reels/"
MEDIA_TTL_DAYS = 7


# ---------- shared helpers ----------
def _vercel_options(token: str, store: str):
    """Build SDK options only when both halves of the credential are present."""
    if not (token and store):
        return None
    try:
        from vercel.blob import sync as blob
        from vercel.blob.sync import SyncBlobServiceOptions

        def factory():
            return blob.BlobCredentials(token=token, store_id=store)

        return SyncBlobServiceOptions(credentials_factory=factory)
    except Exception as exc:  # SDK absent or API changed
        print(f"[publish] Vercel Blob SDK unavailable: {exc}")
        return None


def _is_expired(uploaded_at: Any, cutoff: datetime) -> bool:
    """Tolerate SDK datetimes, ISO strings, epoch numbers and missing values.

    An unknown age is *never* treated as expired — it is always safer to keep a
    blob one more run than to delete live public media.
    """
    if uploaded_at is None or isinstance(uploaded_at, bool):
        return False
    if isinstance(uploaded_at, (int, float)):
        try:
            uploaded_at = datetime.fromtimestamp(uploaded_at, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return False
    if isinstance(uploaded_at, str):
        text = uploaded_at.strip()
        try:
            uploaded_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                uploaded_at = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return False
    if isinstance(uploaded_at, date) and not isinstance(uploaded_at, datetime):
        uploaded_at = datetime.combine(uploaded_at, datetime.min.time())
    if not isinstance(uploaded_at, datetime):
        return False
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    return uploaded_at < cutoff


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Read a blob attribute from either an SDK object or a dict page entry."""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _unpack_page(page: Any) -> tuple[list[Any], Any, Any]:
    """Normalize a listing page to (items, next_cursor, pages)."""
    if page is None:
        return [], None, None
    if isinstance(page, dict):
        items = (page.get("items") or page.get("blobs")
                 or page.get("objects") or page.get("results") or [])
        cursor = (page.get("cursor") or page.get("next_cursor")
                  or page.get("nextCursor"))
        pages = page.get("pages")
    else:
        items = None
        for attr in ("items", "blobs", "objects", "results"):
            candidate = getattr(page, attr, None)
            if isinstance(candidate, (list, tuple)):
                items = candidate
                break
        if items is None and hasattr(page, "__iter__"):
            items = list(page)
        cursor = None
        for attr in ("cursor", "next_cursor", "nextCursor"):
            candidate = getattr(page, attr, None)
            if candidate:
                cursor = candidate
                break
        pages = getattr(page, "pages", None)
    return list(items or []), cursor, pages


def _iter_blob_items(blob: Any, prefix: str, *, max_pages: int = 1000):
    """Yield every blob under ``prefix``, following cursors/pages.

    The current Vercel SDK follows cursors inside ``query_blobs`` and returns a
    flat iterator; older SDKs and HTTP wrappers return one page at a time (or
    a ``.pages`` iterator). Accept all three shapes so retention never silently
    stops at page one. A cursor loop guard prevents an infinite loop on a
    misbehaving backend.
    """
    cursor: Any = None
    seen: set[str] = set()
    for _ in range(max(0, max_pages)):
        kwargs = {"prefix": prefix}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            page = blob.query_blobs(**kwargs)
        except TypeError:
            # Very old stubs do not accept a cursor keyword.
            page = blob.query_blobs(prefix=prefix)
        items, next_cursor, pages = _unpack_page(page)
        yield from items
        if pages is not None:
            for subpage in pages:
                sub_items, _, _ = _unpack_page(subpage)
                yield from sub_items
            return
        if not next_cursor or str(next_cursor) in seen:
            return
        seen.add(str(next_cursor))
        cursor = next_cursor


def _blob_keys_to_skip(just_uploaded: dict[str, Any]) -> set[str]:
    """Identifiers of the object we must never delete during this upload."""
    skip: set[str] = set()
    for value in just_uploaded.values():
        if not isinstance(value, str) or not value:
            continue
        skip.add(value)
        skip.add(value.rsplit("/", 1)[-1])  # SDK may return a full URL
    return skip


def _delete_blob_best_effort(blob: Any, key: str) -> bool:
    try:
        blob.delete_blob(key)
        return True
    except Exception as exc:
        print(f"[publish] blob cleanup skipped {key}: {exc}")
        return False


def _prune_public_blobs(blob: Any, *, just_uploaded: dict[str, Any]) -> None:
    """Delete public media older than the retention window, best effort."""
    skip = _blob_keys_to_skip(just_uploaded)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MEDIA_TTL_DAYS)
    try:
        for item in _iter_blob_items(blob, PUBLIC_PREFIX):
            key = _field(item, "pathname") or _field(item, "key") or _field(item, "url")
            if not key:
                continue
            key = str(key)
            if (key in skip or key.rsplit("/", 1)[-1] in skip
                    or str(_field(item, "url") or "") in skip):
                continue
            if _is_expired(_field(item, "uploaded_at"), cutoff):
                _delete_blob_best_effort(blob, key)
    except Exception as exc:
        # A listing outage must not fail the upload or the social publishes.
        print(f"[publish] public blob cleanup skipped: {exc}")


def _verify_public_url(url: str) -> bool:
    """Confirm the Blob URL is reachable over https before handing it to socials."""
    if not url or not url.startswith("https://"):
        print("[publish] Blob returned no public https URL")
        return False
    try:
        resp = requests.head(url, timeout=15, allow_redirects=True)
        if resp.status_code >= 400:
            print(f"[publish] public URL check failed: HTTP {resp.status_code}")
            return False
    except Exception as exc:
        print(f"[publish] public URL check failed: {exc}")
        return False
    return True


def _already_published(manifest: dict, platform: str) -> bool:
    """Ledger guard: a platform id already recorded means do not post again."""
    published = manifest.get("published") or {}
    if not isinstance(published, dict):
        return False
    entry = published.get(platform)
    if isinstance(entry, dict):
        return bool(entry.get("video_id") or entry.get("media_id") or entry.get("id"))
    return bool(entry)


# ---------- Netlify Blob + Edge Function (public media host) ----------
NETLIFY_MEDIA_STORE = "reels-media"
NETLIFY_SITE_URL = "https://bollywood-reel-generator.netlify.app"
NETLIFY_MEDIA_TTL_DAYS = 7


def _netlify_cli() -> list[str] | None:
    """Locate the Netlify CLI (global binary, else the pinned local install)."""
    exe = shutil.which("netlify")
    if exe:
        return [exe]
    local = (Path(__file__).resolve().parent.parent / "node_modules" /
             "netlify-cli" / "bin" / "run.js")
    if local.exists():
        return ["node", str(local)]
    return None


def _netlify_run(args: list[str], timeout: int = 300):
    cli = _netlify_cli()
    if cli is None:
        return None
    try:
        return subprocess.run(cli + args, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[publish] netlify CLI call failed: {type(exc).__name__}")
        return None


def _prune_netlify_media(just_uploaded: str) -> None:
    """Delete media blobs older than the retention window; best effort."""
    proc = _netlify_run(["blobs:list", NETLIFY_MEDIA_STORE, "--json"])
    if proc is None or proc.returncode != 0:
        return
    try:
        data = json.loads(proc.stdout or "[]")
    except (TypeError, ValueError):
        return
    items = data if isinstance(data, list) else (data.get("blobs") or data.get("items") or [])
    cutoff = time.time() - NETLIFY_MEDIA_TTL_DAYS * 86400
    for item in items:
        key = item if isinstance(item, str) else (item.get("key") or item.get("pathname"))
        if not key or key == just_uploaded:
            continue
        uploaded = item.get("uploadedAt") or item.get("uploaded_at") if isinstance(item, dict) else None
        if uploaded:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(str(uploaded).replace("Z", "+00:00"))
                if dt.timestamp() > cutoff:
                    continue
            except Exception:
                continue  # unknown age: keep, never delete live media
        else:
            continue
        _netlify_run(["blobs:delete", NETLIFY_MEDIA_STORE, str(key), "--force"])


def upload_video_netlify(video_path: str | Path, manifest: dict) -> dict | None:
    """Upload the MP4 to Netlify Blobs and return the Edge Function URL."""
    token = os.environ.get("NETLIFY_AUTH_TOKEN", "").strip()
    site = os.environ.get("NETLIFY_SITE_ID", "").strip()
    if not (token and site) or _netlify_cli() is None:
        return None
    path = Path(video_path)
    if not path.is_file():
        return None
    import re
    reel_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(manifest.get("id") or "reel")).strip("-")[:80]
    key = f"reels/{reel_id}.mp4"
    proc = _netlify_run(["blobs:set", NETLIFY_MEDIA_STORE, key,
                         "--input", str(path), "--force"], timeout=600)
    if proc is None or proc.returncode != 0:
        print(f"[publish] Netlify blob upload failed: {(proc.stderr or '')[-200:] if proc else 'no CLI'}")
        return None
    url = f"{NETLIFY_SITE_URL}/r/{key}"
    if not _verify_public_url(url):
        _netlify_run(["blobs:delete", NETLIFY_MEDIA_STORE, key, "--force"])
        return None
    _prune_netlify_media(just_uploaded=key)
    return {"url": url, "pathname": key, "bytes": path.stat().st_size,
            "provider": "netlify"}


# ---------- Vercel Blob (public media host, optional fallback) ----------
def upload_video(video_path: str | Path, video_cfg: dict, manifest: dict) -> dict | None:
    """Upload the MP4 to the public Blob store and prune media older than 7 days.

    Returns the public URL only after it has been verified reachable; an
    unverifiable upload is removed again so no unreferenced bytes accumulate.
    Cleanup issues are logged and never fail the publish step.
    """
    token = os.environ.get("BLOB_VIDEO_TOKEN", "").strip()
    store = os.environ.get("BLOB_VIDEO_STORE_ID", "").strip()
    opts = _vercel_options(token, store)
    if opts is None:
        # Netlify is the default host; Vercel remains an optional fallback.
        return upload_video_netlify(video_path, manifest)
    path = Path(video_path)
    if not path.is_file():
        print(f"[publish] video file missing: {video_path}")
        return None
    try:
        data = path.read_bytes()
        name = f"{PUBLIC_PREFIX}{manifest.get('id') or 'reel'}.mp4"
        from vercel.api import session
        from vercel.blob import sync as blob

        with session(service_options=[opts]):
            result = blob.put_blob(name, data, access="public", content_type="video/mp4")
            url = getattr(result, "url", "") or ""
            pathname = getattr(result, "pathname", name) or name

            # Rolling one-week retention for public media only. Never delete
            # the object uploaded a moment ago, whatever shape its metadata has.
            _prune_public_blobs(blob, just_uploaded={
                "name": name, "pathname": pathname, "url": url,
            })

        if not _verify_public_url(url):
            # Do not leave an unverifiable (and unpublished) blob behind.
            try:
                with session(service_options=[opts]):
                    _delete_blob_best_effort(blob, pathname)
            except Exception as exc:
                print(f"[publish] cleanup of unverified blob skipped: {exc}")
            return None
        return {"url": url, "pathname": pathname, "bytes": len(data)}
    except Exception as exc:
        print(f"[publish] blob upload failed: {exc}")
        return None


# ---------- YouTube Shorts ----------
def _youtube_token() -> str | None:
    """Exchange the OAuth refresh token for a short-lived access token."""
    cid = os.environ.get("YT_CLIENT_ID", "").strip()
    secret = os.environ.get("YT_CLIENT_SECRET", "").strip()
    refresh = os.environ.get("YT_REFRESH_TOKEN", "").strip()
    if not all((cid, secret, refresh)):
        return None
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": cid, "client_secret": secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        }, timeout=30)
        if resp.status_code != 200:
            print(f"[publish] YouTube token error: {resp.text[:200]}")
            return None
        token = resp.json().get("access_token")
        if not token:
            print("[publish] YouTube token response had no access_token")
            return None
        return token
    except Exception as exc:
        print(f"[publish] YouTube token error: {exc}")
        return None


def youtube_upload(
    video_path: str | Path, video_url: str, manifest: dict, cfg: dict
) -> dict | None:
    """Resumable upload of a Short; returns the video id or None on any error."""
    if _already_published(manifest, "youtube"):
        print("[publish] YouTube already published for this reel; skipping")
        return manifest["published"]["youtube"]
    token = _youtube_token()
    if not token:
        return None

    story = manifest.get("story") or {}
    headline = story.get("headline", "")
    description = (
        f"{headline}\n\n{story.get('script', '')}\n\n"
        f"स्रोत: {story.get('source', '')} — {story.get('url', '')}\n"
        "समाचार चित्रों के हक़ उनके प्रकाशकों के पास हैं।\n#Shorts #Bollywood #BiggBoss"
    )
    pcfg = cfg.get("publish") or {}
    privacy = "private" if pcfg.get("verification_required", True) else "public"
    metadata = {
        "snippet": {
            "title": f"{headline[:90]} #Shorts",
            "description": description[:4900],
            "tags": ["Shorts", "Bollywood", "Bigg Boss", "Entertainment News"],
            "categoryId": "24",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    try:
        path = Path(video_path)
        size = path.stat().st_size
        init = requests.post(
            YOUTUBE_UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(size),
            },
            data=json.dumps(metadata), timeout=30,
        )
        location = init.headers.get("Location")
        if init.status_code not in (200, 201) or not location:
            print(f"[publish] YouTube init failed (HTTP {init.status_code}): "
                  f"{init.text[:200]}")
            return None
        upload = requests.put(location, data=path.read_bytes(), headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }, timeout=180)
        if upload.status_code not in (200, 201):
            print(f"[publish] YouTube upload failed (HTTP {upload.status_code}): "
                  f"{upload.text[:200]}")
            return None
        video_id = upload.json().get("id")
        if not video_id:
            print("[publish] YouTube upload response had no video id")
            return None
        return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
    except Exception as exc:
        print(f"[publish] YouTube error: {exc}")
        return None


# ---------- Instagram Reels ----------
def instagram_upload(video_url: str, manifest: dict, cfg: dict) -> dict | None:
    """Two-step Graph API Reels publish: create container, poll, then publish."""
    if _already_published(manifest, "instagram"):
        print("[publish] Instagram already published for this reel; skipping")
        return manifest["published"]["instagram"]
    token = os.environ.get("IG_LONG_LIVED_TOKEN", "").strip()
    ig_id = os.environ.get("IG_USER_ID", "").strip()
    if not (token and ig_id):
        return None
    if not video_url:
        print("[publish] Instagram needs a public video URL; skipping")
        return None

    story = manifest.get("story") or {}
    caption = (
        f"{story.get('headline', '')}\n\n"
        f"स्रोत: {story.get('source', '')} — {story.get('url', '')}\n"
        "#Bollywood #BiggBoss #EntertainmentNews #Shorts"
    )
    try:
        create = requests.post(f"{GRAPH}/{ig_id}/media", data={
            "media_type": "REELS", "video_url": video_url,
            "caption": caption[:2200], "access_token": token,
        }, timeout=60)
        if create.status_code != 200:
            print(f"[publish] IG container failed (HTTP {create.status_code}): "
                  f"{create.text[:200]}")
            return None
        container = create.json().get("id")
        if not container:
            print("[publish] IG container response had no id")
            return None

        finished = False
        for _ in range(30):
            status_resp = requests.get(f"{GRAPH}/{container}", params={
                "fields": "status_code", "access_token": token}, timeout=30)
            if status_resp.status_code != 200:
                print(f"[publish] IG status check failed: HTTP {status_resp.status_code}")
                return None
            code = status_resp.json().get("status_code")
            if code == "FINISHED":
                finished = True
                break
            if code in ("ERROR", "EXPIRED"):
                print(f"[publish] IG container {code}")
                return None
            time.sleep(2)
        if not finished:
            print("[publish] IG container did not finish in time")
            return None

        publish = requests.post(f"{GRAPH}/{ig_id}/media_publish", data={
            "creation_id": container, "access_token": token}, timeout=60)
        if publish.status_code != 200:
            print(f"[publish] IG publish failed (HTTP {publish.status_code}): "
                  f"{publish.text[:200]}")
            return None
        media_id = publish.json().get("id")
        if not media_id:
            print("[publish] IG publish response had no media id")
            return None

        permalink = ""
        try:
            detail = requests.get(f"{GRAPH}/{media_id}", params={
                "fields": "permalink", "access_token": token}, timeout=30)
            if detail.status_code == 200:
                permalink = detail.json().get("permalink") or ""
        except Exception as exc:
            print(f"[publish] IG permalink lookup skipped: {exc}")
        return {"media_id": media_id, "url": permalink}
    except Exception as exc:
        print(f"[publish] Instagram error: {exc}")
        return None


# ---------- orchestration ----------
def publish_all(
    video_path: str | Path, video_url: str, manifest: dict, cfg: dict
) -> dict:
    """Run enabled platforms and record their ids in the manifest ledger."""
    pcfg = cfg.get("publish") or {}
    result: dict[str, Any] = {}
    if pcfg.get("youtube", True):
        result["youtube"] = youtube_upload(video_path, video_url, manifest, cfg)
    if pcfg.get("instagram", True):
        result["instagram"] = instagram_upload(video_url, manifest, cfg)

    ledger = manifest.setdefault("published", {})
    if isinstance(ledger, dict):
        ledger.update({k: v for k, v in result.items() if v})
    return result
