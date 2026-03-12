"""
flask_app.py — Unified Flask web application for the Resume Screener.

Consolidates logic from server2.py (FastAPI), alpha_app.py (Streamlit),
outreach_tab.py, and database.py into a single Flask server.
"""

import os
import time
import json
import io
import ssl
import smtplib
import threading
from datetime import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    Response,
    session,
)
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from flask_cors import CORS
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import google.genai as genai
from google.genai import types


from database import (
    init_db,
    create_user,
    verify_user,
    get_user_by_username,
    delete_candidate,
    get_all_jobs,
    get_stats,
    get_all_candidates,
    mark_outreach_sent,
    update_candidate_selection_status,
    update_candidate_form_response,
    get_unsynced_candidates,
    save_candidate,
    get_candidate_by_id,
    get_jd_text,
    extract_job_code,
    bulk_update_candidate_status,
    get_candidates_by_ids,
    get_all_unique_job_forms,
    update_job_form_excel,
    update_candidate_form_score,
    update_job_scoring_weights,
    get_existing_resume_filenames,
    # ── QA feature ────────────────────────────────────────────────────────
    save_qa_result,
    get_qa_results_by_candidate,
    update_candidate_qa_score,
    get_candidate_by_visible_id,
)
from form_scorer import calculate_form_score
from sharepoint_helper import SharePointMatchScoreUpdater
from call_qa_scorer import run_qa_pipeline, score_existing_transcript

# ── Load environment ──────────────────────────────────────────────────────────
load_dotenv()

# ── Initialize App ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-key-change-me")
CORS(app)

# ── Initialize DB ─────────────────────────────────────────────────────────────
init_db()

# ── Login Manager ─────────────────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, user_id, username, is_admin):
        self.id = user_id
        self.username = username
        self.is_admin = is_admin


@login_manager.user_loader
def load_user(user_id):
    from database import _cursor

    with _cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row:
        return User(row["id"], row["username"], row["is_admin"])
    return None


# ── AI Config (Legacy matching) ────────────────────────────────────────────────
# Note: We use the new SDK for QA, but keeping a client here for initial screening
# to avoid breaking existing logic.
_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY2"))


# ═══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS  (ported verbatim from server2.py)
# ═══════════════════════════════════════════════════════════════════════════════


class ParameterMatch(BaseModel):
    status: str = Field(description="Match, Partial Match, or No Match")
    summary: str = Field(
        description="A 1-line summary indicating if and why it matches the JD"
    )


class ResumeJDMatch(BaseModel):
    overall_match_score: int = Field(
        description="Overall match score strictly on a scale of 0 to 100"
    )
    experience: ParameterMatch
    education: ParameterMatch
    location: ParameterMatch
    project_history_relevance: ParameterMatch
    tools_used: ParameterMatch
    certifications: ParameterMatch


class PersonalInfo(BaseModel):
    full_name: str
    location: str
    email: str
    phone: str


class Employment(BaseModel):
    current_job_title: str
    current_organization: str


class CareerMetrics(BaseModel):
    total_experience_in_years: float
    total_jobs: int


class Socials(BaseModel):
    linkedin: str
    github: str
    portfolio: str


class Education(BaseModel):
    degree: str
    institution: str
    graduation_year: str


class ResumeDataExtraction(BaseModel):
    personal_information: PersonalInfo
    professional_summary: str
    current_employment: Employment
    career_metrics: CareerMetrics
    social_profiles: Socials
    education_history: list[Education]


class ComprehensiveResumeAnalysis(BaseModel):
    function_1_resume_jd_matching: ResumeJDMatch
    function_2_resume_data_extraction: ResumeDataExtraction


# ═══════════════════════════════════════════════════════════════════════════════
# EMAIL HELPERS  (ported verbatim from server2.py)
# ═══════════════════════════════════════════════════════════════════════════════


