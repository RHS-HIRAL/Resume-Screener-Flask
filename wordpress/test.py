import requests
import json
from requests.auth import HTTPBasicAuth
from typing import Dict, Any, List, Optional

# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIALS  ←  fill these in before running
# ─────────────────────────────────────────────────────────────────────────────
WP_SITE_URL = "https://si2tech.com"  # e.g. "https://si2.ai"
WP_USERNAME = "si2dev"  # WordPress username
WP_APP_PASSWORD = ")5J9%Z%6sS1KiZh*7&W@N5tI"  # WordPress Application Password


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – List all job openings
# ─────────────────────────────────────────────────────────────────────────────


def list_wp_job_openings(
    site_url: str,
    username: str,
    app_password: str,
    per_page: int = 100,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetches all published job openings from the WP Job Openings plugin.

    Uses the standard WordPress REST API endpoint for the custom post type
    `awsm_job_openings` that the plugin registers.

    Returns a list of job dicts on success, or None on failure.
    """
    endpoint = f"{site_url.rstrip('/')}/wp-json/wp/v2/awsm_job_openings"

    params = {
        "per_page": per_page,  # max results per page (WP max = 100)
        "status": "publish",  # only live / published jobs
        "_fields": "id,title,link,date,excerpt,meta",  # trim the payload
    }

    headers = {
        "Accept": "application/json",
    }

    print(f"\n🔍  Fetching job listings from: {endpoint}")
    print(f"    Params: {params}\n")

    try:
        response = requests.get(
            endpoint,
            auth=HTTPBasicAuth(username, app_password),
            headers=headers,
            params=params,
            timeout=15,
        )

        # Surface clear error info for common auth / endpoint issues
        if response.status_code == 401:
            print("❌  401 Unauthorised – check username and application password.")
            return None
        if response.status_code == 404:
            print(
                "❌  404 Not Found – is the WP Job Openings plugin active and the site URL correct?"
            )
            return None

        response.raise_for_status()

        jobs = response.json()

        if not jobs:
            print("ℹ️   No published job openings found.")
            return []

        print(f"✅  Found {len(jobs)} job opening(s):\n")
        print(f"{'#':<4} {'ID':<8} {'Title':<45} {'Date Posted':<14} URL")
        print("-" * 110)

        for idx, job in enumerate(jobs, start=1):
            title = job.get("title", {}).get("rendered", "N/A")
            job_id = job.get("id", "N/A")
            date = job.get("date", "N/A")[:10]  # YYYY-MM-DD
            link = job.get("link", "")
            meta = job.get("meta", {})

            # WP REST API can return meta as [] when fields aren't registered
            if not isinstance(meta, dict):
                meta = {}

            # WP Job Openings stores custom fields in meta
            location = meta.get("awsm_job_location", "")
            job_type = meta.get("awsm_job_type", "")

            print(f"{idx:<4} {job_id:<8} {title[:44]:<45} {date:<14} {link}")
            if location or job_type:
                print(f"     📍 {location}   🕐 {job_type}")


        print()
        return jobs

    except requests.exceptions.ConnectionError:
        print(f"❌  Connection error – could not reach {site_url}. Check the URL.")
        return None
    except requests.exceptions.Timeout:
        print("❌  Request timed out.")
        return None
    except requests.exceptions.HTTPError as http_err:
        print(f"❌  HTTP Error: {http_err}")
        print(f"    Response body: {response.text[:500]}")
        return None
    except Exception as err:
        print(f"❌  Unexpected error: {err}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – Create a new job opening  (commented out – run AFTER Step 1 works)
# ─────────────────────────────────────────────────────────────────────────────

# def create_wp_job_opening(
#     site_url: str,
#     username: str,
#     app_password: str,
#     job_data: Dict[str, Any],
# ) -> Optional[Dict[str, Any]]:
#     """
#     Creates a new job listing in the WP Job Openings plugin.
#     Uncomment and call this function once Step 1 is confirmed working.
#     """
#     endpoint = f"{site_url.rstrip('/')}/wp-json/wp/v2/awsm_job_openings"
#     headers = {
#         "Accept":       "application/json",
#         "Content-Type": "application/json",
#     }
#     try:
#         response = requests.post(
#             endpoint,
#             auth=HTTPBasicAuth(username, app_password),
#             headers=headers,
#             json=job_data,
#             timeout=15,
#         )
#         response.raise_for_status()
#         data = response.json()
#         print(f"✅  Job created!  ID={data.get('id')}  URL={data.get('link')}")
#         return data
#     except requests.exceptions.HTTPError as http_err:
#         print(f"❌  HTTP Error: {http_err}")
#         print(f"    Response: {response.text[:500]}")
#         return None
#     except Exception as err:
#         print(f"❌  Error: {err}")
#         return None


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # STEP 1 – fetch & display existing jobs
    jobs = list_wp_job_openings(WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD)

    # Once Step 1 is working, uncomment below for Step 2
    # new_job_payload = {
    #     "title":   "Senior Back-End Engineer",
    #     "content": "<h3>About the Role</h3><p>We are scaling our cloud infra.</p>",
    #     "status":  "publish",
    #     "excerpt": "Join our remote team to build scalable back-end systems.",
    #     "meta": {
    #         "awsm_job_location": "Remote",
    #         "awsm_job_type":     "Full-Time",
    #         "awsm_job_category": "Engineering",
    #     }
    # }
    # create_wp_job_opening(WP_SITE_URL, WP_USERNAME, WP_APP_PASSWORD, new_job_payload)
