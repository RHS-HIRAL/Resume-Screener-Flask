# App Structure:
```
resume_screener/
├── .env                       # Environment variables
├── requirements.txt           # Python dependencies
├── run.py                     # Entry point to launch the application
├── config.py                  # Centralized configuration loader
├── app/
│   ├── __init__.py            # Application Factory (Flask app, DB, LoginManager init)
│   ├── db/                    # Database / Data Access Layer (DAL)
│   │   ├── __init__.py
│   │   ├── connection.py      # psycopg2 pool, cursors, and init_db logic
│   │   ├── users.py           # Auth and user queries
│   │   ├── jobs.py            # Job/JD queries
│   │   ├── candidates.py      # Candidate CRUD, status updates, bulk ops
│   │   └── qa_results.py      # QA/Call scoring queries
│   ├── models/                # Data Structures
│   │   ├── __init__.py
│   │   ├── schemas.py         # Pydantic models (ResumeJDMatch, etc.)
│   │   └── user.py            # Flask-Login UserMixin class
│   ├── services/              # Business Logic Layer
│   │   ├── __init__.py
│   │   ├── ai_evaluator.py    # Gemini resume parsing & prompting logic
│   │   ├── call_qa.py         # Sarvam STT and Gemini QA scoring (call_qa_scorer.py)
│   │   ├── email_outreach.py  # SMTP logic and HTML template builder
│   │   ├── form_scorer.py     # Rule-based form scoring (form_scorer.py)
│   │   └── sharepoint.py      # MSAL auth, Graph API, and file sync (sharepoint_helper.py)
│   ├── routes/                # Presentation Layer (Flask Blueprints)
│   │   ├── __init__.py
│   │   ├── auth.py            # /login, /register, /logout
│   │   ├── views.py           # UI routes returning HTML (/, /screener, /qa, etc.)
│   │   ├── api_analysis.py    # /api/analyze, /api/analyze/bulk, /api/progress
│   │   ├── api_candidates.py  # /api/candidates, /api/candidate/status, /api/outreach
│   │   ├── api_sharepoint.py  # /api/sp/files, /api/sp/content, /api/sp/match-folder
│   │   └── api_qa.py          # /api/qa/transcribe, /api/qa/evaluate, /api/qa/results
│   ├── utils/                 # Shared Helpers
│   │   ├── __init__.py
│   │   └── helpers.py         # Slug normalizers, text extractors, regex tools
│   ├── templates/             # HTML Templates
│   └── static/                # CSS/JS Assets
```