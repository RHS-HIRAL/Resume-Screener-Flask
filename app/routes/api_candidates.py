# app/routes/api_candidates.py — API endpoints for Candidate CRUD, status updates, MS Forms syncing, and Outreach.

import threading
from flask import Blueprint, request, jsonify
from flask_login import login_required

from config import Config
from app.db.candidates import (
    get_all_candidates,
    update_candidate_selection_status,
    bulk_update_candidate_status,
    get_candidate_by_id,
    get_candidates_by_ids,
    get_unsynced_candidates,
    update_candidate_form_response,
    update_candidate_form_score,
)
from app.db.jobs import (
    get_all_jobs,
    get_all_unique_job_forms,
    update_job_form_excel,
    update_job_scoring_weights,
    get_jd_text,
)
from app.services.sharepoint import SharePointMatchScoreUpdater
from app.services.form_scorer import calculate_form_score
from app.services.email_outreach import build_email_html, send_bulk_outreach_async

api_candidates_bp = Blueprint("api_candidates", __name__)


def _background_bulk_sp_push(candidates: list, status: str):
    """Background thread to push status updates to SharePoint for multiple candidates."""
    print(f"[SP BULK SYNC] Starting sync for {len(candidates)} candidates.")
    try:
        sp = SharePointMatchScoreUpdater()
        for candidate in candidates:
            if candidate.get("resume_filename"):
                metadata = {"SelectionStatus": status}
                # Role_name is fetched via JOIN in the new DB structure
                role_name = candidate.get("role_name", "")
                sp.push_metadata(candidate["resume_filename"], metadata, role_name)
        print("[SP BULK SYNC] Completed bulk sync.")
    except Exception as e:
        print(f"[SP BULK SYNC ERROR] {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CANDIDATE LISTS & STATUS UPDATES
# ══════════════════════════════════════════════════════════════════════════════


@api_candidates_bp.route("/api/candidates")
@login_required
def api_list_candidates():
    min_score = request.args.get("min_score", 40, type=int)
    role = request.args.get("role", "")

    candidates = get_all_candidates(min_score=min_score)
    if role:
        candidates = [c for c in candidates if c.get("role_name") == role]

    return jsonify(candidates)


@api_candidates_bp.route("/api/candidate/status", methods=["POST"])
@login_required
def api_update_status():
    """Update selection status for a single candidate and sync to SharePoint."""
    data = request.json
    cid = data.get("candidate_id")
    status = data.get("status")

    if not cid or not status:
        return jsonify({"error": "Missing id or status"}), 400

    try:
        updated = update_candidate_selection_status(cid, status)
        if not updated:
            return jsonify({"error": "Candidate not found"}), 404

        # Background sync to SharePoint
        candidate = get_candidate_by_id(cid)
        if candidate and candidate.get("resume_filename"):
            threading.Thread(
                target=_background_bulk_sp_push, args=([candidate], status), daemon=True
            ).start()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_candidates_bp.route("/api/candidate/status/bulk", methods=["POST"])
@login_required
def api_bulk_update_status():
    """Update selection status for multiple candidates."""
    data = request.json
    cids = data.get("candidate_ids")
    status = data.get("status")

    if not cids or not status:
        return jsonify({"error": "Missing ids or status"}), 400

    try:
        count = bulk_update_candidate_status(cids, status)
        candidates = get_candidates_by_ids(cids)

        if candidates:
            threading.Thread(
                target=_background_bulk_sp_push, args=(candidates, status), daemon=True
            ).start()

        return jsonify({"success": True, "updated_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL OUTREACH
# ══════════════════════════════════════════════════════════════════════════════


@api_candidates_bp.route("/api/outreach", methods=["POST"])
@login_required
def api_outreach():
    """Triggers asynchronous bulk email outreach."""
    data = request.json
    ids = data.get("candidate_ids", [])
    form_link = data.get("form_link", "")
    custom_msg = data.get("custom_message", "")

    payloads = []
    for cid in ids:
        candidate = get_candidate_by_id(cid)
        if candidate and candidate.get("email"):
            jd_text = get_jd_text(candidate["job_id"])

            # Build HTML synchronously to utilize Flask's template context
            html_body = build_email_html(
                candidate["full_name"],
                candidate.get("role_name", "Unknown Role"),
                jd_text,
                form_link,
                custom_msg,
            )

            payloads.append(
                {
                    "candidate_id": cid,
                    "to_email": candidate["email"],
                    "to_name": candidate["full_name"],
                    "subject": f"Invitation: {candidate.get('role_name', 'Opportunity')}",
                    "html_body": html_body,
                    "form_link": form_link,
                }
            )

    if payloads:
        send_bulk_outreach_async(payloads)

    return jsonify(
        {"status": "queued", "sent": len(payloads), "failed": len(ids) - len(payloads)}
    ), 202


# ══════════════════════════════════════════════════════════════════════════════
# MS FORMS & SCORING
# ══════════════════════════════════════════════════════════════════════════════


@api_candidates_bp.route("/api/job/form-excel", methods=["POST"])
@login_required
def api_update_job_form_excel():
    """Map a Job ID to a specific Microsoft Form Excel filename."""
    data = request.json
    job_id = data.get("job_id")
    form_excel_name = data.get("form_excel_name")

    if not job_id or form_excel_name is None:
        return jsonify({"error": "Missing job_id or form_excel_name"}), 400

    try:
        updated = update_job_form_excel(int(job_id), form_excel_name)
        if updated:
            return jsonify(
                {
                    "success": True,
                    "message": f"Job {job_id} linked to '{form_excel_name}'",
                }
            )
        return jsonify({"error": "Job not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_candidates_bp.route("/api/job/weights", methods=["POST"])
@login_required
def api_update_job_weights():
    """Update scoring weights for a specific Job ID."""
    data = request.json
    job_id = data.get("job_id")
    weights = data.get("weights")

    if not job_id or not weights:
        return jsonify({"error": "Missing job_id or weights"}), 400

    try:
        updated = update_job_scoring_weights(int(job_id), weights)
        if updated:
            return jsonify(
                {"success": True, "message": f"Weights updated for Job {job_id}"}
            )
        return jsonify({"error": "Job not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _sync_ms_form_responses_logic() -> int:
    """Internal logic to fetch and map Excel form responses to candidates."""
    try:
        sp = SharePointMatchScoreUpdater()
        excel_filenames = get_all_unique_job_forms()

        if not excel_filenames:
            excel_filenames = ["Full_Stack_Development_Intern", "candidate information"]

        total_sync_count = 0
        
        # Determine all candidates that we need to consider for form sync
        all_candidates = get_all_candidates(min_score=0)
        candidate_map = {c["email"].lower(): c for c in all_candidates if c.get("email")}

        if not candidate_map:
            return 0

        for excel_filename in excel_filenames:
            rows = []
            try:
                # 1. Try Shared SharePoint Site
                rows = sp.get_excel_rows(excel_filename)

                # 2. Try OneDrive (Personal) if SP fails
                if not rows:
                    possible_emails = ["deep.malusare@si2tech.com", Config.MAILBOX_USER]
                    for email in possible_emails:
                        if not email:
                            continue
                        rows = sp.get_onedrive_excel_rows(email, excel_filename)
                        if rows:
                            break
            except Exception as e:
                print(f"[SYNC ERROR] Error fetching '{excel_filename}': {e}")
                continue

            if not rows:
                continue

            # Process all rows in this file and keep only the LATEST row per email.
            # Assuming Microsoft Forms appends to Excel, the last row for an email is the latest.
            latest_rows_by_email = {}
            for row in rows:
                email = row.get("Email Address") or row.get("Email") or row.get("email")
                if email:
                    email_clean = str(email).strip().lower()
                    latest_rows_by_email[email_clean] = row
            
            # Now we have the latest response for each email present in this Excel part.
            for email_clean, latest_row in latest_rows_by_email.items():
                if email_clean in candidate_map:
                    candidate = candidate_map[email_clean]
                    
                    # Decide if this is a "new" update (they didn't have one, or the data changed)
                    # For simplicity, we just update it and if they already had a response, we reset status.
                    had_previous_response = candidate.get("form_responses") is not None
                    
                    try:
                        updated = update_candidate_form_response(email_clean, latest_row)

                        if updated:
                            # If they submitted a new response after already having one, reset status to PENDING
                            if had_previous_response:
                                update_candidate_selection_status(candidate["id"], "PENDING")
                                
                            total_sync_count += 1
                            # Auto-calculate the Form Score upon sync
                            try:
                                job_id = candidate.get("job_id")
                                jd_text = get_jd_text(job_id) if job_id else ""

                                # We fetch the specific job to get its custom weights
                                jobs = get_all_jobs()
                                job = next(
                                    (j for j in jobs if j["id"] == job_id), {}
                                )
                                custom_weights = job.get("scoring_weights")

                                score_result = calculate_form_score(
                                    latest_row, jd_text, custom_weights
                                )
                                if score_result.get("score") is not None:
                                    update_candidate_form_score(
                                        candidate["id"], score_result["score"]
                                    )
                            except Exception as score_err:
                                print(
                                    f"[SYNC ERROR] Scoring failed for {email_clean}: {score_err}"
                                )
                                
                            # We don't delete from candidate_map here because a candidate might 
                            # (theoretically) appear in another excel file, but it's fine 
                            # since the latest loop aggregates by email anyway.
                    except Exception as e:
                        print(f"[SYNC ERROR] Row processing failed for {email_clean}: {e}")

        return total_sync_count
    except Exception as e:
        print(f"[SYNC ERROR] Fatal sync error: {e}")
        return 0


@api_candidates_bp.route("/api/sync-responses", methods=["POST"])
@login_required
def api_sync_responses():
    """Manual trigger for MS Form sync."""
    count = _sync_ms_form_responses_logic()
    return jsonify({"success": True, "updated_count": count})


@api_candidates_bp.route("/api/backfill-form-scores", methods=["POST"])
@login_required
def api_backfill_form_scores():
    """Re-calculate form scores using updated JD logic or weights."""
    try:
        data = request.json or {}
        filter_job_id = data.get("job_id")

        all_candidates = get_all_candidates(min_score=0)
        jobs = get_all_jobs()
        job_map = {j["id"]: j for j in jobs}

        scored = 0
        skipped = 0

        for c in all_candidates:
            job_id = c.get("job_id")

            if filter_job_id and str(job_id) != str(filter_job_id):
                continue

            if not c.get("form_responses"):
                skipped += 1
                continue

            try:
                job = job_map.get(job_id, {})
                jd_text = job.get("jd_text", "")
                custom_weights = job.get("scoring_weights")

                # ── DIAGNOSTIC LOGGING ────────────────────────────────────────
                print(f"\n[BACKFILL] Candidate: {c.get('full_name')} (id={c['id']}, job_id={job_id})")
                print(f"[BACKFILL]   jd_text present: {bool(jd_text)} | length: {len(jd_text or '')}")
                print(f"[BACKFILL]   form_response keys: {list(c['form_responses'].keys())}")
                print(f"[BACKFILL]   custom_weights: {custom_weights}")
                # ─────────────────────────────────────────────────────────────

                score_result = calculate_form_score(
                    c["form_responses"], jd_text, custom_weights
                )

                print(f"[BACKFILL]   score_result: {score_result}")

                if score_result.get("score") is not None:
                    update_candidate_form_score(c["id"], score_result["score"])
                    scored += 1
                    print(f"[BACKFILL]   ✅ Saved score: {score_result['score']}")
                else:
                    skipped += 1
                    print(f"[BACKFILL]   ⚠️  Score is None — skipped (no parseable criteria matched)")
            except Exception as e:
                print(f"[BACKFILL ERROR] {c.get('full_name', 'Unknown')}: {e}")
                skipped += 1

        msg = f"Re-calculation complete. Scored: {scored}, Skipped: {skipped}"
        return jsonify(
            {"success": True, "message": msg, "scored": scored, "skipped": skipped}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@api_candidates_bp.route("/api/debug-form-scores")
@login_required
def api_debug_form_scores():
    """Diagnostic: show form response keys, jd_text presence, and score result for each candidate with a form response."""
    try:
        all_candidates = get_all_candidates(min_score=0)
        jobs = get_all_jobs()
        job_map = {j["id"]: j for j in jobs}

        results = []
        for c in all_candidates:
            if not c.get("form_responses"):
                continue

            job_id = c.get("job_id")
            job = job_map.get(job_id, {})
            jd_text = job.get("jd_text", "")
            custom_weights = job.get("scoring_weights")

            score_result = calculate_form_score(c["form_responses"], jd_text, custom_weights)

            results.append({
                "candidate_id": c["id"],
                "name": c.get("full_name"),
                "email": c.get("email"),
                "job_id": job_id,
                "jd_text_length": len(jd_text or ""),
                "jd_text_snippet": (jd_text or "")[:200],
                "form_response_keys": list(c["form_responses"].keys()),
                "custom_weights": custom_weights,
                "score_result": score_result,
                "current_form_score": c.get("form_score"),
            })

        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
