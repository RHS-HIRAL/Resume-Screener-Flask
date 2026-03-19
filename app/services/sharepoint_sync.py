"""
app/services/sharepoint_sync.py
Shared auth, data models, SharePoint manager, and the top-level
run_sync() orchestrator that drives all three pipelines and sends
a Teams notification with the combined results.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Dict, Any, Optional
from urllib.parse import quote

import msal
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config

logger = logging.getLogger("sharepoint_sync")

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════


class GraphAuthProvider:
    SCOPES = ["https://graph.microsoft.com/.default"]
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self):
        self._app = msal.ConfidentialClientApplication(
            client_id=Config.AZURE_CLIENT_ID,
            client_credential=Config.AZURE_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{Config.AZURE_TENANT_ID}",
        )

    def get_access_token(self) -> str:
        result = self._app.acquire_token_silent(self.SCOPES, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=self.SCOPES)
        if "access_token" in result:
            return result["access_token"]
        raise RuntimeError(result.get("error_description", "Token acquisition failed"))

    def get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
        }


# ══════════════════════════════════════════════════════════════════════════════
#  DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

SUBJECT_PATTERN = re.compile(
    r"for the position:\s*(.+?)\s*\[(\w+)\]\s*$", re.IGNORECASE
)
JOB_ID_BRACKET = re.compile(r"\[(\w+)\]\s*$")
FIELD_PATTERNS = {
    "job_opening": re.compile(r"Job\s*Opening\s*:\s*(.+)", re.IGNORECASE),
    "name": re.compile(r"Name\s*:\s*(.+)", re.IGNORECASE),
    "email": re.compile(r"Email\s*:\s*(\S+@\S+\.\S+)", re.IGNORECASE),
    "phone": re.compile(r"Phone\s*:\s*([\d\s\+\-().]+)", re.IGNORECASE),
    "resume_url": re.compile(r"Resume\s*:\s*(https?://\S+)", re.IGNORECASE),
}


@dataclass
class CandidateInfo:
    name: str = ""
    email: str = ""
    phone: str = ""
    job_role: str = ""
    job_id: str = ""
    resume_url: str = ""
    attachments: list = field(default_factory=list)
    source_email_id: str = ""
    source_subject: str = ""
    received_datetime: str = ""

    @property
    def safe_name(self):
        cleaned = re.sub(r"[^\w\s\-]", "", self.name).strip()
        return "_".join(w.capitalize() for w in cleaned.split()) or "Unknown"

    @property
    def safe_job_id(self):
        return re.sub(r"[^\w\-]", "", self.job_id).strip() or "NO-ID"

    @property
    def safe_job_role(self):
        cleaned = re.sub(r"[^\w\s\-]", "", self.job_role).strip()
        return "_".join(cleaned.split())[:80] or "General"


@dataclass
class JobDescription:
    slug: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    job_type: str = ""
    department: str = ""
    shifts: str = ""
    experience: str = ""
    job_category: str = ""
    employment_type: str = ""
    sections: list = field(default_factory=list)
    scraped_date: str = ""

    @property
    def safe_slug(self):
        return re.sub(r"[^\w\-]", "", self.slug).strip() or "unknown"

    @property
    def pdf_filename(self):
        return f"JD_{self.safe_slug}.pdf"


# ══════════════════════════════════════════════════════════════════════════════
#  FIELD MAPS
# ══════════════════════════════════════════════════════════════════════════════

RESUME_FIELD_MAP = {
    "CandidateName": "CandidateName",
    "CandidateEmail": "CandidateEmail",
    "CandidatePhone": "CandidatePhone",
    "JobID": "JobID",
    "JobRole": "JobRole",
    "SourceEmailID": "SourceEmailID",
    "Source": "Source",
}

JD_FIELD_MAP = {
    "JDTitle": "JDTitle",
    "JDLocation": "JDLocation",
    "JDJobType": "JDJobType",
    "JDDepartment": "JDDepartment",
    "JDExperience": "JDExperience",
    "JDJobCategory": "JDJobCategory",
    "JDScrapedDate": "JDScrapedDate",
    "JDSourceURL": "JDSourceURL",
    "Title": "Title",
}

# ══════════════════════════════════════════════════════════════════════════════
#  SHAREPOINT MANAGER  (unified for resumes + JDs)
# ══════════════════════════════════════════════════════════════════════════════


class SyncSharePointManager:
    def __init__(self, headers: dict):
        self.base = "https://graph.microsoft.com/v1.0"
        self._site_id = None
        self._drive_id = None
        self._ensured_folders: set = set()
        self._existing_jd_pdfs = None

        # Connection Pooling & Retry Adapter
        self.session = requests.Session()
        self.session.headers.update(headers)

        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # DRY Helper for Graph API calls
    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return self.session.request(method, url, **kwargs)

    def _get_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        domain = Config.SHAREPOINT_SITE_DOMAIN
        path = (Config.SHAREPOINT_SITE_PATH or "").strip("/")
        resp = self._request("GET", f"{self.base}/sites/{domain}:/{path}")
        resp.raise_for_status()
        self._site_id = resp.json()["id"]
        return self._site_id

    def _get_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        resp = self._request("GET", f"{self.base}/sites/{self._get_site_id()}/drives")
        resp.raise_for_status()
        drives = resp.json().get("value", [])
        target = (Config.SHAREPOINT_DRIVE_NAME or "").lower()
        for d in drives:
            if d["name"].lower() == target:
                self._drive_id = d["id"]
                return self._drive_id
        self._drive_id = drives[0]["id"]
        return self._drive_id

    def _ensure_folder(self, drive_id: str, folder_path: str):
        if folder_path in self._ensured_folders:
            return
        current = ""
        for part in folder_path.strip("/").split("/"):
            current = f"{current}/{part}" if current else part
            if current in self._ensured_folders:
                continue

            check_url = f"{self.base}/drives/{drive_id}/root:/{quote(current)}"
            if self._request("GET", check_url, timeout=15).status_code == 404:
                parent = "/".join(current.split("/")[:-1])
                create_url = (
                    f"{self.base}/drives/{drive_id}/root:/{quote(parent)}:/children"
                    if parent
                    else f"{self.base}/drives/{drive_id}/root/children"
                )
                self._request(
                    "POST",
                    create_url,
                    json={
                        "name": part,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                    timeout=15,
                )
            self._ensured_folders.add(current)

    def file_exists(self, remote_path: str) -> bool:
        drive_id = self._get_drive_id()
        try:
            resp = self._request(
                "GET",
                f"{self.base}/drives/{drive_id}/root:/{quote(remote_path.strip('/'))}",
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_file_metadata(self, folder: str, filename: str) -> Optional[dict]:
        drive_id = self._get_drive_id()
        encoded = quote(f"{folder}/{filename}")
        try:
            resp = self._request(
                "GET",
                f"{self.base}/drives/{drive_id}/root:/{encoded}?$expand=listItem",
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json().get("listItem", {}).get("fields", {})
        except Exception:
            pass
        return None

    def _set_metadata(
        self, drive_id: str, item_id: str, metadata: dict, field_map: dict
    ):
        if not item_id or item_id == "resumable_upload_complete":
            return
        fields = {field_map[k]: v for k, v in metadata.items() if k in field_map and v}
        if fields:
            self._request(
                "PATCH",
                f"{self.base}/drives/{drive_id}/items/{item_id}/listItem/fields",
                json=fields,
            )

    def _content_type(self, filename: str) -> str:
        ext = filename.lower().rsplit(".", 1)[-1]
        return {
            "pdf": "application/pdf",
            "txt": "text/plain; charset=utf-8",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(ext, "application/octet-stream")

    def _simple_upload(
        self, drive_id: str, folder: str, filename: str, file_path: str
    ) -> dict:
        url = f"{self.base}/drives/{drive_id}/root:/{quote(f'{folder}/{filename}')}:/content"
        headers = {"Content-Type": self._content_type(filename)}
        with open(file_path, "rb") as f:
            resp = self._request("PUT", url, headers=headers, data=f, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _upload_content_bytes(self, folder: str, filename: str, content: bytes) -> dict:
        drive_id = self._get_drive_id()
        self._ensure_folder(drive_id, folder)
        url = f"{self.base}/drives/{drive_id}/root:/{quote(f'{folder}/{filename}')}:/content"
        headers = {"Content-Type": self._content_type(filename)}
        resp = self._request("PUT", url, headers=headers, data=content, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def upload_resume(
        self, file_path: str, target_filename: str, subfolder: str, metadata: dict
    ) -> dict:
        drive_id = self._get_drive_id()
        full_folder = f"{Config.SHAREPOINT_JOBS_FOLDER.strip('/')}/{subfolder}"
        self._ensure_folder(drive_id, full_folder)
        item = self._simple_upload(drive_id, full_folder, target_filename, file_path)
        self._set_metadata(drive_id, item.get("id", ""), metadata, RESUME_FIELD_MAP)
        return item

    def update_match_score(self, item_id: str, score: int):
        drive_id = self._get_drive_id()
        self._request(
            "PATCH",
            f"{self.base}/drives/{drive_id}/items/{item_id}/listItem/fields",
            json={"MatchScore": score},
        )

    def upload_text_file(
        self, local_path, remote_path: str, skip_existing: bool = True
    ) -> bool:
        if skip_existing and self.file_exists(remote_path):
            return False
        drive_id = self._get_drive_id()
        parent = "/".join(remote_path.strip("/").split("/")[:-1])
        if parent:
            self._ensure_folder(drive_id, parent)

        url = f"{self.base}/drives/{drive_id}/root:/{quote(remote_path.strip('/'))}:/content"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        with open(local_path, "rb") as f:
            resp = self._request("PUT", url, headers=headers, data=f, timeout=60)
        return resp.status_code in (200, 201)

    # Yields paginated folders efficiently
    def list_subfolders(self, folder_path: str) -> Iterator[Dict[str, Any]]:
        drive_id = self._get_drive_id()
        encoded = quote(folder_path.strip("/"), safe="/")
        url = f"{self.base}/drives/{drive_id}/root:/{encoded}:/children?$select=id,name,file,folder&$top=999"

        while url:
            resp = self._request("GET", url)
            if not resp.ok:
                break
            data = resp.json()
            for item in data.get("value", []):
                if "folder" in item:
                    yield {"name": item["name"], "id": item.get("id", "")}
            url = data.get("@odata.nextLink")

    # Yields paginated files efficiently
    def list_files(
        self, folder_path: str, extensions: tuple = ()
    ) -> Iterator[Dict[str, Any]]:
        drive_id = self._get_drive_id()
        encoded = quote(folder_path.strip("/"), safe="/")
        url = f"{self.base}/drives/{drive_id}/root:/{encoded}:/children?$select=id,name,file,folder,@microsoft.graph.downloadUrl&$top=999"

        while url:
            resp = self._request("GET", url)
            if not resp.ok:
                break
            data = resp.json()
            for item in data.get("value", []):
                if "file" not in item:
                    continue
                name = item.get("name", "")
                if extensions and not name.lower().endswith(extensions):
                    continue
                yield {
                    "id": item.get("id", ""),
                    "name": name,
                    "download_url": item.get("@microsoft.graph.downloadUrl", ""),
                }
            url = data.get("@odata.nextLink")

    def download_file_by_url(self, download_url: str, dest_path) -> bool:
        try:
            with self.session.get(download_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error("Download failed: %s", e)
            return False

    def download_file(self, item_id: str, dest_path) -> bool:
        drive_id = self._get_drive_id()
        try:
            resp = self._request(
                "GET",
                f"{self.base}/drives/{drive_id}/items/{item_id}/content",
                timeout=60,
                allow_redirects=True,
            )
            resp.raise_for_status()
            dest_path.write_bytes(resp.content)
            return True
        except Exception as e:
            logger.error("Download failed for %s: %s", item_id, e)
            return False

    # ── JD-specific ────────────────────────────────────────────────────────

    def jd_pdf_exists(self, filename: str) -> bool:
        """Checks if a JD PDF exists in memory cache to avoid O(N) network calls."""
        if getattr(self, "_existing_jd_pdfs", None) is None:
            self._existing_jd_pdfs = self._list_existing_jd_pdfs()
        return filename.lower() in self._existing_jd_pdfs

    def _list_existing_jd_pdfs(self) -> set:
        try:
            drive_id = self._get_drive_id()
            folder = Config.SHAREPOINT_JOBS_FOLDER.strip("/")
            url = (
                f"{self.base}/drives/{drive_id}/root:/{quote(folder)}:/children"
                f"?$select=name&$top=1000"
            )
            names: set = set()
            while url:
                resp = self._request("GET", url)

                if resp.status_code == 404:
                    return set()
                resp.raise_for_status()
                data = resp.json()
                for item in data.get("value", []):
                    names.add(item["name"].lower())
                url = data.get("@odata.nextLink")
            return names
        except Exception as e:
            logger.warning("Could not list existing JD PDFs: %s", e)
            return set()

    def upload_jd_pdf(
        self, file_path: str, target_filename: str, metadata: dict = None
    ) -> dict:
        drive_id = self._get_drive_id()
        folder = Config.SHAREPOINT_JOBS_FOLDER.strip("/")
        self._ensure_folder(drive_id, folder)
        item = self._simple_upload(drive_id, folder, target_filename, file_path)
        if metadata:
            self._set_metadata(drive_id, item.get("id", ""), metadata, JD_FIELD_MAP)

        # Update cache if it exists
        if getattr(self, "_existing_jd_pdfs", None) is not None:
            self._existing_jd_pdfs.add(target_filename.lower())
        return item

    def upload_jd_text(
        self,
        text_content: str,
        filename: str,
        metadata: dict = None,
        skip_existing: bool = True,
    ):
        folder = Config.SHAREPOINT_JOBS_FOLDER.strip("/")
        remote_path = f"{folder}/{filename}"
        if skip_existing and self.file_exists(remote_path):
            return None
        item = self._upload_content_bytes(
            folder, filename, text_content.encode("utf-8")
        )
        if metadata:
            drive_id = self._get_drive_id()
            self._set_metadata(drive_id, item.get("id", ""), metadata, JD_FIELD_MAP)
        return item

    def find_item_by_path(self, remote_path: str) -> Optional[dict]:
        drive_id = self._get_drive_id()
        encoded = quote(remote_path.strip("/"))
        try:
            resp = self._request(
                "GET", f"{self.base}/drives/{drive_id}/root:/{encoded}", timeout=15
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def get_folder_files_metadata(self, folder_path: str) -> dict:
        """
        Fetches all files in a specific SharePoint folder to build a metadata cache.
        Returns a dictionary mapping: { 'filename.ext': 'source_email_id' }
        """
        drive_id = self._get_drive_id()
        encoded = quote(folder_path.strip("/"), safe="/")
        url = f"{self.base}/drives/{drive_id}/root:/{encoded}:/children?$expand=listItem&$top=999"

        file_metadata = {}
        try:
            while url:
                resp = self._request("GET", url)
                if not resp.ok:
                    break
                data = resp.json()

                for item in data.get("value", []):
                    if "file" in item:
                        filename = item.get("name")
                        source_email_id = (
                            item.get("listItem", {})
                            .get("fields", {})
                            .get("SourceEmailID")
                        )
                        if filename and source_email_id:
                            file_metadata[filename] = source_email_id

                url = data.get("@odata.nextLink")
        except Exception as e:
            logger.error(
                "Failed to fetch folder metadata cache for %s: %s", folder_path, e
            )

        return file_metadata


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def _unique_base_path(directory: str, candidate: CandidateInfo) -> str:
    base = f"{candidate.safe_name}_{candidate.safe_job_id}"
    h = hashlib.md5(f"{base}{datetime.now().isoformat()}".encode()).hexdigest()[:6]
    return os.path.join(directory, f"{base}_{h}")


def _save_last_sync():
    sync_file = Config.SYNC_LAST_SYNC_FILE
    os.makedirs(
        os.path.dirname(sync_file) if os.path.dirname(sync_file) else ".", exist_ok=True
    )
    with open(sync_file, "w") as f:
        json.dump({"last_sync": datetime.utcnow().isoformat() + "Z"}, f)


def get_last_sync_time() -> str | None:
    try:
        with open(Config.SYNC_LAST_SYNC_FILE) as f:
            return json.load(f).get("last_sync")
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT — runs all pipelines + Teams notification
# ══════════════════════════════════════════════════════════════════════════════


def run_sync() -> dict:
    """
    Run the full sync pipeline:
      1. Email → resume upload
      2. Text extraction (with corrupted-file handling)
      3. JD scraping → PDF + TXT upload
      4. Send Teams notification summary
    Returns a combined results dict.
    """
    from app.services.resume_sync import (
        run_email_fetch_pipeline,
        run_text_extraction_pipeline,
    )
    from app.services.jd_sync import run_jd_pipeline
    from app.services.teams_notification import send_teams_notification

    logger.info("╔══════════════════════════════════════════════════════╗")
    logger.info("║        FULL SYNC PIPELINE STARTED                  ║")
    logger.info("╚══════════════════════════════════════════════════════╝")

    try:
        auth = GraphAuthProvider()
        auth.get_access_token()
        logger.info("✅ Microsoft Graph authentication successful.")
    except Exception as e:
        raise RuntimeError(f"Authentication failed: {e}") from e

    combined = {}
    errors = []

    try:
        p1 = run_email_fetch_pipeline(auth)
        combined["email_fetch"] = p1
    except Exception as e:
        logger.error("Pipeline 1 (email fetch) failed: %s", e, exc_info=True)
        errors.append(f"Email fetch: {e}")

    try:
        p2 = run_text_extraction_pipeline(auth)
        combined["text_extraction"] = p2
    except Exception as e:
        logger.error("Pipeline 2 (text extraction) failed: %s", e, exc_info=True)
        errors.append(f"Text extraction: {e}")

    try:
        p3 = run_jd_pipeline(auth)
        combined["jd_sync"] = p3
    except Exception as e:
        logger.error("Pipeline 3 (JD sync) failed: %s", e, exc_info=True)
        errors.append(f"JD sync: {e}")

    if errors:
        combined["errors"] = errors

    _save_last_sync()

    try:
        send_teams_notification(combined)
    except Exception as e:
        logger.error("Teams notification failed: %s", e)

    logger.info("╔══════════════════════════════════════════════════════╗")
    logger.info("║        FULL SYNC PIPELINE COMPLETE                 ║")
    logger.info("╚══════════════════════════════════════════════════════╝")
    return combined
