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

    # Check if unused function
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

    def get_excel_rows(self, filename: str) -> list[dict]:
        """Search for an Excel file globally/in folders and parse via pandas."""
        drive_id = self._get_drive_id()
        candidates = self.find_matching_items(filename)
        if not candidates and not filename.endswith(".xlsx"):
            candidates = self.find_matching_items(filename + ".xlsx")

        if not candidates:
            for folder in ["/", "General", "Recordings"]:
                try:
                    items = self._list_folder_children(folder)
                    candidates = [
                        i
                        for i in items
                        if i["name"].lower().startswith(filename.lower())
                    ]
                    if candidates:
                        break
                except Exception:
                    continue

        xlsx_candidates = [c for c in candidates if c["name"].lower().endswith(".xlsx")]
        if not xlsx_candidates:
            return []

        item = next(
            (
                c
                for c in xlsx_candidates
                if c["name"].lower() in [filename.lower(), (filename + ".xlsx").lower()]
            ),
            xlsx_candidates[0],
        )

        content_url = f"{self.GRAPH_BASE}/drives/{drive_id}/items/{item['id']}/content"
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
            return df.to_dict(orient="records")
        except Exception as e:
            print(f"[SP] Pandas error reading Excel: {e}")
            return []

    def get_onedrive_excel_rows(self, user_email: str, filename: str) -> list[dict]:
        """Search for an Excel file in a specific user's OneDrive."""
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

        content_url = (
            f"{self.GRAPH_BASE}/users/{user_email}/drive/items/{item['id']}/content"
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
            return df.to_dict(orient="records")
        except Exception as e:
            print(f"[OneDrive] Pandas error: {e}")
            return []
