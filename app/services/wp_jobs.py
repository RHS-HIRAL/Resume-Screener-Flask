"""
app/services/wp_jobs.py
Fetches published job openings from the WP Job Openings WordPress plugin
via the standard REST API.  Returns a slug-keyed dict for O(1) lookups.
"""

import logging
import requests
from requests.auth import HTTPBasicAuth
from typing import Dict, Any

from config import Config

logger = logging.getLogger("wp_jobs")


def fetch_wp_jobs() -> Dict[str, Dict[str, Any]]:
    """
    Fetch all published jobs from WordPress and return them as a dict
    keyed by the URL slug.

    Returns:
        {
            "talent-acquisition-specialist-hr-ops-exposure": {
                "wp_id": 10373,
                "title": "Talent Acquisition Specialist ...",
                "date_posted": "2026-03-20",
                "link": "https://si2tech.com/jobs/talent-acquisition-specialist-hr-ops-exposure/",
            },
            ...
        }

    On failure (missing creds, network error), returns an empty dict
    so the caller can fall back to slug-only behaviour.
    """
    site_url = Config.WP_SITE_URL
    username = Config.WP_USERNAME
    app_password = Config.WP_APP_PASSWORD

    if not site_url or not username or not app_password:
        logger.warning("WordPress credentials not configured — skipping WP job fetch.")
        return {}

    endpoint = f"{site_url.rstrip('/')}/wp-json/wp/v2/awsm_job_openings"

    params = {
        "per_page": 100,
        "status": "publish",
        "_fields": "id,title,link,date,slug",
    }

    jobs: Dict[str, Dict[str, Any]] = {}

    try:
        response = requests.get(
            endpoint,
            auth=HTTPBasicAuth(username, app_password),
            headers={"Accept": "application/json"},
            params=params,
            timeout=15,
        )

        if response.status_code == 401:
            logger.error("WP API 401 — check username / application password.")
            return {}
        if response.status_code == 404:
            logger.error("WP API 404 — is WP Job Openings plugin active?")
            return {}

        response.raise_for_status()

        for job in response.json():
            slug = job.get("slug", "")
            if not slug:
                # Derive slug from the link URL as fallback
                link = job.get("link", "")
                slug = link.rstrip("/").split("/")[-1] if link else ""
            if not slug:
                continue

            jobs[slug] = {
                "wp_id": job.get("id"),
                "title": job.get("title", {}).get("rendered", ""),
                "date_posted": job.get("date", "")[:10],  # YYYY-MM-DD
                "link": job.get("link", ""),
            }

        logger.info("Fetched %d job(s) from WordPress.", len(jobs))

    except requests.exceptions.ConnectionError:
        logger.error("Could not connect to WordPress at %s", site_url)
    except requests.exceptions.Timeout:
        logger.error("WordPress API request timed out.")
    except Exception as e:
        logger.error("Unexpected WP API error: %s", e)

    return jobs
