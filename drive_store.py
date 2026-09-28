"""Google Drive storage for OCR source PDFs.

The OCR app writes the same source-file identity that ``ingest`` expects: a PDF is filed under
``exam / subject / module / chapter`` and its Drive id is stored on ``ingest_documents``.
"""

import os
import io
from pathlib import Path


FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"
SCOPES = ["https://www.googleapis.com/auth/drive"]


def configured():
    """Whether all credentials required to file PDFs in the ingest Drive are present."""
    return all(os.environ.get(name) for name in (
        "DRIVE_ROOT_FOLDER_ID", "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN",
    ))


def _service():
    if not configured():
        raise RuntimeError(
            "Google Drive is not configured. Set DRIVE_ROOT_FOLDER_ID, GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN in .env."
        )
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as error:
        raise RuntimeError("Google Drive support is not installed. Run: pip install google-api-python-client google-auth") from error
    credentials = Credentials(
        token=None, refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"], scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _escape_query(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_or_create_folder(service, name, parent_id):
    query = (f"'{_escape_query(parent_id)}' in parents and name = '{_escape_query(name)}' "
             f"and mimeType = '{FOLDER_MIME}' and trashed = false")
    response = service.files().list(
        q=query, fields="files(id,name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    matches = response.get("files", [])
    if matches:
        return matches[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id,name", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def upload_pdf(pdf_path, file_name, *, exam=None, subject=None, module=None, chapter=None):
    """File ``pdf_path`` under the ingest path and return its immutable Drive file id."""
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise RuntimeError("Google Drive support is not installed. Run: pip install google-api-python-client google-auth") from error
    path = Path(pdf_path)
    if not path.is_file():
        raise RuntimeError(f"PDF source is missing: {path}")
    service = _service()
    parent_id = os.environ["DRIVE_ROOT_FOLDER_ID"]
    for name in (exam, subject, module, chapter):
        if isinstance(name, str) and name.strip():
            parent_id = _find_or_create_folder(service, name.strip(), parent_id)
    media = MediaFileUpload(str(path), mimetype=PDF_MIME, resumable=True)
    uploaded = service.files().create(
        body={"name": file_name, "parents": [parent_id]}, media_body=media,
        fields="id,name,mimeType,size,modifiedTime", supportsAllDrives=True,
    ).execute()
    return uploaded["id"]


def download_pdf(file_id, destination):
    """Download a Drive source into a caller-provided temporary path."""
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as error:
        raise RuntimeError("Google Drive support is not installed. Run: pip install google-api-python-client google-auth") from error
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    service = _service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with target.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return target