def _build_email_html(
    candidate_name: str,
    jd_title: str,
    jd_text: str,
    form_link: str,
    custom_message: str,
) -> str:
    """Build a clean HTML email body."""
    form_block = ""
    if form_link:
        form_block = f"""
        <div style="margin:28px 0; text-align:center;">
            <a href="{form_link}"
               style="background:#6366f1;color:#fff;padding:14px 32px;
                      border-radius:8px;text-decoration:none;font-weight:600;
                      font-size:15px;display:inline-block;">
                &#128221; Submit Candidate Form
            </a>
        </div>
        """

    custom_block = ""
    if custom_message and custom_message.strip():
        custom_block = f"""
        <p style="color:#374151;margin-bottom:16px;">{custom_message}</p>
        """

    jd_html = jd_text.replace("\n", "<br>")

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f3f4f6;font-family:'Segoe UI',Arial,sans-serif;">
        <div style="max-width:620px;margin:40px auto;background:#fff;
                    border-radius:12px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,0.08);">
            <div style="background:linear-gradient(135deg,#6366f1,#4f46e5);padding:32px 40px;">
                <div style="font-size:22px;font-weight:700;color:#fff;">
                    Exciting Opportunity For You 🎉
                </div>
                <div style="font-size:14px;color:rgba(255,255,255,0.8);margin-top:4px;">
                    {jd_title}
                </div>
            </div>
            <div style="padding:36px 40px;">
                <p style="color:#111827;font-size:16px;margin-bottom:16px;">
                    Dear <strong>{candidate_name}</strong>,
                </p>
                <p style="color:#374151;margin-bottom:16px;">
                    Thank you for your interest. After reviewing your profile, we're pleased to
                    share this opportunity and invite you to the next step in our hiring process.
                </p>
                {custom_block}
                <div style="background:#f9fafb;border-left:4px solid #6366f1;
                            border-radius:0 8px 8px 0;padding:20px 24px;margin:24px 0;">
                    <div style="font-size:12px;font-weight:700;color:#6366f1;
                                text-transform:uppercase;letter-spacing:0.08em;margin-bottom:12px;">
                        Job Description
                    </div>
                    <div style="font-size:14px;color:#374151;line-height:1.7;">
                        {jd_html}
                    </div>
                </div>
                {form_block}
                <p style="color:#6b7280;font-size:13px;margin-top:24px;">
                    If you have any questions, please reply to this email directly.
                </p>
                <p style="color:#374151;font-size:14px;margin-top:16px;">
                    Best regards,<br>
                    <strong>HR Team</strong>
                </p>
            </div>
            <div style="background:#f9fafb;padding:20px 40px;
                        border-top:1px solid #e5e7eb;
                        font-size:12px;color:#9ca3af;text-align:center;">
                This email was sent as part of our candidate outreach process.
            </div>
        </div>
    </body>
    </html>
    """


def _send_email(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
) -> tuple[bool, str]:
    """Send via SMTP.  Reads SMTP_* from env."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    from_name = os.getenv("SMTP_FROM_NAME", "HR Team")

    if not smtp_user or not smtp_pass:
        return False, "SMTP credentials not configured in .env."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{smtp_user}>"
    msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        return True, "Email sent successfully."
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed."
    except smtplib.SMTPRecipientsRefused:
        return False, f"Recipient refused: {to_email}"
    except Exception as e:
        return False, str(e)


# ═══════════════════════════════════════════════════════════════════════════════
# PROGRESS TRACKING  (SSE — simple global store)
# ═══════════════════════════════════════════════════════════════════════════════

progress_store = {"percent": 0, "message": "Waiting..."}


# ═══════════════════════════════════════════════════════════════════════════════
# SHAREPOINT PUSH HELPER
# ═══════════════════════════════════════════════════════════════════════════════


def _sp_config():
    """Build SharePoint config dict from environment."""
    return {
        "tenant_id": os.getenv("AZURE_TENANT_ID"),
        "client_id": os.getenv("AZURE_CLIENT_ID"),
        "client_secret": os.getenv("AZURE_CLIENT_SECRET"),
        "site_domain": os.getenv("SHAREPOINT_SITE_DOMAIN"),
        "site_path": os.getenv("SHAREPOINT_SITE_PATH"),
        "drive_name": os.getenv("SHAREPOINT_DRIVE_NAME"),
    }


def push_to_sharepoint(filename, metadata, role_hint="", item_id=""):
    """Background task — push Metadata back to SharePoint."""
    try:
        cfg = _sp_config()
        if not all(cfg.values()):
            print("[SP ERROR] Missing SharePoint config in .env")
            return
        updater = SharePointMatchScoreUpdater(**cfg)
        status, msg, _ = updater.push_metadata(
            filename, metadata, role_hint=role_hint, confirmed_item_id=item_id
        )
        print(f"[SP SYNC] {status}: {msg}")
    except Exception as e:
        print(f"[SP ERROR] Sync failed: {e}")


