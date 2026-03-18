resume_screener/
│
├── .env                                     # Secret keys, DB creds, API keys
├── requirements.txt                         # All Python package dependencies
├── run.py                                   # App entry point, starts Flask server
│   ├── setup_initial_state()                # Init DB and create default admin
│
├── config.py                                # Loads .env into one Config class
├── QA.txt                                   # Benchmark Q&A sheet for call scoring
├── prompt_template.txt                      # Gemini prompt template for call scoring
│
└── app/
    ├── __init__.py                          # Flask app factory, registers blueprints
    │
    ├── db/                                  # All raw SQL — no SQL outside this folder
    │   │
    │   ├── connection.py                    # DB pool setup, table creation
    │   │   ├── get_cursor(commit)           # Checks out a DB connection from pool
    │   │   └── init_db()                   # Creates all tables and indexes
    │   │
    │   ├── users.py                         # User auth queries
    │   │   ├── get_user_by_username()       # Fetch user row by username
    │   │   ├── get_user_by_id()             # Fetch user row by primary key
    │   │   ├── create_user()               # Hash password and insert new user
    │   │   └── verify_user()               # Validate login credentials, return user
    │   │
    │   ├── jobs.py                          # Job / JD table queries
    │   │   ├── upsert_job()                 # Insert or update a job record
    │   │   ├── get_all_jobs()              # Return all job records as list
    │   │   ├── get_all_unique_job_forms()  # Return distinct form Excel names per job
    │   │   ├── get_jd_text()               # Return raw JD text for a job ID
    │   │   ├── update_job_form_excel()     # Link a job to MS Forms Excel file
    │   │   └── update_job_scoring_weights() # Save custom scoring weights for job
    │   │
    │   ├── candidates.py                    # All candidate CRUD operations
    │   │   ├── _generate_atomic_candidate_id() # Generate race-safe sequential ID
    │   │   ├── save_candidate()            # Insert or update candidate from Gemini result
    │   │   ├── get_all_candidates()        # Fetch all candidates filtered by score
    │   │   ├── get_candidate_by_id()       # Fetch one candidate by integer PK
    │   │   ├── get_candidate_by_visible_id() # Fetch candidate by human-readable ID
    │   │   ├── get_candidates_by_ids()     # Bulk fetch candidates by PK list
    │   │   ├── get_candidates_for_role()   # Fetch all candidates for a role
    │   │   ├── get_unsynced_candidates()   # Fetch candidates with no form response
    │   │   ├── get_stats()                 # Return aggregate dashboard stats
    │   │   ├── update_candidate_selection_status() # Set Pending/Selected/Rejected
    │   │   ├── bulk_update_candidate_status()      # Batch update status for many
    │   │   ├── update_candidate_match_score()      # Update AI match score field
    │   │   ├── update_candidate_form_response()    # Save MS Forms response by email
    │   │   ├── update_candidate_form_score()       # Save calculated form score
    │   │   ├── update_candidate_qa_score()         # Save Gemini call QA score
    │   │   ├── mark_outreach_sent()        # Mark email sent, save meeting link
    │   │   └── delete_candidates_by_ids() # Permanently delete candidates from DB
    │   │
    │   └── qa_results.py                   # Call QA scoring result queries
    │       ├── save_qa_result()            # Insert new QA scoring row for candidate
    │       └── get_qa_results_by_candidate_fk() # Fetch all QA results, newest first
    │
    ├── models/                              # Data schemas and session user model
    │   ├── schemas.py                       # Pydantic models for Gemini JSON response
    │   └── user.py                          # Flask-Login UserMixin session class
    │
    ├── services/                            # Business logic — AI, email, SharePoint
    │   │
    │   ├── ai_evaluator.py                  # Gemini resume vs JD analysis
    │   │   └── evaluate_resume()           # Call Gemini, return structured analysis dict
    │   │
    │   ├── call_qa.py                       # Sarvam STT + Gemini interview scoring
    │   │   ├── _find_project_root()        # Walk dirs to find QA.txt location
    │   │   ├── load_prompt_resources()     # Read and cache QA.txt + prompt template
    │   │   ├── start_transcription()       # Upload audio to Sarvam, return job_id
    │   │   ├── check_transcription_status() # Poll Sarvam, return transcript text
    │   │   ├── score_transcript()          # Send transcript to Gemini, return score
    │   │   └── _background_qa_pipeline()  # Thread: STT → score → save to DB
    │   │
    │   ├── email_outreach.py               # SMTP bulk email sender
    │   │   ├── build_email_html()          # Render Jinja2 email template to HTML
    │   │   ├── _send_emails_worker()       # Worker: one SMTP connection, send all
    │   │   └── send_bulk_outreach_async()  # Spawn worker thread, return immediately
    │   │
    │   ├── form_scorer.py                  # MS Forms response scoring engine
    │   │   └── calculate_form_score()      # Score form answers against JD, 0–100
    │   │
    │   ├── sharepoint.py                   # Microsoft Graph API file operations
    │   │   ├── _get_headers()              # Return MSAL auth headers for Graph API
    │   │   ├── _get_drive_id()             # Fetch and cache SharePoint drive ID
    │   │   ├── ensure_folder_exists()      # Recursively create folders if missing
    │   │   ├── upload_file()               # Upload bytes or text to SP folder
    │   │   ├── delete_file()               # Permanently delete SP file by name
    │   │   ├── download_text_content()     # Download SP item, extract text (PDF/DOCX)
    │   │   ├── find_txt_version()          # Find .txt version of a resume in SP
    │   │   ├── list_resumes_grouped()      # List all resumes grouped by job folder
    │   │   ├── list_jd_files()             # List all JD files from JD folder
    │   │   ├── push_metadata()             # Write MatchScore/Status to SP columns
    │   │   ├── find_matching_items()       # Search SP for file by name and role
    │   │   ├── upload_jd_pdf()             # Upload JD PDF to SP JD folder
    │   │   ├── upload_jd_text()            # Upload JD plain text, skip if exists
    │   │   └── find_item_by_path()         # Fetch SP item by full relative path
    │   │
    │   └── sharepoint_sync.py              # Full sync orchestrator pipeline
    │       ├── GraphAuthProvider.get_access_token() # Acquire MSAL client-creds token
    │       ├── GraphAuthProvider.get_headers()      # Return ready auth headers
    │       ├── _unique_base_path()         # Generate unique local path with hash
    │       ├── _save_last_sync()           # Write current UTC time to sync file
    │       ├── get_last_sync_time()        # Read timestamp of last sync run
    │       └── run_sync()                  # Run email→SP→extract pipelines, notify Teams
    │
    ├── routes/                             # Flask Blueprints — URL handlers
    │   │
    │   ├── auth.py                         # Login, register, logout pages
    │   │   ├── login()          /login     # Validate credentials, start user session
    │   │   ├── register()       /register  # Create new user account
    │   │   └── logout()         /logout    # End session, redirect to login
    │   │
    │   ├── views.py                        # HTML page renders (no data mutation)
    │   │   ├── dashboard()      /          # Dashboard with stats and recent candidates
    │   │   ├── screener()       /screener  # AI resume screener tool page
    │   │   ├── outreach()       /outreach  # Bulk email outreach management page
    │   │   └── responses()      /responses # MS Forms responses review page
    │   │
    │   ├── api_analysis.py                 # AI screening endpoints
    │   │   ├── set_progress()              # Update in-memory progress for a user
    │   │   ├── get_progress()              # Read current progress for a user
    │   │   ├── _background_sp_push()       # Thread: push match score to SharePoint
    │   │   ├── api_progress()   /api/progress        # SSE stream of screening progress
    │   │   ├── api_analyze()    /api/analyze          # Analyze single resume with Gemini
    │   │   └── api_analyze_bulk() /api/analyze/bulk  # SSE bulk analysis for all resumes
    │   │
    │   ├── api_candidates.py               # Candidate management endpoints
    │   │   ├── _background_bulk_sp_push()  # Thread: push status to SP in bulk
    │   │   ├── api_list_candidates()       /api/candidates              # List filtered candidates
    │   │   ├── api_update_status()         /api/candidate/status        # Update one status
    │   │   ├── api_bulk_update_status()    /api/candidate/status/bulk   # Batch update status
    │   │   ├── api_delete_candidates()     /api/candidates/delete       # Password-protected delete
    │   │   ├── api_outreach()              /api/outreach                # Queue bulk email send
    │   │   ├── api_update_job_form_excel() /api/job/form-excel          # Link job to Excel form
    │   │   ├── api_update_job_weights()    /api/job/weights             # Update scoring weights
    │   │   ├── api_sync_forms()            /api/forms/sync              # Pull MS Forms from SP
    │   │   ├── api_score_forms()           /api/forms/score             # Score all form responses
    │   │   └── api_debug_form_scores()     /api/debug-form-scores       # Debug scoring details
    │   │
    │   ├── api_sharepoint.py               # SharePoint file operation endpoints
    │   │   ├── api_sp_sync()       /api/sp/sync          # Trigger full sync in background
    │   │   ├── api_sp_last_sync()  /api/sp/last-sync     # Return last sync timestamp
    │   │   ├── api_sp_files()      /api/sp/files         # Return grouped resumes and JDs
    │   │   ├── api_sp_content()    /api/sp/content       # Download text of one SP item
    │   │   └── api_sp_match_folder() /api/sp/match-folder # Find resume folder for JD
    │   │
    │   └── api_qa.py                       # Call QA scoring endpoints
    │       ├── _background_sp_upload()     # Thread: upload file to SP silently
    │       ├── qa_page()           /qa                       # Render QA dashboard page
    │       ├── api_qa_transcribe()  /api/qa/transcribe       # Upload audio, start STT job
    │       ├── api_qa_status()      /api/qa/status/<job_id>  # Poll STT job for transcript
    │       ├── api_qa_evaluate()    /api/qa/evaluate         # Score transcript with Gemini
    │       └── api_qa_results()     /api/qa/results/<id>     # Fetch all past QA results
    │
    ├── utils/                              # Shared helpers used across the app
    │   └── helpers.py
    │       ├── extract_job_code()          # Pull 4-digit job code from folder name
    │       └── normalize_slug()            # Strip prefixes, return clean snake_case
    │
    ├── templates/                          # Jinja2 HTML templates
    │   ├── login.html                      # Login form page
    │   ├── register.html                   # Registration form page
    │   ├── dashboard.html                  # Stats, recent candidates, sync trigger
    │   ├── screener.html                   # Resume screener and rescore UI
    │   ├── outreach.html                   # Candidate email outreach UI
    │   ├── responses.html                  # MS Forms response viewer UI
    │   ├── qa.html                         # Call QA audio upload and scoring UI
    │   └── email/
    │       └── outreach.html               # HTML email template for candidates
    │
    └── static/                             # Frontend assets
        ├── css/
        │   └── style.css                   # Global styles, sidebar, responsive layout
        └── js/                             # Per-page JS — fetch calls, DOM, SSE