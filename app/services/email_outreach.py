import ssl
import smtplib
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import render_template
from config import Config
from app.db.candidates import mark_outreach_sent


def build_email_html(
    candidate_name: str,
    jd_title: str,
    jd_text: str,
    form_link: str,
    custom_message: str,
) -> str:
    """
    Renders the email HTML dynamically using Jinja2 templates.
    Must be called synchronously from the route (while Flask request context is active).
    """
    return render_template(
        "email/outreach.html",
        candidate_name=candidate_name,
        jd_title=jd_title,
        jd_text=jd_text.replace(
            "\n", "<br>"
        ),  # Converts pure text linebreaks to HTML breaks
        form_link=form_link,
        custom_message=custom_message,
    )


def _send_emails_worker(payloads: list[dict]):
    """
    Background worker that pools an SMTP connection and sends all emails in one batch.
    Payload expected format:
    { "candidate_id": 1, "to_email": "x@x.com", "to_name": "Bob", "subject": "Hi", "html_body": "...", "form_link": "..." }
    """
    if not payloads:
        return

    if not Config.SMTP_USER or not Config.SMTP_PASSWORD:
        print("[EMAIL ERROR] SMTP credentials not configured in .env.")
        return

    try:
        # 1. Open the SMTP connection ONCE
        context = ssl.create_default_context()
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)

            # 2. Iterate and send all emails using the single connection
            for payload in payloads:
                cid = payload.get("candidate_id")
                to_email = payload.get("to_email")
                to_name = payload.get("to_name", "")

                msg = MIMEMultipart("alternative")
                msg["Subject"] = payload.get("subject", "Exciting Opportunity")
                msg["From"] = f"{Config.SMTP_FROM_NAME} <{Config.SMTP_USER}>"
                msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
                msg.attach(MIMEText(payload.get("html_body", ""), "html", "utf-8"))

                try:
                    server.sendmail(Config.SMTP_USER, to_email, msg.as_string())
                    print(f"[EMAIL] Sent successfully to {to_email}")

                    # 3. Update the database on success (Thread-safe thanks to psycopg2 pool)
                    if cid:
                        mark_outreach_sent(cid, payload.get("form_link", ""))
                except Exception as e:
                    print(f"[EMAIL ERROR] Failed to send to {to_email}: {e}")

    except Exception as e:
        print(f"[EMAIL ERROR] SMTP Server Connection failed: {e}")


def send_bulk_outreach_async(email_payloads: list[dict]):
    """
    Triggers the background thread to send emails. This returns immediately so the API doesn't block.
    """
    # Start thread and mark as daemon so it won't prevent the server from shutting down
    thread = threading.Thread(target=_send_emails_worker, args=(email_payloads,))
    thread.daemon = True
    thread.start()