def bulk_push_to_sharepoint(candidates, status):
    """Loop through candidates and push status to SharePoint."""
    print(f"[SP BULK SYNC] Starting sync for {len(candidates)} candidates.")
    for candidate in candidates:
        if candidate.get("resume_filename"):
            metadata = {"SelectionStatus": status}
            # We call the logic directly to avoid spawning too many threads
            # NOTE: We don't always have SharePoint IDs in the DB yet, so search might still happen here.
            push_to_sharepoint(
                candidate["resume_filename"], metadata, candidate["role_name"]
            )
    print(f"[SP BULK SYNC] Completed bulk sync.")


def sync_ms_form_responses():
    """Background function to fetch and sync MS Form Excel data across all job roles."""
    try:
        print("[SYNC] Starting Multi-Form MS Form sync...")
        cfg = _sp_config()
        updater = SharePointMatchScoreUpdater(**cfg)

        # Get all unique Excel filenames from the jobs table
        excel_filenames = get_all_unique_job_forms()

        # Fallback to the default if no specific forms are defined yet (for backward compatibility)
        if not excel_filenames:
            # We use the previous common name as a fallback
            excel_filenames = ["Full_Stack_Development_Intern", "candidate information"]

        print(
            f"[SYNC] Found {len(excel_filenames)} unique Excel files to check: {excel_filenames}"
        )

        total_sync_count = 0

        # Optimization: Only match against candidates who don't have responses yet
        unsynced = get_unsynced_candidates()
        # Store the whole candidate object to have access to ID and other fields
        unsynced_map = {c["email"].lower(): c for c in unsynced}

        if not unsynced_map:
            print("[SYNC] All candidates already have form responses. Skipping.")
            return 0

        for excel_filename in excel_filenames:
            print(f"[SYNC] Processing file: '{excel_filename}'")
            rows = []
            try:
                # 1. Try SharePoint (Shared Site)
                print(f"[SYNC] Searching for '{excel_filename}' in SharePoint...")
                rows = updater.get_excel_rows(excel_filename)

                # 2. Try OneDrive (Personal) if SharePoint fails
                if not rows:
                    user_email = os.getenv("MAILBOX_USER")
                    # Prioritize deep.malusare because we confirmed the file is there
                    possible_emails = ["deep.malusare@si2tech.com", user_email]
                    for email in possible_emails:
                        if not email:
                            continue
                        print(
                            f"[SYNC] SharePoint failed. Trying OneDrive for {email}..."
                        )
                        rows = updater.get_onedrive_excel_rows(email, excel_filename)
                        if rows:
                            print(
                                f"[SYNC] Successfully found rows in {email}'s OneDrive."
                            )
                            break
            except Exception as e:
                print(f"[SYNC] Error fetching '{excel_filename}': {e}")
                continue

            if not rows:
                print(f"[SYNC] No rows found in '{excel_filename}'.")
                continue

            print(f"[SYNC] Comparing {len(rows)} rows from '{excel_filename}'...")
            for row in rows:
                try:
                    # Match by "Email Address" as specified by user
                    email = (
                        row.get("Email Address") or row.get("Email") or row.get("email")
                    )

                    if email:
                        email_clean = str(email).strip().lower()
                        if email_clean in unsynced_map:
                            candidate = unsynced_map[email_clean]
                            print(
                                f"[SYNC] New response found for: {candidate['full_name']} ({email_clean})"
                            )
                            updated = update_candidate_form_response(email_clean, row)

                            if updated:
                                total_sync_count += 1

                                # ── NEW: Calculate Form Score ──────────────────────
                                try:
                                    job_id = candidate.get("job_id")
                                    jd_text = get_jd_text(job_id) if job_id else ""

                                    score_result = calculate_form_score(row, jd_text)
                                    if score_result.get("score") is not None:
                                        update_candidate_form_score(
                                            candidate["id"], score_result["score"]
                                        )
                                        print(
                                            f"[SYNC] Form Score for {email_clean}: {score_result['score']}%"
                                        )
                                except Exception as score_err:
                                    print(
                                        f"[SYNC ERROR] Scoring failed for {email_clean}: {score_err}"
                                    )
                                # ──────────────────────────────────────────────────

                                # Remove from the memory map to avoid redundant checks in next files
                                del unsynced_map[email_clean]
                except Exception as e:
                    print(
                        f"[SYNC ERROR] Error processing row in '{excel_filename}': {e}"
                    )

        print(f"[SYNC] Multi-Form Sync Finished. Total updated: {total_sync_count}")
        return total_sync_count
    except Exception as e:
        print(f"[SYNC ERROR] {e}")
        return 0


@app.route("/api/job/form-excel", methods=["POST"])
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


@app.route("/api/job/weights", methods=["POST"])
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


