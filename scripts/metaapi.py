"""Thin, guarded client for the Meta Marketing API (Graph v26.0).

Design rules (see SKILL.md):
- stdlib only (urllib), Python 3.9+.
- Every write is audited to audit/audit.jsonl (token redacted).
- Retries only on transient / throttle errors, never on create POSTs.
- Honors X-Business-Use-Case-Usage: stops when usage crosses the threshold.
- Paces writes (sleep between mutating calls).
- Optional appsecret_proof when APP_SECRET is configured.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API_VERSION = "v26.0"
BASE = f"https://graph.facebook.com/{API_VERSION}"
SKILL_DIR = Path(__file__).resolve().parent.parent
AUDIT_PATH = SKILL_DIR / "audit" / "audit.jsonl"

CHUNKED_VIDEO_BYTES = 40 * 1024 * 1024  # above this, upload_video uses the resumable protocol
TRANSIENT_CODES = {1, 2, 4, 17, 613, 80000, 80003, 80004, 80014}
ENV_CANDIDATES = [SKILL_DIR / ".env"]


class MetaError(Exception):
    def __init__(self, payload: dict, http_status: int = 0):
        self.payload = payload or {}
        err = self.payload.get("error", {}) if isinstance(self.payload, dict) else {}
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.user_msg = err.get("error_user_msg") or err.get("message")
        self.blame = err.get("error_data")
        self.is_transient = bool(err.get("is_transient"))
        self.http_status = http_status
        super().__init__(f"Meta API error {self.code}/{self.subcode}: {self.user_msg} blame={self.blame}")


def load_env() -> dict:
    """Read KEY=VALUE pairs from the skill's .env; environment variables override it."""
    env = {}
    for p in ENV_CANDIDATES:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
            env["_source"] = str(p)
            break
    for k in ("ACCESS_TOKEN", "APP_SECRET", "AD_ACCOUNT_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def redact(obj):
    """Recursively hide tokens in anything we log."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k in ("access_token", "appsecret_proof") else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"EAA[A-Za-z0-9]{20,}", "EAA<redacted>", obj)
    return obj


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----metaLaunch" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
        buf.write(str(v).encode("utf-8"))
        buf.write(b"\r\n")
    for k, src in files.items():
        # a path, or (filename, bytes) for an in-memory part such as a video chunk
        name, data = src if isinstance(src, tuple) else (Path(src).name, Path(src).read_bytes())
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class MetaClient:
    def __init__(self, token: str, app_secret: str | None = None, pace_seconds: float = 1.5,
                 usage_stop_pct: int = 75, audit_path: Path = AUDIT_PATH, dry_run: bool = False):
        if not token or not token.startswith("EAA"):
            raise SystemExit("ACCESS_TOKEN missing or malformed. Put it in the skill's .env (see SETUP.md, step 5).")
        self.token = token
        self.app_secret = app_secret or None
        self.pace_seconds = pace_seconds
        self.usage_stop_pct = usage_stop_pct
        self.audit_path = audit_path
        self.dry_run = dry_run
        self.last_usage = None
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ core
    def _auth(self) -> dict:
        auth = {"access_token": self.token}
        if self.app_secret:
            auth["appsecret_proof"] = hmac.new(self.app_secret.encode(), self.token.encode(), hashlib.sha256).hexdigest()
        return auth

    def _audit(self, record: dict):
        record = dict(record)
        record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redact(record), ensure_ascii=False) + "\n")

    def _check_usage(self, headers):
        raw = headers.get("X-Business-Use-Case-Usage") or headers.get("x-business-use-case-usage")
        if not raw:
            return
        try:
            usage = json.loads(raw)
        except Exception:
            return
        self.last_usage = usage
        worst = 0
        for _acct, entries in usage.items():
            for e in entries:
                for k in ("call_count", "total_cputime", "total_time"):
                    worst = max(worst, int(e.get(k, 0) or 0))
        if worst >= self.usage_stop_pct:
            wait = 0
            for _acct, entries in usage.items():
                for e in entries:
                    wait = max(wait, int(e.get("estimated_time_to_regain_access", 0) or 0))
            raise SystemExit(f"Rate-limit usage at {worst}% (threshold {self.usage_stop_pct}%). "
                             f"Stop and retry in ~{max(wait, 5)} minutes. Nothing else was sent.")

    def request(self, method: str, path: str, params: dict | None = None, files: dict | None = None,
                write: bool = False, retries: int = 3):
        params = dict(params or {})
        if not re.fullmatch(r"[A-Za-z0-9_/]+", path.lstrip("/")):
            # a bad path (e.g. "123 --flag") makes urllib raise InvalidURL with the token-bearing URL in the message
            raise SystemExit(f"invalid Graph path {path!r}: object ids and edges only")
        url = f"{BASE}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            body = None
            headers = {}
            auth = self._auth()
            if method == "GET":
                q = {**params, **auth}
                full = url + "?" + urllib.parse.urlencode(q)
                req = urllib.request.Request(full, method="GET")
            else:
                if files:
                    body, ctype = _multipart({**params, **auth}, files)
                    headers["Content-Type"] = ctype
                else:
                    body = urllib.parse.urlencode({**params, **auth}).encode()
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    text = resp.read().decode("utf-8")
                    self._check_usage(resp.headers)
                    data = json.loads(text) if text else {}
                    if write:
                        self._audit({"method": method, "path": path, "params": params,
                                     "files": list((files or {}).keys()), "response": data})
                        time.sleep(self.pace_seconds)
                    return data
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read().decode("utf-8"))
                except Exception:
                    payload = {"error": {"message": f"HTTP {e.code}", "code": e.code}}
                err = MetaError(payload, e.code)
                if write:
                    self._audit({"method": method, "path": path, "params": params,
                                 "files": list((files or {}).keys()), "error": payload})
                transient = err.is_transient or (err.code in TRANSIENT_CODES)
                # Never blindly re-POST a create: a timeout may have created the object.
                if transient and attempt < retries and not (write and method == "POST" and e.code >= 500):
                    time.sleep(min(60, 3 * attempt))
                    continue
                raise err
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < retries and not write:
                    time.sleep(3 * attempt)
                    continue
                raise SystemExit(f"Network error talking to Meta: {e}. "
                                 + ("A write may or may not have been applied; check the ledger/status before re-running." if write else ""))

    def get(self, path: str, **params):
        return self.request("GET", path, params)

    def post(self, path: str, params: dict, files: dict | None = None):
        return self.request("POST", path, params, files=files, write=True)

    # --------------------------------------------------------------- helpers
    def validate(self, edge: str, params: dict) -> dict:
        """execution_options=validate_only: nothing is created."""
        p = dict(params)
        p["execution_options"] = json.dumps(["validate_only"])
        return self.request("POST", edge, p, write=False)

    def upload_image(self, act: str, path: str) -> dict:
        data = self.post(f"act_{act}/adimages", {}, files={"filename": path})
        images = data.get("images", {})
        first = next(iter(images.values()))
        return {"hash": first["hash"], "url": first.get("url")}

    def upload_video(self, act: str, path: str, title: str | None = None) -> str:
        params = {"title": title or Path(path).stem}
        if Path(path).stat().st_size > CHUNKED_VIDEO_BYTES:
            return self._upload_video_chunked(act, path, params)
        data = self.post(f"act_{act}/advideos", params, files={"source": path})
        return data.get("id") or data.get("video_id")

    def _upload_video_chunked(self, act: str, path: str, params: dict) -> str:
        """Resumable upload (upload_phase start/transfer/finish). One POST of a 100 MB+ file breaks the pipe
        (2026-10-18, 109 MB). start/finish are audited writes; a transfer re-sent at the same offset is safe,
        so transfers go through the read path and get its network retries."""
        edge, size = f"act_{act}/advideos", Path(path).stat().st_size
        s = self.post(edge, {"upload_phase": "start", "file_size": size})
        session, vid = s["upload_session_id"], s["video_id"]
        start, end = int(s["start_offset"]), int(s["end_offset"])
        with open(path, "rb") as f:
            while start < end:
                f.seek(start)
                chunk = f.read(end - start)
                r = self.request("POST", edge, {"upload_phase": "transfer", "upload_session_id": session,
                                                "start_offset": start},
                                 files={"video_file_chunk": (f"chunk{start}", chunk)}, retries=5)
                start, end = int(r["start_offset"]), int(r["end_offset"])
        self.post(edge, {"upload_phase": "finish", "upload_session_id": session, **params})
        return vid

    def wait_video(self, video_id: str, timeout: int = 900) -> str:
        t0 = time.time()
        while True:
            d = self.get(video_id, fields="status")
            status = (d.get("status") or {}).get("video_status")
            if status == "ready":
                return status
            if status == "error":
                raise SystemExit(f"Video {video_id} failed processing: {d}")
            if time.time() - t0 > timeout:
                raise SystemExit(f"Video {video_id} still '{status}' after {timeout}s")
            time.sleep(10)
