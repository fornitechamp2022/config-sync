"""State layer.

Default: append-only JSON records in a PRIVATE Netlify Blob store named
``reel-state``. The Vercel Blob SDK backend remains available, and a local JSON
file is used for development and as a safe fallback whenever the chosen
backend's CLI/credentials/SDK are unavailable. One immutable, uniquely named
record per run avoids overwrites, CDN staleness and the need for conditional
writes.

State stores metadata only — article keys, image hashes, ledger ids and public
URLs. Image/video *bytes* are never committed to state. Credentials are read
from the environment and are never placed in CLI command strings or logs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .utils import similarity

JACCARD_THRESHOLD = 0.6
RECORD_PREFIX = "state/runs/"
NETLIFY_STATE_STORE = "reel-state"
STATE_TTL_DAYS = 30
# The newest runs and everything they reference are pinned so the current run
# can never be pruned, even if the local clock or an old record disagrees.
KEEP_RECENT_RUNS = 5
PRUNED_AT_KEY = "pruned_at"
LAST_PRUNE_KEY = "last_prune"

# Sentinel used by _metadata_only() to distinguish "drop this value" from a
# legitimate JSON null.
_DROP = object()


def _blank() -> dict:
    return {"version": 1, "articles": {}, "images": {}, "reels": [], "runs": []}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    """Parse an ISO timestamp, datetime/date, epoch number or id prefix."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            match = re.match(r"^(\d{8}T\d{6}Z)", text)
            try:
                if match:
                    parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
                else:
                    parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _key_time(key: str) -> datetime | None:
    """Read the UTC timestamp embedded in a remote record key, if present."""
    match = re.search(r"(\d{8}T\d{6}Z)", str(key))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _entry_time(entry: Any) -> datetime | None:
    """Age of a ledger entry from its own metadata (never from media bytes)."""
    if not isinstance(entry, dict):
        return None
    for field in ("at", "updated_at", "seen_at", "created_at"):
        parsed = _parse_time(entry.get(field))
        if parsed is not None:
            return parsed
    return None