@app.route("/api/backfill-form-scores", methods=["POST"])
@login_required
def api_backfill_form_scores():
    """Re-calculate form scores. Supports optional 'job_id' to only score one role."""
    try:
        data = request.json or {}
        filter_job_id = data.get("job_id")  # Optional: only backfill this role

        all_candidates = get_all_candidates(min_score=0)

        # Optimization: Fetch all jobs to get their custom weights
        jobs = get_all_jobs()
        job_map = {j["id"]: j for j in jobs}

        scored = 0
        skipped = 0

        for c in all_candidates:
            job_id = c.get("job_id")

            # Skip if we are filtering by role and this isn't the role
            if filter_job_id and str(job_id) != str(filter_job_id):
                continue

            if not c.get("form_responses"):
                skipped += 1
                continue

            try:
                job = job_map.get(job_id, {})
                jd_text = job.get("jd_text", "")
                custom_weights = job.get("scoring_weights")

                score_result = calculate_form_score(
                    c["form_responses"], jd_text, custom_weights
                )
                if score_result.get("score") is not None:
                    update_candidate_form_score(c["id"], score_result["score"])
                    scored += 1
                    # print(f"[BACKFILL] {c['full_name']}: {score_result['score']}%")
                else:
                    skipped += 1
            except Exception as e:
                print(f"[BACKFILL ERROR] {c.get('full_name', 'Unknown')}: {e}")
                skipped += 1

        msg = f"Re-calculation complete. Scored: {scored}, Skipped: {skipped}"
        return jsonify(
            {"success": True, "message": msg, "scored": scored, "skipped": skipped}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        user_data = verify_user(username, password)
        if user_data:
            user = User(user_data["id"], user_data["username"], user_data["is_admin"])
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Invalid username or password", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirm = request.form.get("confirm_password")

        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect(url_for("register"))

        success, msg = create_user(username, password)
        if success:
            flash("Registration successful! Please login.", "success")
            return redirect(url_for("login"))
        flash(msg, "danger")
    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/")
@login_required
def dashboard():
    stats = get_stats()
    recent = get_all_candidates()[:5]
    all_jobs = get_all_jobs()
    return render_template(
        "dashboard.html", stats=stats, recent_candidates=recent, all_jobs=all_jobs
    )


@app.route("/screener")
@login_required
def screener():
    return render_template("screener.html")


@app.route("/outreach")
@login_required
def outreach():
    roles = get_all_jobs()
    return render_template("outreach.html", roles=roles)


@app.route("/responses")
@login_required
def responses():
    """Review Dashboard."""
    roles = get_all_jobs()
    return render_template("responses.html", roles=roles)


@app.route("/api/sync-responses", methods=["POST"])
@login_required
def api_sync_responses():
    """Manual trigger for MS Form sync."""
    count = sync_ms_form_responses()
    return jsonify({"success": True, "updated_count": count})


@app.route("/api/candidate/status", methods=["POST"])
@login_required
def api_update_status():
    """Update selection status and sync to SharePoint."""
    data = request.json
    cid = data.get("candidate_id")
    status = data.get("status")

    if not cid or not status:
        return jsonify({"error": "Missing id or status"}), 400

    try:
        updated = update_candidate_selection_status(cid, status)
        if not updated:
            return jsonify({"error": "Candidate not found"}), 404

        # Optional: Sync status back to SharePoint
        candidate = get_candidate_by_id(cid)
        if candidate and candidate.get("resume_filename"):
            metadata = {"SelectionStatus": status}
            threading.Thread(
                target=push_to_sharepoint,
                args=(candidate["resume_filename"], metadata, candidate["role_name"]),
            ).start()

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/candidate/status/bulk", methods=["POST"])
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

        # Sync to SharePoint in the background
        candidates = get_candidates_by_ids(cids)
        if candidates:
            threading.Thread(
                target=bulk_push_to_sharepoint,
                args=(candidates, status),
            ).start()

        return jsonify({"success": True, "updated_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# SHAREPOINT API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/api/sp/files")
@login_required
def api_sp_files():
    """Return grouped resume folders + flat JD list from SharePoint."""
    try:
        cfg = _sp_config()
        updater = SharePointMatchScoreUpdater(**cfg)
        resumes = updater.list_resumes_grouped()
        jds = updater.list_jd_files()
        return jsonify({"resumes": resumes, "jds": jds, "connected": True})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500


@app.route("/api/sp/content")
@login_required
def api_sp_content():
    """Download the text content of a single SharePoint item by id."""
    item_id = request.args.get("item_id")
    if not item_id:
        return jsonify({"error": "No item_id provided"}), 400
    try:
        cfg = _sp_config()
        updater = SharePointMatchScoreUpdater(**cfg)
        content = updater.download_text_content(item_id)
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# CANDIDATE / ANALYSIS API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/api/candidates")
@login_required
def api_list_candidates():
    min_score = request.args.get("min_score", 40, type=int)
    role = request.args.get("role", "")
    candidates = get_all_candidates(min_score=min_score)
    if role:
        candidates = [c for c in candidates if c["role_name"] == role]
    return jsonify(candidates)


@app.route("/api/progress")
@login_required
def api_progress():
    """SSE endpoint for real-time progress updates."""

    def generate():
        while True:
            yield f"data: {json.dumps(progress_store)}\n\n"
            if progress_store["percent"] >= 100:
                break
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    """Run AI analysis on resume + JD content (already fetched from SharePoint)."""
    global progress_store

    progress_store = {"percent": 10, "message": "Reading content..."}

    jd_title = request.form.get("jd_title")
    jd_text = request.form.get("jd_text")
    resume_text = request.form.get("resume_text")
    resume_filename = request.form.get("resume_filename")
    sync_sp = request.form.get("sync_sharepoint") == "on"

    if not all([jd_title, jd_text, resume_text, resume_filename]):
        return jsonify({"error": "Missing required fields (JD or Resume content)"}), 400

    try:
        progress_store = {"percent": 30, "message": "Applying AI logic..."}

        prompt = f"""
        You are an expert technical recruiter. Analyze the Resume and JD.
        Return strictly JSON with matching parameters and data extraction.

        Resume: {resume_text}
        JD: {jd_text}
        """

        response = _client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ComprehensiveResumeAnalysis,
                temperature=0.1,
            ),
        )

        progress_store = {"percent": 70, "message": "Saving to database..."}

        analysis_dict = json.loads(response.text)
        print(
            f"[ANALYZE] AI response parsed. Score: {analysis_dict.get('function_1_resume_jd_matching', {}).get('overall_match_score', '?')}"
        )

        cid = save_candidate(
            result=analysis_dict,
            role_name=jd_title,
            jd_filename=jd_title,
            jd_text=jd_text,
            resume_filename=resume_filename,
        )
        print(f"[ANALYZE] Candidate saved to PostgreSQL with id={cid}")

        if sync_sp:
            progress_store = {"percent": 90, "message": "Syncing to SharePoint..."}

            # Extract metadata for SharePoint using discovered internal names
            score = analysis_dict.get("function_1_resume_jd_matching", {}).get(
                "overall_match_score", 0
            )
            extraction = analysis_dict.get("function_2_resume_data_extraction", {})
            personal = extraction.get("personal_information", {})

            job_id_val = "Unknown"
            try:
                # Extracts 4 digits from role folder name
                job_id_val = str(extract_job_code(jd_title))
            except:
                pass

            metadata = {
                "MatchScore": score,
                "CandidateName": personal.get("full_name", "Unknown"),
                "CandidateEmail": personal.get("email", ""),
                "CandidatePhone": personal.get("phone", ""),
                "JobID": job_id_val,
                "JobRole": jd_title,
            }

            threading.Thread(
                target=push_to_sharepoint,
                args=(resume_filename, metadata, jd_title),
            ).start()

        progress_store = {"percent": 100, "message": "Analysis Complete!"}
        return response.text, 200, {"Content-Type": "application/json"}

    except Exception as e:
        import traceback

        traceback.print_exc()
        progress_store = {"percent": 100, "message": f"Error: {str(e)}"}
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# BULK SCREENING API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


