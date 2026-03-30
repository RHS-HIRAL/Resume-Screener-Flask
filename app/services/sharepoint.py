# app/services/sharepoint.py — Handles Microsoft Graph API interactions, file sync, and MSAL authentication.

import re
import io
import os
import requests
import msal
import pandas as pd
import concurrent.futures
from pathlib import Path
from config import Config


class SharePointMatchScoreUpdater:
    """
    Finds a resume file uploaded to SharePoint and writes the MatchScore metadata.
    Handles browsing, downloading, and uploading files synchronously with session pooling.
    """

    GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    SCOPES = ["https://graph.microsoft.com/.default"]

    def __init__(self):
        # 1. Centralized Configuration
        self._msal_app = msal.ConfidentialClientApplication(
            client_id=Config.AZURE_CLIENT_ID,
            client_credential=Config.AZURE_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{Config.AZURE_TENANT_ID}",
        )
        self.site_domain = Config.SHAREPOINT_SITE_DOMAIN
        self.site_path = Config.SHAREPOINT_SITE_PATH.strip("/")
        self.drive_name = Config.SHAREPOINT_DRIVE_NAME

        self._site_id: str | None = None
        self._drive_id: str | None = None

        # 2. HTTP Session Pooling (Reuses TCP connections for all requests)
        self.session = requests.Session()

    def _get_headers(self) -> dict:
        """Acquire a token and return headers. MSAL caches the token automatically."""
        result = self._msal_app.acquire_token_silent(self.SCOPES, account=None)
        if not result:
            result = self._msal_app.acquire_token_for_client(scopes=self.SCOPES)
        if "access_token" not in result:
            raise RuntimeError(
                result.get("error_description", "Token acquisition failed")
            )
        return {
            "Authorization": f"Bearer {result['access_token']}",
            "Content-Type": "application/json",
        }

    def _get_site_id(self) -> str:
        if self._site_id:
            return self._site_id
        url = f"{self.GRAPH_BASE}/sites/{self.site_domain}:/{self.site_path}"
        resp = self.session.get(url, headers=self._get_headers(), timeout=30)
        resp.raise_for_status()
        self._site_id = resp.json()["id"]
        return self._site_id

    def _get_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        site_id = self._get_site_id()
        url = f"{self.GRAPH_BASE}/sites/{site_id}/drives"
        resp = self.session.get(url, headers=self._get_headers(), timeout=30)
        resp.raise_for_status()

        drives = resp.json().get("value", [])
        for d in drives:
            if d["name"].lower() == self.drive_name.lower():
                self._drive_id = d["id"]
                return self._drive_id
        if drives:
            self._drive_id = drives[0]["id"]
            return self._drive_id
        raise RuntimeError(
            f"No drives found on SharePoint site '{self.site_domain}/{self.site_path}'"
        )

    # ── Folder browsing ──────────────────────────────────────────────────────

    def _list_folder_children(
        self, folder_path: str, include_fields: bool = False
    ) -> list[dict]:
        from urllib.parse import quote as _quote

        drive_id = self._get_drive_id()
        folder_stripped = folder_path.strip("/")
        expand = "&$expand=listItem($expand=fields)" if include_fields else ""

        if not folder_stripped:
            url = f"{self.GRAPH_BASE}/drives/{drive_id}/root/children?$select=id,name,file,folder&$top=999{expand}"
        else:
            encoded = _quote(folder_stripped, safe="/")
            url = f"{self.GRAPH_BASE}/drives/{drive_id}/root:/{encoded}:/children?$select=id,name,file,folder&$top=999{expand}"

        items = []
        headers = self._get_headers()
        while url:
            resp = self.session.get(url, headers=headers, timeout=30)
            if not resp.ok:
                print(
                    f"[ERROR] SharePoint folder list failed: {resp.status_code} - {resp.text}"
                )
                break
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")

        return items

    def list_resumes_grouped(self) -> dict[str, list[dict]]:
        """
        3. Concurrent Folder Browsing: Return ALL role subfolders and their resume files.
        Fetches subfolder contents in parallel, drastically reducing load times.
        Excludes JD files (prefixed with 'JD_') from the resume lists.
        Filters to return ONLY PDF files, but attaches the corresponding .txt file ID for LLM scoring.
        """
        subfolders = [
            item
            for item in self._list_folder_children(Config.SHAREPOINT_JOBS_FOLDER)
            if "folder" in item
        ]
        groups = {}

        def fetch_folder_contents(sf):
            sf_path = f"{Config.SHAREPOINT_JOBS_FOLDER}/{sf['name']}"
            children = self._list_folder_children(sf_path, include_fields=True)

            # 1. Build a map of base filenames to their .txt file IDs
            txt_map = {}
            for f in children:
                if "file" in f and f["name"].lower().endswith(".txt"):
                    base_name = os.path.splitext(f["name"])[0].lower()
                    txt_map[base_name] = f["id"]

            files = []
            for f in children:
                if "file" not in f:
                    continue
                name = f["name"]
                name_lower = name.lower()
                # Skip JD files
                if name_lower.startswith("jd_"):
                    continue
                # 2. ONLY list PDF files
                if not (
                    name_lower.endswith(".pdf")
                    or name_lower.endswith("doc")
                    or name_lower.endswith("docx")
                ):
                    continue
                base_name = os.path.splitext(name)[0].lower()

                fields = (f.get("listItem") or {}).get("fields", {})
                match_score = fields.get("MatchScore") or 0

                # 3. Combine the PDF data with the corresponding TXT id
                files.append(
                    {
                        "id": f["id"],  # PDF ID -> Use this to update MatchScore
                        "name": name,  # PDF Name -> Show this in the UI
                        "match_score": match_score,
                        "source": fields.get("Source", ""),
                        "txt_id": txt_map.get(
                            base_name
                        ),  # TXT ID -> Use this to download text for LLM
                    }
                )
            return sf["name"], files

        # Fetch all subfolders simultaneously
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_folder_contents, sf) for sf in subfolders]
            for future in concurrent.futures.as_completed(futures):
                folder_name, folder_files = future.result()
                groups[folder_name] = folder_files

        return groups

    def list_jd_files(self) -> list[dict]:
        """List all JD files (prefixed with 'JD_') across all role subfolders."""
        subfolders = [
            item
            for item in self._list_folder_children(Config.SHAREPOINT_JOBS_FOLDER)
            if "folder" in item
        ]
        all_jds = []

        def fetch_jds(sf):
            sf_path = f"{Config.SHAREPOINT_JOBS_FOLDER}/{sf['name']}"
            children = self._list_folder_children(sf_path)
            jds = []
            for f in children:
                if "file" not in f:
                    continue
                name_lower = f["name"].lower()
                if name_lower.startswith("jd_") and name_lower.endswith(".txt"):
                    jds.append({"id": f["id"], "name": f["name"]})
            return jds

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_jds, sf) for sf in subfolders]
            for future in concurrent.futures.as_completed(futures):
                all_jds.extend(future.result())

        return all_jds

    def download_text_content(self, item_id: str) -> str:
        drive_id = self._get_drive_id()
        url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
        headers = self._get_headers()

        resp = self.session.get(url, headers=headers, timeout=60, allow_redirects=True)
        resp.raise_for_status()

        item_meta_url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
        meta_resp = self.session.get(item_meta_url, headers=headers)
        filename = meta_resp.json().get("name", "").lower()

        content = resp.content
        if filename.endswith(".pdf"):
            import PyPDF2

            reader = PyPDF2.PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif filename.endswith(".docx"):
            from docx import Document

            doc = Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        else:
            return content.decode("utf-8", errors="replace")

    def find_txt_version(self, folder_name: str, original_filename: str) -> dict | None:
        """Find the corresponding .txt file in the same job-role folder."""
        from pathlib import Path

        txt_folder_path = f"{Config.SHAREPOINT_JOBS_FOLDER}/{folder_name}"
        stem = Path(original_filename).stem
        target_name = f"{stem}.txt".lower()

        try:
            children = self._list_folder_children(txt_folder_path)
            for item in children:
                if "file" in item and item["name"].lower() == target_name:
                    return {"id": item["id"], "name": item["name"]}
        except Exception as e:
            print(
                f"[SP] Error finding txt version for {original_filename} in {folder_name}: {e}"
            )

        return None

    # ── File lookup & Metadata ───────────────────────────────────────────────

    def find_matching_items(self, filename: str, role_hint: str = "") -> list[dict]:
        from urllib.parse import quote as _quote

        drive_id = self._get_drive_id()
        stem = Path(filename).stem
        encoded_stem = _quote(stem, safe="")
        url = f"{self.GRAPH_BASE}/drives/{drive_id}/root/search(q='{encoded_stem}')"
        resp = self.session.get(url, headers=self._get_headers(), timeout=30)
        if not resp.ok:
            return []

        matches = []
        for item in resp.json().get("value", []):
            if "folder" in item:
                continue
            if item.get("name", "").lower() != filename.lower():
                continue
            parent_path = item.get("parentReference", {}).get("path", "") or ""
            matches.append(
                {"id": item["id"], "name": item["name"], "path": parent_path}
            )

        if len(matches) <= 1 or not role_hint:
            return matches

        role_tokens = [t.lower() for t in re.split(r"[\W_]+", role_hint) if len(t) > 2]

        def _score(m: dict) -> int:
            p = m["path"].lower()
            return sum(1 for t in role_tokens if t in p)

        ranked = sorted(matches, key=_score, reverse=True)
        top_score = _score(ranked[0])
        top_group = [m for m in ranked if _score(m) == top_score]
        return top_group if len(top_group) == 1 else ranked

    def get_item_fields(self, item_id: str) -> dict:
        """
        Fetch the SharePoint listItem fields for a given drive item ID.
        Returns the fields dict, or an empty dict on failure.
        Used to read the Source field before deciding whether to rename.
        """
        drive_id = self._get_drive_id()
        url = (
            f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
            "?$expand=listItem($expand=fields)"
        )
        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=30)
            if resp.ok:
                return (resp.json().get("listItem") or {}).get("fields", {})
        except Exception as e:
            print(f"[SP] get_item_fields failed for {item_id}: {e}")
        return {}

    def rename_item(self, item_id: str, new_name: str) -> tuple[str, str]:
        """
        Rename a SharePoint drive item by PATCHing its name property (no re-upload).

        Collision handling: if new_name already exists in the same folder, a counter
        is inserted before the job-ID suffix:
            John_Smith_9456.pdf  →  John_Smith_2_9456.pdf  →  John_Smith_3_9456.pdf

        The current item is excluded from the sibling check so we don't collide
        with ourselves.

        Returns:
            (final_name, "OK")   on success
            ("", error_message)  on failure
        """
        drive_id = self._get_drive_id()
        headers = self._get_headers()

        ext = Path(new_name).suffix  # e.g. ".pdf"
        stem = Path(new_name).stem  # e.g. "John_Smith_9456"

        # ── Fetch the item to get its parent folder ────────────────────────
        item_resp = self.session.get(
            f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}",
            headers=headers,
            timeout=30,
        )
        if not item_resp.ok:
            return "", f"Could not fetch item info: {item_resp.status_code}"

        parent_id = item_resp.json().get("parentReference", {}).get("id", "")

        # ── Collect sibling names to detect collisions ────────────────────
        existing_names: set = set()
        if parent_id:
            sib_url = (
                f"{self.GRAPH_BASE}/drives/{drive_id}/items/{parent_id}"
                "/children?$select=id,name&$top=999"
            )
            sib_resp = self.session.get(sib_url, headers=headers, timeout=30)
            if sib_resp.ok:
                existing_names = {
                    item["name"].lower()
                    for item in sib_resp.json().get("value", [])
                    if item.get("id") != item_id  # exclude the file being renamed
                }

        # ── Resolve a unique final name ────────────────────────────────────
        final_name = new_name
        if new_name.lower() in existing_names:
            # Split "John_Smith_9456" → name_part="John_Smith", job_id_part="9456"
            parts = stem.rsplit("_", 1)
            if len(parts) == 2:
                name_part, job_id_part = parts
                counter = 2
                while True:
                    candidate = f"{name_part}_{counter}_{job_id_part}{ext}"
                    if candidate.lower() not in existing_names:
                        final_name = candidate
                        break
                    counter += 1
            # If stem can't be split (no underscore), keep new_name as-is and
            # let SharePoint raise the conflict — unlikely given our naming scheme.

        # ── PATCH the item name ────────────────────────────────────────────
        patch_resp = self.session.patch(
            f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}",
            headers=headers,
            json={"name": final_name},
            timeout=30,
        )

        if patch_resp.ok:
            return final_name, "OK"
        return (
            "",
            f"Rename PATCH failed: {patch_resp.status_code} - {patch_resp.text[:200]}",
        )

    def push_metadata(
        self,
        filename: str,
        metadata: dict,
        role_hint: str = "",
        confirmed_item_id: str = "",
        overwrite: bool = False,
    ) -> tuple[str, str, list[dict]]:
        drive_id = self._get_drive_id()
        if confirmed_item_id:
            item_id = confirmed_item_id
        else:
            candidates = self.find_matching_items(filename, role_hint=role_hint)
            if not candidates:
                return (
                    "NOT_FOUND",
                    f"File **{filename}** not found in SharePoint.",
                    [],
                )
            if len(candidates) > 1:
                return ("NEEDS_CONFIRM", "Multiple matches found.", candidates)
            item_id = candidates[0]["id"]

        # Fetch existing fields before patching
        headers = self._get_headers()
        existing_fields = {}
        try:
            get_url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}?$expand=listItem($expand=fields)"
            get_resp = self.session.get(get_url, headers=headers, timeout=30)
            if get_resp.ok:
                existing_fields = (get_resp.json().get("listItem") or {}).get(
                    "fields", {}
                )
        except Exception as e:
            print(f"[SP] Could not fetch existing fields for {filename}: {e}")

        # Construct safe metadata payload.
        # MatchScore and ScreenedWith are always overwritten so re-screens stay accurate.
        final_metadata = {}
        for key, value in metadata.items():
            if (
                overwrite
                or key in ("MatchScore", "ScreenedWith")
                or not existing_fields.get(key)
                or existing_fields.get(key) == "Unknown"
                or str(existing_fields.get(key)).strip() == ""
            ):
                final_metadata[key] = value

        if not final_metadata:
            return ("OK", f"No new metadata to patch for `{filename}`.", [])

        url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}/listItem/fields"
        resp = self.session.patch(url, headers=headers, json=final_metadata, timeout=30)
        if resp.status_code == 200:
            return ("OK", f"Metadata updated successfully for `{filename}`.", [])
        return ("ERROR", f"SharePoint Error {resp.status_code}: {resp.text[:200]}", [])

    # ── 4. Removed Async Anti-Patterns (Now fully synchronous) ───────────────

    def ensure_folder_exists(self, folder_path: str) -> str:
        """Recursively check if a folder exists in SharePoint and creates it if not."""
        drive_id = self._get_drive_id()
        parts = [p for p in folder_path.split("/") if p]
        parent_id = "root"
        headers = self._get_headers()

        current_path = ""
        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{parent_id}/children?$filter=name eq '{part}'"
            resp = self.session.get(url, headers=headers, timeout=30)

            items = resp.json().get("value", [])
            if items:
                parent_id = items[0]["id"]
            else:
                create_url = (
                    f"{self.GRAPH_BASE}/drives/{drive_id}/items/{parent_id}/children"
                )
                payload = {
                    "name": part,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                }
                c_resp = self.session.post(
                    create_url, headers=headers, json=payload, timeout=30
                )
                if not c_resp.ok:
                    raise RuntimeError(
                        f"Failed to create folder '{part}': {c_resp.text}"
                    )
                parent_id = c_resp.json()["id"]
        return parent_id

    def upload_file(self, folder_path: str, filename: str, content: bytes | str) -> str:
        """Upload binary data or text to a specific folder path."""
        drive_id = self._get_drive_id()
        folder_id = self.ensure_folder_exists(folder_path)

        content_bytes = content.encode("utf-8") if isinstance(content, str) else content

        url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/{filename}:/content"
        headers = self._get_headers()
        headers["Content-Type"] = "application/octet-stream"

        resp = self.session.put(url, headers=headers, data=content_bytes, timeout=60)
        if not resp.ok:
            raise RuntimeError(
                f"SharePoint Upload failed for '{filename}': {resp.status_code} - {resp.text}"
            )
        return resp.json()["id"]

    def delete_file(self, filename: str, role_hint: str = "") -> tuple[str, str]:
        """
        Permanently delete a file from SharePoint by filename.
        Uses find_matching_items to locate the item, then calls
        DELETE /drives/{drive_id}/items/{item_id}.

        Returns: ("OK"|"NOT_FOUND"|"ERROR", message)
        """
        drive_id = self._get_drive_id()

        candidates = self.find_matching_items(filename, role_hint=role_hint)
        if not candidates:
            return ("NOT_FOUND", f"File '{filename}' not found in SharePoint.")

        # If multiple matches, attempt to delete all (resume may exist in
        # multiple folders — e.g. originals + text versions)
        errors = []
        deleted = 0
        for item in candidates:
            item_id = item["id"]
            url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
            resp = self.session.delete(url, headers=self._get_headers(), timeout=30)
            if resp.status_code in (204, 200):
                deleted += 1
                print(f"[SP DELETE] Deleted '{item['name']}' (id={item_id})")
            else:
                errors.append(f"item {item_id}: {resp.status_code}")

        if deleted == 0:
            return ("ERROR", f"SharePoint deletion failed: {'; '.join(errors)}")
        if errors:
            return (
                "PARTIAL",
                f"Deleted {deleted} copy/copies; errors on: {'; '.join(errors)}",
            )
        return ("OK", f"Deleted '{filename}' from SharePoint ({deleted} copy/copies).")

    # ── Excel / Forms Syncing ────────────────────────────────────────────────

    def refresh_excel_workbook(self, item_id: str) -> None:
        """
        Force Microsoft Forms to flush pending responses into the Excel workbook.

        Creates a persistent workbook session using the Graph API — equivalent to
        opening the file in a browser — which triggers the Forms backend to append
        any pending responses, then immediately closes the session.

        Using persistChanges=True creates a more "real" editing session that is
        more likely to trigger the Forms sync machinery.
        """
        drive_id = self._get_drive_id()
        headers = self._get_headers()
        session_url = (
            f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
            "/workbook/createSession"
        )
        try:
            resp = self.session.post(
                session_url,
                headers=headers,
                json={"persistChanges": True},
                timeout=30,
            )
            if resp.ok:
                session_id = resp.json().get("id", "")
                print(
                    f"[SP EXCEL] Workbook session opened for item {item_id} (persistent) — Forms flush triggered."
                )
                # Close the session immediately; the flush already happened
                if session_id:
                    close_url = (
                        f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item_id}"
                        "/workbook/closeSession"
                    )
                    close_headers = {**headers, "workbook-session-id": session_id}
                    try:
                        self.session.post(close_url, headers=close_headers, timeout=15)
                    except Exception:
                        pass  # Closing is best-effort; the flush already happened
            else:
                print(
                    f"[SP EXCEL] Could not open workbook session (status {resp.status_code}). Proceeding with download anyway."
                )
        except Exception as e:
            # Non-fatal: if session creation fails, fall back to downloading as-is
            print(f"[SP EXCEL] refresh_excel_workbook failed for item {item_id}: {e}")

    def get_onedrive_excel_rows(self, user_email: str, filename: str) -> list[dict]:
        """Search for an Excel file in a specific user's OneDrive and read its rows.

        Microsoft Forms only writes new responses to the linked Excel file when
        the workbook is actively opened/loaded by the Excel server engine.

        This method uses the Graph API workbook session approach instead of a
        headless browser — it creates a persistent editing session on the server,
        reads the worksheet data through that session (which forces the Excel
        engine to load and triggers Forms to flush pending responses), then
        parses the result.

        Flow:
        1. Find the Excel file in the user's OneDrive
        2. Create a persistent workbook session via Graph API
        3. Read the used range through the session (triggers Forms sync)
        4. Wait for Forms to flush, then read again
        5. Close the session
        6. Return parsed rows (falls back to raw .xlsx download if API read fails)
        """
        from urllib.parse import quote as _quote

        encoded_filename = _quote(filename, safe="")
        search_url = f"{self.GRAPH_BASE}/users/{user_email}/drive/root/search(q='{encoded_filename}')"
        resp = self.session.get(search_url, headers=self._get_headers(), timeout=30)
        if not resp.ok:
            return []

        results = resp.json().get("value", [])
        xlsx_results = [r for r in results if r["name"].lower().endswith(".xlsx")]
        if not xlsx_results:
            return []

        item = next(
            (
                r
                for r in xlsx_results
                if r["name"].lower() in [filename.lower(), (filename + ".xlsx").lower()]
            ),
            xlsx_results[0],
        )

        item_id = item["id"]
        item_id = item["id"]

        # Get the webUrl and force edit mode (Forms only syncs in edit mode, not view)
        web_url = ""
        try:
            meta_resp = self.session.get(
                f"{self.GRAPH_BASE}/users/{user_email}/drive/items/{item_id}",
                headers=self._get_headers(),
                timeout=30,
            )
            if meta_resp.ok:
                raw_url = meta_resp.json().get("webUrl", "")
                # Replace action=default with action=edit so Excel Online opens in
                # edit mode — Microsoft Forms only syncs pending responses when the
                # file is actively opened for editing by the file owner.
                if raw_url:
                    web_url = raw_url.replace("action=default", "action=edit")
                    if "action=" not in web_url:
                        web_url += "&action=edit"
        except Exception:
            pass

        # ── Run the workbook session + browser open + before/after download comparison ──
        return self._sync_and_read_via_workbook_session(user_email, item_id, web_url)

    def _sync_and_read_via_workbook_session(
        self, user_email: str, item_id: str, web_url: str = ""
    ) -> list[dict]:
        """
        Force Microsoft Forms to sync pending responses into the Excel workbook,
        then read the updated data.

        Strategy:
        1. Create a persistent workbook session (loads the Excel engine server-side)
        2. Confirm the session can read the file's worksheets and content
        3. Trigger a full workbook recalculation (fires any data-refresh connectors)
        4. Close the session (persists server-side changes)
        5. Download the raw .xlsx NOW as a baseline row-count snapshot
        6. Wait 25s for the Microsoft Forms connector to write pending responses
        7. Download the raw .xlsx AGAIN and compare row counts to detect sync
        8. Return the latest rows (always the freshest download)
        """
        import time

        base_url = f"{self.GRAPH_BASE}/users/{user_email}/drive/items/{item_id}"
        headers = self._get_headers()
        session_id = ""

        try:
            # ── Step 1: Create a persistent workbook session ──
            # print("[OneDrive EXCEL] Creating workbook session to load Excel engine...")
            session_resp = self.session.post(
                f"{base_url}/workbook/createSession",
                headers=headers,
                json={"persistChanges": True},
                timeout=30,
            )

            if not session_resp.ok:
                print(
                    f"[OneDrive EXCEL] createSession failed ({session_resp.status_code}): "
                    f"{session_resp.text[:300]}"
                )
                # Fall back to a single raw download without sync trigger
                return self._download_onedrive_excel_raw(user_email, item_id)

            session_id = session_resp.json().get("id", "")
            # print(f"[OneDrive EXCEL] ✅ Workbook session created (id={session_id[:40]}...)")
            wb_headers = {**headers, "workbook-session-id": session_id}

            # ── Step 2: List worksheets to confirm the file is open and accessible ──
            sheets_resp = self.session.get(
                f"{base_url}/workbook/worksheets",
                headers=wb_headers,
                timeout=30,
            )
            sheet_name = "Sheet1"
            if sheets_resp.ok:
                sheets = sheets_resp.json().get("value", [])
                if sheets:
                    sheet_name = sheets[0].get("name", "Sheet1")
                    # print(f"[OneDrive EXCEL] ✅ File is open in Excel engine. Found {len(sheets)} worksheet(s)")
                else:
                    print(
                        "[OneDrive EXCEL] ⚠️ No worksheets found — file may be empty or corrupt."
                    )
            else:
                print(
                    f"[OneDrive EXCEL] ⚠️ Could not list worksheets ({sheets_resp.status_code}) "
                    f"— file may not have loaded properly in the engine."
                )

            # ── Step 3: Read used range to confirm the content is accessible ──
            from urllib.parse import quote as _q

            encoded_sheet = _q(sheet_name, safe="")
            used_range_url = (
                f"{base_url}/workbook/worksheets('{encoded_sheet}')/usedRange"
            )

            range_resp = self.session.get(
                used_range_url, headers=wb_headers, timeout=60
            )
            if range_resp.ok:
                values = range_resp.json().get("values", [])
                session_row_count = max(0, len(values) - 1)
                # print(f"[OneDrive EXCEL] ✅ File content confirmed: {session_row_count} data rows visible in active session.")
            else:
                print(
                    f"[OneDrive EXCEL] ⚠️ Could not read used range ({range_resp.status_code}) "
                    f"— session may not have full access to file content."
                )

            # ── Step 4: Trigger a full workbook recalculation ──
            # print("[OneDrive EXCEL] Triggering workbook recalculation (fires data-refresh connectors)...")
            calc_resp = self.session.post(
                f"{base_url}/workbook/application/calculate",
                headers=wb_headers,
                json={"calculationType": "FullRebuild"},
                timeout=30,
            )
            if calc_resp.ok or calc_resp.status_code == 204:
                print("[OneDrive EXCEL] ✅ Workbook recalculation triggered.")

            else:
                print(
                    f"[OneDrive EXCEL] ⚠️ Recalculation returned {calc_resp.status_code} "
                    f"(may still work — continuing)."
                )

            # ── Step 5: Close the session (flushes any server-side pending writes) ──
            try:
                self.session.post(
                    f"{base_url}/workbook/closeSession",
                    headers=wb_headers,
                    timeout=15,
                )
                # print("[OneDrive EXCEL] Workbook session closed (server-side changes flushed).")
            except Exception:
                pass
            session_id = ""  # Mark closed so finally block skips double-close

            # ── Step 6: Download baseline snapshot BEFORE opening the browser ──
            # Take the snapshot NOW so we have a true pre-sync row count to compare against.
            # print("[OneDrive EXCEL] Downloading baseline .xlsx snapshot (pre-browser-sync)...")
            baseline_rows = self._download_onedrive_excel_raw(user_email, item_id)
            baseline_count = len(baseline_rows)
            # print(f"[OneDrive EXCEL] Baseline: {baseline_count} rows in raw file.")

            # ── Step 7: Open in browser as real user (edit mode) — strongest sync trigger ──
            # This is the primary trigger. Excel Online in edit mode activates the
            # Microsoft Forms connector which writes any pending form responses.
            if web_url:
                self._open_excel_with_stored_auth(web_url)
            else:
                # No URL — wait 30s anyway to give the API session recalculation time
                # print("[OneDrive EXCEL] ⏳ Waiting 30s for API session to trigger Forms sync...")
                time.sleep(30)

            # ── Step 8: Download again and compare row counts ──
            # print("[OneDrive EXCEL] Downloading updated .xlsx (after browser sync)...")
            updated_rows = self._download_onedrive_excel_raw(user_email, item_id)
            updated_count = len(updated_rows)

            if updated_count > baseline_count:
                new_count = updated_count - baseline_count
                print(
                    f"[OneDrive EXCEL] ✅ Forms sync DETECTED — "
                    f"{new_count} new response(s) added ({baseline_count} → {updated_count} rows)."
                )
            elif updated_count == baseline_count:
                print(
                    f"[OneDrive EXCEL] ℹ️ No new responses detected (row count unchanged: {updated_count}). "
                    f"All pending responses may already be synced, or Forms has no new submissions."
                )
            else:
                print(
                    f"[OneDrive EXCEL] ⚠️ Row count decreased ({baseline_count} → {updated_count}). "
                    f"File may have been modified externally during the sync wait."
                )

            return updated_rows

        except Exception as e:
            print(f"[OneDrive EXCEL] Workbook session error: {e}")
            # Best-effort fallback: raw download without sync trigger
            try:
                return self._download_onedrive_excel_raw(user_email, item_id)
            except Exception:
                return []

        finally:
            # Close session if still open (e.g. error occurred before step 5)
            if session_id:
                try:
                    close_headers = {**headers, "workbook-session-id": session_id}
                    self.session.post(
                        f"{base_url}/workbook/closeSession",
                        headers=close_headers,
                        timeout=15,
                    )
                    # print("[OneDrive EXCEL] Workbook session closed (finally block).")
                except Exception:
                    pass

    def _open_excel_with_stored_auth(self, web_url: str) -> None:
        """
        Open the Excel Online URL in a headless Playwright browser using
        STORED authentication state, simulating a real user editing the file.

        This is the primary Forms sync trigger — Microsoft Forms only writes
        pending responses when the Excel file is opened in EDIT mode by an
        authenticated user. This method:

        1. Loads stored cookies (saved via save_browser_auth_state)
        2. Navigates to the Excel Online URL with action=edit
        3. Waits for Excel Online to fully render (networkidle + title check)
        4. Simulates real user interaction (click a cell, scroll) so the
           session is indistinguishable from a real browser session
        5. Waits 45s — gives Microsoft Forms time to detect the active edit
           session and flush any pending form submissions into the sheet
        6. Closes the browser (Microsoft saves state on close)
        """
        import time
        import os
        from config import Config

        # Resolve auth state path: if relative, anchor to project root
        raw_path = Config.PLAYWRIGHT_AUTH_STATE_PATH
        if not os.path.isabs(raw_path):
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            auth_state_path = os.path.join(project_root, raw_path)
        else:
            auth_state_path = raw_path

        if not os.path.exists(auth_state_path):
            print(
                "[OneDrive EXCEL] ℹ️ No stored browser auth state found. "
                "Skipping browser-based sync trigger. "
                "Run save_browser_auth_state() once to enable this."
            )
            return

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

            # print("[OneDrive EXCEL] Opening Excel Online in headless browser (edit mode, simulating real user)...")
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ],
                )
                context = browser.new_context(
                    storage_state=auth_state_path,
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    # Mimic real browser locale/timezone
                    locale="en-US",
                    timezone_id="Asia/Kolkata",
                )
                page = context.new_page()
                try:
                    # print(f"[OneDrive EXCEL] Navigating to: {web_url[:100]}...")

                    # Use networkidle to wait for Excel Online to fully load
                    # (not just domcontentloaded, which misses the JS app)
                    try:
                        page.goto(web_url, wait_until="networkidle", timeout=90000)
                    except PWTimeout:
                        # networkidle can time out on heavy SPAs — that's fine
                        pass

                    # Wait up to 60s for Excel Online to fully render
                    excel_loaded = False
                    for attempt in range(12):  # 12 × 5s = 60s
                        time.sleep(5)
                        title = page.title()
                        url = page.url

                        if "login.microsoftonline" in url or "login.live" in url:
                            print(
                                "[OneDrive EXCEL] ⚠️ Redirected to login page — "
                                "stored auth may have expired. "
                                "Re-run save_browser_auth_state() to refresh."
                            )
                            break

                        is_excel_title = any(
                            kw in title.lower() for kw in ["excel", ".xlsx", "workbook"]
                        )
                        is_excel_url = (
                            "excel" in url.lower() or "_layouts" in url.lower()
                        )

                        if is_excel_title or is_excel_url:
                            excel_loaded = True
                            # print(f"[OneDrive EXCEL] ✅ Excel Online confirmed open")
                            break

                        # print(f"[OneDrive EXCEL] Waiting for Excel Online to render...")

                    if excel_loaded:
                        # ── Simulate real user activity ──────────────────────────────
                        # Click on the spreadsheet body to activate the edit session.
                        # Microsoft Forms detects active edit sessions by checking
                        # whether the workbook has been interacted with.
                        # print("[OneDrive EXCEL] Simulating user interactions to activate edit session...")
                        try:
                            # Try to click the spreadsheet grid (Excel Online renders
                            # the grid as a canvas — click near center of the viewport)
                            page.mouse.click(960, 400)
                            time.sleep(1)

                            # Scroll down slightly (simulates browsing the sheet)
                            page.mouse.wheel(0, 300)
                            time.sleep(1)

                            # Click again to ensure the cell editor is active
                            page.mouse.click(960, 400)
                            time.sleep(1)

                            # Press Escape to exit any cell edit without changing data
                            page.keyboard.press("Escape")
                            time.sleep(0.5)

                            # print("[OneDrive EXCEL] ✅ User interactions simulated.")
                        except Exception as interact_err:
                            print(
                                f"[OneDrive EXCEL] User interaction warning: {interact_err}"
                            )

                        # ── Wait for Forms to flush responses ─────────────────────────
                        # Microsoft Forms watches for active edit sessions. Once it
                        # detects one, it queues a write of pending responses.
                        # 45s is enough for the queue to be processed in most cases.
                        # print("[OneDrive EXCEL] ⏳ Waiting 30s for Microsoft Forms to flush pending responses...")
                        time.sleep(30)
                        # print("[OneDrive EXCEL] ✅ Browser sync wait complete.")
                    else:
                        print(
                            "[OneDrive EXCEL] ⚠️ Excel Online did not fully render — "
                            "waiting 15s anyway (partial load may still trigger sync)."
                        )
                        time.sleep(15)

                except Exception as nav_err:
                    print(f"[OneDrive EXCEL] Browser navigation error: {nav_err}")
                    time.sleep(10)
                finally:
                    context.close()
                    browser.close()
                    # print("[OneDrive EXCEL] Headless browser closed.")

        except ImportError:
            print(
                "[OneDrive EXCEL] Playwright not installed — skipping browser sync trigger."
            )
        except Exception as e:
            print(f"[OneDrive EXCEL] Browser sync error: {e}")

    def save_browser_auth_state(
        self, login_url: str = "https://www.office.com"
    ) -> None:
        """
        One-time setup: open a visible browser, let the user log into Microsoft 365,
        then save the authenticated session (cookies + storage) to disk.

        The saved state is reused by _open_excel_with_stored_auth() on every sync
        without requiring another login.

        Usage (run once from a Python shell or a separate script):
            from app.services.sharepoint import SharePointMatchScoreUpdater
            sp = SharePointMatchScoreUpdater()
            sp.save_browser_auth_state()   # Browser opens — log in and close it

        The auth state is saved to playwright_auth_state.json in the project root.
        """
        import os
        from config import Config

        # Resolve auth state path: if relative, anchor to project root
        raw_path = Config.PLAYWRIGHT_AUTH_STATE_PATH
        if not os.path.isabs(raw_path):
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            auth_state_path = os.path.join(project_root, raw_path)
        else:
            auth_state_path = raw_path

        os.makedirs(os.path.dirname(auth_state_path), exist_ok=True)

        try:
            from playwright.sync_api import sync_playwright

            login_email = (
                Config.MS365_LOGIN_EMAIL or "your-ms365-account@yourdomain.com"
            )
            print("[OneDrive EXCEL] Opening browser for Microsoft 365 login...")
            print(f"[OneDrive EXCEL] Log in as {login_email}, then CLOSE the browser.")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)  # Visible window for login
                context = browser.new_context(
                    viewport={"width": 1400, "height": 900},
                )
                page = context.new_page()
                page.goto(login_url)

                # Wait for user to complete login and close the window
                try:
                    # Wait until the user closes the browser (page closes)
                    page.wait_for_url("**/office.com**", timeout=120000)
                    print("[OneDrive EXCEL] Login detected. Saving auth state...")
                except Exception:
                    print(
                        "[OneDrive EXCEL] Saving auth state (timeout or manual close)..."
                    )

                context.storage_state(path=auth_state_path)
                context.close()
                browser.close()

            print(f"[OneDrive EXCEL] ✅ Auth state saved to: {auth_state_path}")
            print(
                "[OneDrive EXCEL] Future syncs will use this stored session automatically."
            )

        except ImportError:
            print(
                "[OneDrive EXCEL] Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
        except Exception as e:
            print(f"[OneDrive EXCEL] Failed to save auth state: {e}")

    def _download_onedrive_excel_raw(self, user_email: str, item_id: str) -> list[dict]:
        """Download the raw .xlsx binary from OneDrive and parse with pandas."""
        content_url = (
            f"{self.GRAPH_BASE}/users/{user_email}/drive/items/{item_id}/content"
        )
        resp = self.session.get(
            content_url, headers=self._get_headers(), timeout=60, allow_redirects=True
        )
        if not resp.ok:
            return []

        try:
            df = pd.read_excel(io.BytesIO(resp.content), engine="openpyxl")
            for col in df.select_dtypes(include=["datetime", "datetimetz"]).columns:
                df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            df = df.astype(object).where(pd.notnull(df), None)
            print(
                f"[OneDrive EXCEL] Downloaded and parsed {len(df)} rows from raw .xlsx."
            )
            return df.to_dict(orient="records")
        except Exception as e:
            print(f"[OneDrive] Pandas error: {e}")
            return []