def _metadata_only(value: Any) -> Any:
    """Recursively strip binary payloads so state can never commit media bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _DROP
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _metadata_only(item)
            if cleaned is not _DROP:
                out[str(key)] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        out_list: list[Any] = []
        for item in value:
            cleaned = _metadata_only(item)
            if cleaned is not _DROP:
                out_list.append(cleaned)
        return out_list
    if isinstance(value, Path):
        return str(value)
    return value


class State:
    def __init__(self, cfg: dict, workdir: str | Path = "."):
        self.cfg = cfg or {}
        self.workdir = Path(workdir)
        publish_cfg = self.cfg.get("publish") or {}
        self.backend = publish_cfg.get("state_backend", "local")
        self.data = _blank()
        # Credentials are read from the environment, never from the repo/config.
        self._token = os.environ.get("BLOB_STATE_TOKEN", "").strip()
        self._store = os.environ.get("BLOB_STATE_STORE_ID", "").strip()
        # Netlify CLI reads these straight from the environment; we only inspect
        # them here to decide whether the backend is usable. They are never
        # passed on a command line (which could be logged).
        self._netlify_token = os.environ.get("NETLIFY_AUTH_TOKEN", "").strip()
        self._netlify_site = os.environ.get("NETLIFY_SITE_ID", "").strip()
        self._netlify_store = str(
            publish_cfg.get("netlify_state_store") or NETLIFY_STATE_STORE
        )
        self._local = self.workdir / "state" / "ledger.json"
        try:
            self._threshold = float(
                publish_cfg.get("state_jaccard_threshold", JACCARD_THRESHOLD)
            )
        except (TypeError, ValueError):
            self._threshold = JACCARD_THRESHOLD

    # ---------- credentials / SDK ----------
    def _has_blob_creds(self) -> bool:
        """Both halves of the credential pair are required for a private store."""
        return self.backend == "blob" and bool(self._token and self._store)

    def _blob_options(self):
        """Return SDK session options, or None when Blob is unavailable."""
        if not self._has_blob_creds():
            return None
        try:
            from vercel.blob import sync as blob
            from vercel.blob.sync import SyncBlobServiceOptions

            def factory():
                return blob.BlobCredentials(token=self._token, store_id=self._store)

            return SyncBlobServiceOptions(credentials_factory=factory)
        except Exception as exc:  # SDK absent or API changed
            print(f"[state] Vercel Blob SDK unavailable: {exc}")
            return None

    # ---------- Netlify Blobs (CLI) ----------
    def _has_netlify_creds(self) -> bool:
        """The Netlify CLI reads both credentials from the environment."""
        return (
            self.backend == "netlify"
            and bool(self._netlify_token and self._netlify_site)
        )

    def _netlify_cli(self):
        """Return the CLI argv prefix, or None when no CLI is installed.

        Prefer a locally installed binary; fall back to the project's pinned
        local CLI via ``npx --no-install``. ``--no-install`` matters in CI: it
        prevents npm from fetching an unpinned version at runtime.
        """
        exe = shutil.which("netlify")
        if exe:
            return [exe]
        npx = shutil.which("npx")
        if npx:
            return [npx, "--no-install", "netlify"]
        return None

    def _netlify_run(self, args: list[str]):
        """Run one Netlify CLI subcommand. Never echoes credentials."""
        cli = self._netlify_cli()
        if cli is None:
            return None
        try:
            # Credentials are supplied only via the inherited environment.
            return subprocess.run(
                cli + args, capture_output=True, text=True, timeout=180
            )
        except Exception as exc:  # CLI missing, crashed or timed out
            print(f"[state] netlify CLI call failed: {type(exc).__name__}")
            return None

    @staticmethod
    def _netlify_keys(stdout: str) -> list[str]:
        """Accept either a JSON array or an object wrapping the key list."""
        try:
            data = json.loads(stdout)
        except (TypeError, ValueError):
            return []
        if isinstance(data, dict):
            for field in ("blobs", "keys", "items", "objects", "results"):
                if isinstance(data.get(field), list):
                    data = data[field]
                    break
        if not isinstance(data, list):
            return []
        keys: list[str] = []
        for item in data:
            if isinstance(item, str):
                key = item
            elif isinstance(item, dict):
                key = (
                    item.get("key")
                    or item.get("pathname")
                    or item.get("name")
                    or ""
                )
            else:
                continue
            key = str(key)
            if key.startswith(RECORD_PREFIX):
                keys.append(key)
        return keys

    def _netlify_load(self) -> bool:
        """Merge all immutable Netlify records. Return False to use local JSON."""
        if not self._has_netlify_creds() or self._netlify_cli() is None:
            return False
        proc = self._netlify_run(
            ["blobs:list", self._netlify_store, "--prefix", RECORD_PREFIX, "--json"]
        )
        if proc is None or proc.returncode != 0:
            print("[state] netlify blob list failed, using local")
            return False

        records: list[tuple[str, dict]] = []
        for key in self._netlify_keys(proc.stdout):
            fd, tmp = tempfile.mkstemp(prefix="netlify-state-", suffix=".json")
            os.close(fd)
            try:
                got = self._netlify_run(
                    ["blobs:get", self._netlify_store, key, "--output", tmp]
                )
                if got is None or got.returncode != 0:
                    print(f"[state] skipping unreadable netlify record {key}")
                    continue
                record = json.loads(Path(tmp).read_text(encoding="utf-8"))
                if isinstance(record, dict):
                    records.append((key, record))
            except Exception as exc:
                print(f"[state] skipping corrupt netlify record {key}: "
                      f"{type(exc).__name__}")
                continue
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        # Apply oldest-first so later runs win on shared article keys.
        for _key, record in sorted(
            records, key=lambda pair: (str(pair[1].get("at", "")), pair[0])
        ):
            self._merge(record)
        return True

    def _netlify_record(self, key: str, delta: dict) -> bool:
        """Set one uniquely named, immutable record. Return True on success."""
        fd, tmp = tempfile.mkstemp(prefix="netlify-state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(delta, handle, ensure_ascii=False, sort_keys=True)
            proc = self._netlify_run(
                ["blobs:set", self._netlify_store, key, "--input", tmp, "--force"]
            )
            if proc is not None and proc.returncode == 0:
                return True
            print("[state] netlify blob write failed, using local")
            return False
        except Exception as exc:
            print(f"[state] netlify blob write failed: {type(exc).__name__}")
            return False
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---------- loading ----------
    def load(self) -> dict:
        """Load and merge every immutable run record; fall back to local JSON."""
        self.data = _blank()
        if self.backend == "netlify" and self._netlify_load():
            self.prune(remote=True)
            return self.data
        opts = self._blob_options()
        if opts is not None:
            try:
                from vercel.api import session
                from vercel.blob import sync as blob

                records: list[dict] = []
                with session(service_options=[opts]):
                    for item in blob.query_blobs(prefix=RECORD_PREFIX):
                        try:
                            result = blob.get_blob(item.pathname, access="private")
                            payload = result.body
                            if isinstance(payload, (bytes, bytearray, memoryview)):
                                payload = bytes(payload).decode("utf-8")
                            record = json.loads(payload)
                            if isinstance(record, dict):
                                records.append(record)
                        except Exception as exc:
                            print(f"[state] skipping unreadable record "
                                  f"{getattr(item, 'pathname', '?')}: {exc}")
                            continue
                # Apply oldest-first so later runs win on shared article keys.
                for record in sorted(records, key=lambda r: str(r.get("at", ""))):
                    self._merge(record)
                self.prune(remote=True)
                return self.data
            except Exception as exc:
                print(f"[state] blob load failed, using local: {exc}")
        self._load_local()
        self.prune()
        return self.data

    def _load_local(self) -> None:
        self.data = _blank()
        if not self._local.exists():
            return
        try:
            loaded = json.loads(self._local.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._merge(loaded)
        except Exception as exc:
            print(f"[state] local load failed: {exc}")

    def _merge(self, record: dict) -> None:
        """Merge one metadata delta into in-memory state (idempotent on run id)."""
        if not isinstance(record, dict):
            return
        articles = self.data.setdefault("articles", {})
        if isinstance(record.get("articles"), dict):
            articles.update(record["articles"])
        images = self.data.setdefault("images", {})
        if isinstance(record.get("images"), dict):
            images.update(record["images"])
        reels = self.data.setdefault("reels", [])
        if isinstance(record.get("reels"), list):
            reels.extend(record["reels"])
        runs = self.data.setdefault("runs", [])
        seen_ids = {run.get("id") for run in runs if isinstance(run, dict)}
        for run in record.get("runs") or []:
            if not isinstance(run, dict):
                continue
            rid = run.get("id")
            if rid and rid not in seen_ids:
                runs.append(run)
                seen_ids.add(rid)

    # ---------- retention ----------
    def _delete_stale_remote_keys(self, keys: list[str], cutoff: datetime,
                                  protected: set[str], delete_fn) -> None:
        """Delete old immutable remote records, skipping the newest/current one."""
        parsed = [(str(key), _key_time(key)) for key in keys if key]
        known = [stamp for _, stamp in parsed if stamp is not None]
        newest = max(known) if known else None
        for key, stamp in parsed:
            if stamp is None or stamp >= cutoff:
                continue
            if any(run_id and run_id in key for run_id in protected):
                continue
            if newest is not None and stamp == newest:
                continue
            try:
                if delete_fn(key):
                    print(f"[state] pruned remote record {key}")
            except Exception as exc:
                print(f"[state] remote prune skipped {key}: {type(exc).__name__}: {exc}")

    def _prune_remote(self, cutoff: datetime, protected: set[str]) -> None:
        """Best-effort pruning of the append-only remote record store."""
        if self.backend == "netlify":
            if not (self._has_netlify_creds() and self._netlify_cli() is not None):
                return
            try:
                proc = self._netlify_run([
                    "blobs:list", self._netlify_store, "--prefix", RECORD_PREFIX, "--json"
                ])
                if proc is None or proc.returncode != 0:
                    return

                def delete(key: str) -> bool:
                    result = self._netlify_run([
                        "blobs:delete", self._netlify_store, key, "--force"
                    ])
                    return result is not None and result.returncode == 0

                self._delete_stale_remote_keys(
                    self._netlify_keys(proc.stdout), cutoff, protected, delete)
            except Exception as exc:
                print(f"[state] netlify remote prune skipped: {type(exc).__name__}")
            return

        opts = self._blob_options()
        if opts is None:
            return
        try:
            from vercel.api import session
            from vercel.blob import sync as blob

            with session(service_options=[opts]):
                keys = [
                    getattr(item, "pathname", "")
                    for item in blob.query_blobs(prefix=RECORD_PREFIX)
                    if getattr(item, "pathname", "")
                ]
                self._delete_stale_remote_keys(
                    keys, cutoff, protected, lambda key: self._safe_blob_delete(blob, key))
        except Exception as exc:
            print(f"[state] blob remote prune skipped: {exc}")

    @staticmethod
    def _safe_blob_delete(blob, key: str) -> bool:
        try:
            blob.delete_blob(key)
            return True
        except Exception as exc:
            print(f"[state] blob prune skipped {key}: {exc}")
            return False

    def prune(self, max_age_days: int = STATE_TTL_DAYS, *, remote: bool = False,
              keep_recent: int = KEEP_RECENT_RUNS) -> dict:
        """Drop ledger entries older than the retention window.

        Called on load and before each write. The newest few runs and anything
        they reference are pinned, so the current run is never removed. Returns
        a summary and records ``pruned_at`` / ``last_prune`` in ``self.data``.
        Media bytes are never read or written — only metadata ages are checked.
        """
        try:
            days = int(max_age_days)
        except (TypeError, ValueError):
            days = STATE_TTL_DAYS
        now = _utc_now()
        cutoff = now - timedelta(days=max(0, days))
        data = self.data
        runs = [run for run in (data.get("runs") or []) if isinstance(run, dict)]

        def run_time(run: dict) -> datetime | None:
            return _parse_time(run.get("at")) or _parse_time(run.get("id"))

        # Pin the newest runs before anything is removed. Keeping at least one
        # guarantees the just-written/current run survives a prune.
        try:
            keep_n = max(1, int(keep_recent))
        except (TypeError, ValueError):
            keep_n = KEEP_RECENT_RUNS
        ordered = sorted(
            runs,
            key=lambda run: run_time(run) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        protected = {run.get("id") for run in ordered[:keep_n] if run.get("id")}

        # Learn ages and references from ALL runs first: entries without their
        # own timestamp inherit the age of the run that recorded them.
        article_times: dict[str, datetime | None] = {}
        image_times: dict[str, datetime | None] = {}
        reel_times: dict[str, datetime | None] = {}
        refs: dict[str, dict[str, set]] = {"articles": {}, "images": {}, "reels": {}}
        for run in runs:
            run_id, stamp = run.get("id"), run_time(run)
            for key in (run.get("articles") or {}):
                article_times.setdefault(str(key), stamp)
                refs["articles"].setdefault(str(key), set()).add(run_id)
            for key in (run.get("images") or {}):
                image_times.setdefault(str(key), stamp)
                refs["images"].setdefault(str(key), set()).add(run_id)
            for reel in run.get("reels") or []:
                reel_id = reel.get("id") if isinstance(reel, dict) else None
                if reel_id:
                    reel_times.setdefault(str(reel_id), stamp)
                    refs["reels"].setdefault(str(reel_id), set()).add(run_id)

        def pinned(key: str, kind: str) -> bool:
            return bool(refs[kind].get(key, set()) & protected)

        def expired(stamp: datetime | None, hint: Any) -> bool:
            stamp = stamp or _parse_time(hint)
            return stamp is not None and stamp < cutoff

        removed = {"runs": 0, "articles": 0, "images": 0, "reels": 0}

        kept_runs: list[dict] = []
        for run in runs:
            if run.get("id") in protected or not expired(run_time(run), run.get("id")):
                kept_runs.append(run)
            else:
                removed["runs"] += 1
        data["runs"] = kept_runs

        for kind, times in (("articles", article_times), ("images", image_times)):
            entries = data.get(kind)
            if isinstance(entries, dict):
                for key in list(entries):
                    if pinned(str(key), kind):
                        continue
                    stamp = _entry_time(entries[key]) or times.get(str(key))
                    if expired(stamp, key):
                        del entries[key]
                        removed[kind] += 1

        reels = data.get("reels")
        if isinstance(reels, list):
            kept_reels = []
            for reel in reels:
                reel_id = str(reel.get("id")) if isinstance(reel, dict) and reel.get("id") else ""
                if reel_id and pinned(reel_id, "reels"):
                    kept_reels.append(reel)
                    continue
                stamp = _entry_time(reel) or reel_times.get(reel_id)
                if expired(stamp, reel_id or None):
                    removed["reels"] += 1
                else:
                    kept_reels.append(reel)
            data["reels"] = kept_reels

        summary = {
            "at": now.isoformat(),
            "max_age_days": days,
            "removed": removed,
        }
        data[PRUNED_AT_KEY] = now.isoformat()
        data[LAST_PRUNE_KEY] = summary
        if remote:
            self._prune_remote(cutoff, protected)
        return summary

    # ---------- dedupe ----------
    def is_seen(self, candidate: dict) -> bool:
        """Seen by canonical URL key, normalized title key, or title Jaccard."""
        candidate = candidate or {}
        articles = self.data.get("articles") or {}
        if not isinstance(articles, dict):
            return False

        url_key = candidate.get("url_key")
        if url_key and url_key in articles:
            return True

        title_key = candidate.get("title_key")
        if title_key:
            for stored in articles.values():
                if isinstance(stored, dict) and stored.get("title_key") == title_key:
                    return True

        title = candidate.get("title") or ""
        if title:
            for stored in articles.values():
                if not isinstance(stored, dict):
                    continue
                if similarity(title, stored.get("title") or "") >= self._threshold:
                    return True
        return False

    def seen_image(self, digest: str) -> bool:
        """True when a perceptual-image hash is already in state."""
        if not digest:
            return False
        images = self.data.get("images") or {}
        if isinstance(images, dict):
            if digest in images:
                return True
            for stored in images.values():
                if stored == digest or (
                    isinstance(stored, dict) and digest in stored.values()
                ):
                    return True
        elif isinstance(images, (list, tuple, set)):
            return digest in images
        return False

    # ---------- writing ----------
    def record_run(self, run: dict) -> None:
        """Append one immutable, uniquely named record; never overwrite media."""
        # Bound the in-memory ledger before each write; remote keys are pruned
        # on load, where they are already enumerated.
        self.prune()
        run = dict(run or {})
        now = _utc_now()
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        run.setdefault("id", f"{stamp}-{uuid.uuid4().hex[:6]}")
        run.setdefault("at", now.isoformat())
        delta = {
            "version": 1,
            "at": run["at"],
            "runs": [run],
            "articles": run.get("articles") or {},
            "images": run.get("images") or {},
            "reels": run.get("reels") or [],
        }
        delta = _metadata_only(delta)

        # Keep this process's view consistent regardless of the chosen backend.
        self._merge(delta)

        if (
            self.backend == "netlify"
            and self._has_netlify_creds()
            and self._netlify_cli() is not None
        ):
            # Unique suffix means a rerun always appends a new record rather
            # than overwriting an existing one (immutable audit trail).
            key = f"{RECORD_PREFIX}{stamp}-{uuid.uuid4().hex[:12]}.json"
            if self._netlify_record(key, delta):
                return
        opts = self._blob_options()
        if opts is not None:
            try:
                from vercel.api import session
                from vercel.blob import sync as blob

                # Unique suffix means a rerun always appends a new record rather
                # than overwriting an existing one (immutable audit trail).
                path = f"{RECORD_PREFIX}{stamp}-{uuid.uuid4().hex[:12]}.json"
                body = json.dumps(delta, ensure_ascii=False, sort_keys=True).encode("utf-8")
                with session(service_options=[opts]):
                    blob.put_blob(
                        path, body, access="private", content_type="application/json",
                        allow_overwrite=False,
                    )
                return
            except Exception as exc:
                print(f"[state] blob write failed, using local: {exc}")
        self._write_local()

    def _write_local(self) -> None:
        try:
            self._local.parent.mkdir(parents=True, exist_ok=True)
            self._local.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[state] local write failed: {exc}")