def _normalize_slug(name: str) -> str:
    """
    Normalize a name for matching:
      'JD_it-helpdesk-engineer.pdf' → 'it_helpdesk_engineer'
      '9097_IT_Helpdesk_Engineer'   → 'it_helpdesk_engineer'
    """
    import re as _re

    # Remove file extension
    name = Path(name).stem
    # Remove leading 'JD_' prefix (case-insensitive)
    name = _re.sub(r"^(?:JD_)", "", name, flags=_re.IGNORECASE)
    # Remove leading digits followed by underscore (e.g., '9097_')
    name = _re.sub(r"^\d+_", "", name)
    # Replace hyphens with underscores and lowercase
    return name.replace("-", "_").lower().strip("_")


@app.route("/api/sp/match-folder", methods=["POST"])
@login_required
def api_sp_match_folder():
    """
    Given a JD filename, find the matching resume folder in SharePoint.
    Returns the folder name and the list of resume files inside it.
    """
    data = request.json or {}
    jd_name = data.get("jd_name", "")
    if not jd_name:
        return jsonify({"error": "Missing jd_name"}), 400

    jd_slug = _normalize_slug(jd_name)
    print(f"[BULK] JD slug: '{jd_slug}' (from '{jd_name}')")

    try:
        cfg = _sp_config()
        sp = SharePointMatchScoreUpdater(**cfg)
        resumes_grouped = sp.list_resumes_grouped()

        matched_folder = None
        matched_files = []
        for folder_name, files in resumes_grouped.items():
            folder_slug = _normalize_slug(folder_name)
            if folder_slug == jd_slug:
                matched_folder = folder_name
                matched_files = files
                break

        if not matched_folder:
            return jsonify(
                {
                    "matched": False,
                    "jd_slug": jd_slug,
                    "available_folders": list(resumes_grouped.keys()),
                }
            )

        return jsonify(
            {
                "matched": True,
                "folder_name": matched_folder,
                "resume_count": len(matched_files),
                "resumes": matched_files,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze/bulk", methods=["POST"])
@login_required
def api_analyze_bulk():
    """
    Bulk-analyze all resumes in a matched folder against a JD.
    Returns Server-Sent Events (SSE) so the frontend can track progress
    and display each candidate result as it completes.
    """
    data = request.json or {}
    jd_id = data.get("jd_id")
    jd_name = data.get("jd_name", "")
    folder_name = data.get("folder_name", "")
    resume_list = data.get("resumes", [])  # [{id, name}, ...]
    sync_sp = data.get("sync_sharepoint", True)

    if not jd_id or not folder_name or not resume_list:
        return jsonify({"error": "Missing jd_id, folder_name, or resumes"}), 400

    def generate():
        cfg = _sp_config()
        sp = SharePointMatchScoreUpdater(**cfg)

        # 1. Download JD content
        try:
            jd_text = sp.download_text_content(jd_id)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'Failed to download JD: {e}'})}\n\n"
            return

        jd_title = (
            folder_name  # Role name = folder name (e.g. '9097_IT_Helpdesk_Engineer')
        )

        # 2. Check which resumes are already analysed via SharePoint MatchScore
        # We now skip files where match_score > 0 as per SharePoint data
        to_process = [r for r in resume_list if (r.get("match_score") or 0) <= 0]
        skipped_count = len(resume_list) - len(to_process)

        total = len(to_process)
        yield f"data: {json.dumps({'type': 'init', 'total': total, 'skipped': skipped_count})}\n\n"

        if total == 0:
            yield f"data: {json.dumps({'type': 'done', 'message': 'All resumes already analysed.'})}\n\n"
            return

        for idx, resume_info in enumerate(to_process, 1):
            resume_id = resume_info["id"]
            resume_name = resume_info["name"]

            yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total, 'resume_name': resume_name})}\n\n"

            try:
                # Download resume content
                resume_text = sp.download_text_content(resume_id)

                # AI Analysis (same logic as single /api/analyze)
                prompt = f"""
                You are an expert technical recruiter. Analyze the Resume and JD.
                Return strictly JSON with matching parameters and data extraction.

                Resume: {resume_text}
                JD: {jd_text}
                """

                response = _client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ComprehensiveResumeAnalysis,
                        temperature=0.1,
                    ),
                )

                analysis_dict = json.loads(response.text)
                score = analysis_dict.get("function_1_resume_jd_matching", {}).get(
                    "overall_match_score", 0
                )
                extraction = analysis_dict.get("function_2_resume_data_extraction", {})
                personal = extraction.get("personal_information", {})

                # Save to DB
                cid = save_candidate(
                    result=analysis_dict,
                    role_name=jd_title,
                    jd_filename=jd_name,
                    jd_text=jd_text,
                    resume_filename=resume_name,
                )

                # Sync to SharePoint (background)
                if sync_sp:
                    try:
                        job_id_val = str(extract_job_code(jd_title))
                    except Exception:
                        job_id_val = "Unknown"

                    sp_metadata = {
                        "MatchScore": score,
                        "CandidateName": personal.get("full_name", "Unknown"),
                        "CandidateEmail": personal.get("email", ""),
                        "CandidatePhone": personal.get("phone", ""),
                        "JobID": job_id_val,
                        "JobRole": jd_title,
                    }
                    threading.Thread(
                        target=push_to_sharepoint,
                        args=(resume_name, sp_metadata, jd_title, resume_id),
                    ).start()

                # Send result to frontend
                result_payload = {
                    "type": "result",
                    "current": idx,
                    "total": total,
                    "candidate": {
                        "name": personal.get("full_name", "Unknown"),
                        "email": personal.get("email", ""),
                        "score": score,
                        "resume_filename": resume_name,
                        "experience": extraction.get("career_metrics", {}).get(
                            "total_experience_in_years", 0
                        ),
                        "current_title": extraction.get("current_employment", {}).get(
                            "current_job_title", ""
                        ),
                        "match_details": {
                            k: analysis_dict.get(
                                "function_1_resume_jd_matching", {}
                            ).get(k, {})
                            for k in [
                                "experience",
                                "education",
                                "location",
                                "project_history_relevance",
                                "tools_used",
                                "certifications",
                            ]
                        },
                    },
                }
                yield f"data: {json.dumps(result_payload)}\n\n"
                print(f"[BULK] {idx}/{total} ✓ {resume_name} → Score: {score}")

            except Exception as e:
                import traceback

                traceback.print_exc()
                yield f"data: {json.dumps({'type': 'error_item', 'current': idx, 'total': total, 'resume_name': resume_name, 'error': str(e)})}\n\n"
                print(f"[BULK] {idx}/{total} ✗ {resume_name} → Error: {e}")

        yield f"data: {json.dumps({'type': 'done', 'message': f'Bulk analysis complete. Processed {total} resumes.'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# ═══════════════════════════════════════════════════════════════════════════════
