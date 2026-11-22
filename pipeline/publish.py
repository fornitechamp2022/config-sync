"""Publishing: Vercel Blob (public video URL), YouTube Shorts, Instagram Reels.

Every platform step is optional and guarded by its own environment credentials.
Successful platform ids are written back to ``manifest["published"]`` so a rerun
never double-posts. No token or secret is ever stored in this repository.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
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
    """Tolerate SDK datetime objects (naive or aware) and ISO timestamp strings."""
    if isinstance(uploaded_at, str):
        try:
            uploaded_at = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(uploaded_at, datetime):
        return False
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    return uploaded_at < cutoff


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


# ---------- Vercel Blob (public media host) ----------
def upload_video(video_path: str | Path, video_cfg: dict, manifest: dict) -> dict | None:
    """Upload the MP4 to the public Blob store and prune media older than 7 days."""
    token = os.environ.get("BLOB_VIDEO_TOKEN", "").strip()
    store = os.environ.get("BLOB_VIDEO_STORE_ID", "").strip()
    opts = _vercel_options(token, store)
    if opts is None:
        return None
    try:
        from vercel.api import session
        from vercel.blob import sync as blob

        path = Path(video_path)
        if not path.is_file():
            print(f"[publish] video file missing: {video_path}")
            return None
        data = path.read_bytes()
        name = f"{PUBLIC_PREFIX}{manifest.get('id') or 'reel'}.mp4"
        with session(service_options=[opts]):
            result = blob.put_blob(name, data, access="public", content_type="video/mp4")
            url = getattr(result, "url", "")
            pathname = getattr(result, "pathname", name)

            # Rolling one-week retention for public media only.
            cutoff = datetime.now(timezone.utc) - timedelta(days=MEDIA_TTL_DAYS)
            for item in blob.query_blobs(prefix=PUBLIC_PREFIX):
                if item.pathname == name:
                    continue
                if _is_expired(getattr(item, "uploaded_at", None), cutoff):
                    try:
                        blob.delete_blob(item.pathname)
                    except Exception as exc:
                        print(f"[publish] blob cleanup skipped {item.pathname}: {exc}")
        if not _verify_public_url(url):
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
