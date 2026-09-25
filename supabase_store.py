"""Upload question images to Supabase Storage and hand back their public URLs.

Plain REST over requests rather than the supabase SDK: it is three calls, and this keeps the app's
dependencies to what is already installed.

    SUPABASE_URL          https://<project>.supabase.co
    SUPABASE_SERVICE_KEY  service role key - it can write to storage, so it stays in .env
    SUPABASE_BUCKET       bucket name (default "images")

An object is stored once under the id the export already gave it, so a second push finds it there
instead of uploading the bytes again.
"""

import os
from pathlib import Path

TIMEOUT = 60


def configured():
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def settings():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    return url, os.environ.get("SUPABASE_SERVICE_KEY") or "", os.environ.get("SUPABASE_BUCKET") or "images"


def public_url(dest, url=None, bucket=None):
    """Where the object can be read from, for a public bucket."""
    base, _key, buck = settings()
    return f"{url or base}/storage/v1/object/public/{bucket or buck}/{dest}"


def _headers(key, extra=None):
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    h.update(extra or {})
    return h


def check():
    """Can we reach the bucket and is it public? Read-only. Returns (ok, message)."""
    import requests

    if not configured():
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set - add them to .env")
    base, key, bucket = settings()
    try:
        r = requests.get(f"{base}/storage/v1/bucket/{bucket}", headers=_headers(key), timeout=TIMEOUT)
    except Exception as e:
        return False, f"cannot reach {base}: {e}"
    if r.status_code == 404:
        return False, f'bucket "{bucket}" does not exist in this project'
    if r.status_code in (401, 403):
        return False, "SUPABASE_SERVICE_KEY was refused - is it the service role key?"
    if not r.ok:
        return False, f"{r.status_code} {r.text[:120]}"
    info = r.json()
    if not info.get("public"):
        return True, (f'bucket "{bucket}" is private, so the stored URLs will not open without a signed '
                      f"link - make it public in Supabase, or serve the images through your own app")
    return True, f'bucket "{bucket}" is reachable and public'


def upload(path, dest, content_type="image/png", overwrite=False):
    """Put one file in the bucket. Returns "stored", "exists" or raises.
    Without overwrite an object already there is left alone, so re-running costs nothing."""
    import requests

    base, key, bucket = settings()
    endpoint = f"{base}/storage/v1/object/{bucket}/{dest}"
    headers = _headers(key, {"Content-Type": content_type,
                             "cache-control": "public, max-age=31536000",
                             "x-upsert": "true" if overwrite else "false"})
    r = requests.post(endpoint, data=Path(path).read_bytes(), headers=headers, timeout=TIMEOUT)
    if r.ok:
        return "stored"
    # Supabase answers 409, or 400 with "Duplicate", when the object is already there
    if r.status_code == 409 or "duplicate" in r.text.lower() or "already exists" in r.text.lower():
        return "exists"
    raise RuntimeError(f"upload of {dest} failed: {r.status_code} {r.text[:160]}")


def remove(dest):
    """Delete one object - used to clean up after a connection test."""
    import requests

    base, key, bucket = settings()
    r = requests.delete(f"{base}/storage/v1/object/{bucket}/{dest}", headers=_headers(key), timeout=TIMEOUT)
    return r.ok