# OUTREACH API ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/api/outreach", methods=["POST"])
@login_required
def api_outreach():
    data = request.json
    ids = data.get("candidate_ids", [])
    form_link = data.get("form_link", "")
    custom_msg = data.get("custom_message", "")

    results = []
    for cid in ids:
        try:
            candidate = get_candidate_by_id(cid)
            if not candidate or not candidate.get("email"):
                continue

            jd_text_for_email = get_jd_text(candidate["job_id"])
            html_body = _build_email_html(
                candidate_name=candidate["full_name"],
                jd_title=candidate["role_name"],
                jd_text=jd_text_for_email,
                form_link=form_link,
                custom_message=custom_msg,
            )

            success, msg = _send_email(
                to_email=candidate["email"],
                to_name=candidate["full_name"],
                subject=f"Invitation: {candidate['role_name']}",
                html_body=html_body,
            )

            if success:
                mark_outreach_sent(cid, form_link)
                results.append({"id": cid, "status": "sent"})
            else:
                results.append({"id": cid, "status": "failed", "error": msg})

        except Exception as e:
            results.append({"id": cid, "status": "error", "error": str(e)})

    sent = sum(1 for r in results if r["status"] == "sent")
    return jsonify({"sent": sent, "failed": len(results) - sent, "details": results})


