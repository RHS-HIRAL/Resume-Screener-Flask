# app/utils/helpers.py — Shared utility functions for parsing identifiers and normalizing strings across the application.

import re
from pathlib import Path


def extract_job_code(folder_or_role_name: str) -> int:
    m = re.match(r"(\d{4})", folder_or_role_name)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot extract 4-digit job code from '{folder_or_role_name}'")


def normalize_slug(name: str) -> str:
    name = Path(name).stem
    name = re.sub(r"^(?:JD_)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\d+_", "", name)
    return name.replace("-", "_").lower().strip("_")
