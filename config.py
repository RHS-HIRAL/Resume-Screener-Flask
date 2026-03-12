# config.py — Centralized configuration loader for the application environment.

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration variables."""

    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "super-secret-key-change-me")

    # Database Configuration
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = int(os.getenv("PG_PORT", "5433"))
    PG_DATABASE = os.getenv("PG_DATABASE", "resume_screener")
    PG_USER = os.getenv("PG_USER", "postgres")
    PG_PASSWORD = os.getenv("PG_PASSWORD", "")

    # API Keys
    GOOGLE_API_KEYS = [
        key
        for key in [
            os.getenv(f"GOOGLE_API_KEY{i if i > 1 else ''}", "") for i in range(1, 11)
        ]
        if key
    ]
    SARVAM_API_KEYS = [
        key
        for key in [
            os.getenv(f"SARVAM_API_KEY{i if i > 1 else ''}", "") for i in range(1, 3)
        ]
        if key
    ]

    # SMTP / Email Outreach Configuration
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "HR Team")

    # SharePoint & Microsoft Graph Configuration
    AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
    AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
    AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
    SHAREPOINT_SITE_DOMAIN = os.getenv("SHAREPOINT_SITE_DOMAIN")
    SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH")
    SHAREPOINT_DRIVE_NAME = os.getenv("SHAREPOINT_DRIVE_NAME")
    SHAREPOINT_BASE_FOLDER = os.getenv("SHAREPOINT_BASE_FOLDER", "Resumes")
    SHAREPOINT_JD_FOLDER = os.getenv("SHAREPOINT_JD_FOLDER", "Text Files/jobDescription")
    SHAREPOINT_RESUME_TXT_FOLDER = os.getenv("SHAREPOINT_RESUME_TXT_FOLDER", "Text Files/Resumes")
    MAILBOX_USER = os.getenv("MAILBOX_USER")