# ═══════════════════════════════════════════════════════════════════════════════
# CALL QA ROUTES
# ═══════════════════════════════════════════════════════════════════════════════


@app.route("/qa")
@login_required
def qa_page():
    """QA Call Scoring page."""
    # Filter only "Selected" candidates as requested
    all_c = get_all_candidates(min_score=0)
    candidates = []
    for c in all_c:
        if c.get("selection_status", "").lower() == "selected":
            # Convert datetimes to strings for tojson
            if c.get("screened_at"):
                c["screened_at"] = c["screened_at"].isoformat()
            if c.get("outreach_sent_at"):
                c["outreach_sent_at"] = c["outreach_sent_at"].isoformat()
            candidates.append(c)

    # Get unique roles for the filter
    roles = sorted(list(set(c["role_name"] for c in candidates)))
    return render_template("qa.html", candidates=candidates, roles=roles)


@app.route("/api/qa/transcribe", methods=["POST"])
@login_required
def api_qa_transcribe():
    """
    Stage 1: Upload audio, store in SharePoint, and transcribe via Sarvam STT.
    Returns the raw transcript for HR to edit.
    """
    audio_file = request.files.get("audio_file")
    candidate_id = request.form.get("candidate_id")  # visible candidate_id (text)
    if not audio_file or not candidate_id:
        return jsonify({"error": "Missing audio_file or candidate_id"}), 400

    candidate = get_candidate_by_visible_id(candidate_id)
    if not candidate:
        return jsonify({"error": "Candidate not found"}), 404

    # 1. Save temp locally for Sarvam
    import tempfile, os as _os, asyncio

    suffix = Path(audio_file.filename).suffix or ".mp3"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        audio_content = audio_file.read()
        tmp.write(audio_content)
        tmp_path = tmp.name

    try:
        # 2. SharePoint Upload (Recordings)
        # Format: CallRecordings/recordings/[JobID]_[RoleName]/[JobID]_[CandidateName].mp3
        job_id = candidate.get("job_id", "Unknown")
        role_name = candidate.get("role_name", "Unknown").replace(" ", "_")
        candidate_name = candidate.get("full_name", "Unknown").replace(" ", "_")

        folder_label = f"{job_id}_{role_name}"
        audio_filename = f"{job_id}_{candidate_name}{suffix}"

        cfg = _sp_config()
        sp_updater = SharePointMatchScoreUpdater(**cfg)

        # Upload recording (running async in sync bridge)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            sp_updater.upload_file(
                f"CallRecordings/recordings/{folder_label}",
                audio_filename,
                audio_content,
            )
        )

        # 3. Transcribe via Sarvam
        from call_qa_scorer import transcribe_audio

        stt_result = transcribe_audio(tmp_path)
        transcript_text = stt_result["conversation_text"]

        # 4. SharePoint Upload (Transcripts)
        # Format: CallRecordings/transcripts/[JobID]_[RoleName]/[JobID]_[CandidateName].txt
        transcript_filename = f"{job_id}_{candidate_name}.txt"
        loop.run_until_complete(
            sp_updater.upload_file(
                f"CallRecordings/transcripts/{folder_label}",
                transcript_filename,
                transcript_text,
            )
        )
        loop.close()

        return jsonify(
            {
                "success": True,
                "transcript": transcript_text,
                "job_id": stt_result.get("job_id"),
            }
        )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            _os.unlink(tmp_path)
        except:
            pass


