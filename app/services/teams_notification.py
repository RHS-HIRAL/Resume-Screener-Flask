"""
app/services/teams_notification.py
Sends sync-pipeline summary notifications to Microsoft Teams via
an Incoming Webhook using Adaptive Cards.
"""

import logging
from datetime import datetime

import requests

from config import Config

logger = logging.getLogger("teams_notification")


def send_teams_notification(results: dict) -> None:
    """
    Post an Adaptive Card to the configured Teams webhook summarizing
    all three sync pipelines.

    ``results`` is the combined dict returned by ``run_sync()``, e.g.::

        {
            "email_fetch":      {"success": 3, "failed": 0, "skipped": 1},
            "text_extraction":  {"uploaded": 5, "skipped": 2, "failed": 0},
            "jd_sync":          {"uploaded": 2, "skipped": 4, "failed": 0,
                                 "text_uploaded": 2, "text_skipped": 4},
            "errors":           ["optional error strings …"]
        }
    """
    webhook_url = Config.TEAMS_WEBHOOK_URL
    if not webhook_url:
        logger.info("TEAMS_WEBHOOK_URL not set — skipping Teams notification.")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    errors = results.get("errors", [])
    overall_status = "⚠️ Completed with errors" if errors else "✅ Completed successfully"

    # ── Build pipeline summary rows ──────────────────────────────────────
    def _row(label: str, values: list[tuple[str, str | int]]) -> dict:
        """Create a single fact-set row."""
        return {
            "type": "FactSet",
            "facts": [{"title": k, "value": str(v)} for k, v in values],
        }

    body_items: list[dict] = [
        # Header
        {
            "type": "TextBlock",
            "size": "Large",
            "weight": "Bolder",
            "text": f"🔄 SharePoint Sync Summary — {now}",
        },
        {
            "type": "TextBlock",
            "text": overall_status,
            "color": "Attention" if errors else "Good",
            "weight": "Bolder",
            "spacing": "Small",
        },
        {
            "type": "TextBlock",
            "text": "---",
            "spacing": "Small",
        },
    ]

    # Pipeline 1 — Email Fetch & Resume Upload
    p1 = results.get("email_fetch", {})
    if p1:
        body_items.append(
            {
                "type": "TextBlock",
                "text": "📧 Pipeline 1 — Email Fetch & Resume Upload",
                "weight": "Bolder",
                "spacing": "Medium",
            }
        )
        body_items.append(
            _row("P1", [
                ("✅ Uploaded", p1.get("success", 0)),
                ("⏭️ Skipped", p1.get("skipped", 0)),
                ("❌ Failed", p1.get("failed", 0)),
            ])
        )

    # Pipeline 2 — Text Extraction
    p2 = results.get("text_extraction", {})
    if p2:
        body_items.append(
            {
                "type": "TextBlock",
                "text": "📝 Pipeline 2 — Text Extraction",
                "weight": "Bolder",
                "spacing": "Medium",
            }
        )
        body_items.append(
            _row("P2", [
                ("✅ Uploaded", p2.get("uploaded", 0)),
                ("⏭️ Skipped", p2.get("skipped", 0)),
                ("❌ Failed", p2.get("failed", 0)),
            ])
        )

    # Pipeline 3 — JD Scrape → PDF → Text
    p3 = results.get("jd_sync", {})
    if p3:
        body_items.append(
            {
                "type": "TextBlock",
                "text": "📋 Pipeline 3 — JD Sync",
                "weight": "Bolder",
                "spacing": "Medium",
            }
        )
        body_items.append(
            _row("P3", [
                ("✅ PDFs Uploaded", p3.get("uploaded", 0)),
                ("⏭️ PDFs Skipped", p3.get("skipped", 0)),
                ("❌ PDFs Failed", p3.get("failed", 0)),
                ("📄 Texts Uploaded", p3.get("text_uploaded", 0)),
                ("⏭️ Texts Skipped", p3.get("text_skipped", 0)),
            ])
        )

    # Errors section (if any)
    if errors:
        body_items.append(
            {
                "type": "TextBlock",
                "text": "❌ Errors",
                "weight": "Bolder",
                "color": "Attention",
                "spacing": "Medium",
            }
        )
        for err in errors[:5]:
            body_items.append(
                {
                    "type": "TextBlock",
                    "text": f"• {err}",
                    "wrap": True,
                    "color": "Attention",
                    "spacing": "None",
                }
            )

    # ── Compose Adaptive Card payload ────────────────────────────────────
    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": body_items,
                },
            }
        ],
    }

    # ── Send ─────────────────────────────────────────────────────────────
    try:
        resp = requests.post(
            webhook_url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.ok:
            logger.info("Teams notification sent successfully.")
        else:
            logger.warning(
                "Teams webhook returned %d: %s", resp.status_code, resp.text[:200]
            )
    except Exception as e:
        logger.error("Failed to send Teams notification: %s", e)