@app.route("/api/qa/evaluate", methods=["POST"])
@login_required
def api_qa_evaluate():
    """
    Stage 2: Score the (edited) transcript using Gemini.
    """
    data = request.json or {}
    candidate_id = data.get("candidate_id")
    transcript = data.get("transcript", "").strip()

    if not candidate_id or not transcript:
        return jsonify({"error": "Missing candidate_id or transcript"}), 400

    # Validate candidate exists before scoring (prevents FK violation in call_qa_results)
    candidate = get_candidate_by_visible_id(str(candidate_id))
    if not candidate:
        return jsonify(
            {
                "error": f"Candidate '{candidate_id}' not found in the database. "
                f"Please screen the resume first before running QA."
            }
        ), 404

    import re as _re

    try:
        from call_qa_scorer import score_transcript, _save_eval_results

        # Gemini Scoring (loads QA.txt and prompt_template.txt internally)
        scoring_result = score_transcript(transcript=transcript)
        score_text = scoring_result["score_text"]

        # Save to Local Disk (Evaluation reports)
        eval_file = _save_eval_results(
            score_text=score_text,
            token_meta=scoring_result["token_meta"],
            label=f"cand{candidate_id}",
        )

        # Save to Database
        qa_row_id = save_qa_result(
            candidate_id=candidate_id,
            audio_filename="",  # Filled in stage 1 or left blank for evaluate only
            stt_job_id="",
            conversation_file="",
            conversation_text=transcript,
            score_text=score_text,
            eval_file=eval_file,
            token_meta=scoring_result["token_meta"],
        )

        # Parse raw score: "**Total Score:** 45 / 50" -> 45
        # Robust regex: handles bold tags and various separators
        m = _re.search(
            r"Total\s+Score[^*:\s]*[:\s\*\-]+(\d+)", score_text, _re.IGNORECASE
        )
        raw_score = 0
        if m:
            raw_score = int(m.group(1))
            update_candidate_qa_score(
                candidate_id, raw_score
            )  # uses visible candidate_id (text)

        return jsonify(
            {
                "success": True,
                "score_text": score_text,
                "numeric_score": raw_score,
                "qa_row_id": qa_row_id,
            }
        )
    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/qa/results/<string:candidate_id>")
@login_required
def api_qa_results(candidate_id):
    """Return all past QA results for a candidate."""
    try:
        results = get_qa_results_by_candidate(candidate_id)
        for r in results:
            if r.get("created_at"):
                r["created_at"] = r["created_at"].isoformat()
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Ensure at least one admin user exists on first run
    with app.app_context():
        user = get_user_by_username("admin")
        if not user:
            print("Creating default admin user...")
            create_user("admin", "admin123", is_admin=1)

    app.run(debug=True, port=5001)
