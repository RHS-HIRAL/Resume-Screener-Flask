"""
app/services/jd_sync.py
JD sync pipeline (Pipeline 3):
  Scrape JD website → parse job details → generate branded PDF + TXT
  → upload to SharePoint.
"""

import os
import re
import shutil
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config import Config
from app.services.sharepoint_sync import (
    GraphAuthProvider,
    SyncSharePointManager,
    JobDescription,
    logger,
)

# ── Optional dependencies ──────────────────────────────────────────────────────
try:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


# ══════════════════════════════════════════════════════════════════════════════
#  JD SCRAPER
# ══════════════════════════════════════════════════════════════════════════════

_scrape_session = requests.Session()
_scrape_session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
)

META_LABEL_MAP = {
    "location": "location",
    "job type": "job_type",
    "internship type": "job_type",
    "department": "department",
    "shift": "shifts",
    "experience": "experience",
    "job category": "job_category",
    "employment type": "employment_type",
    "job title": "_skip",
    "internship title": "_skip",
    "job location": "location",
}

SECTION_KEYWORDS = [
    "job summary",
    "role overview",
    "job description",
    "position summary",
    "key responsibilities",
    "responsibilities",
    "must have skills",
    "must-have skills",
    "required skills",
    "technical skills",
    "knowledge",
    "good to have",
    "preferred qualifications",
    "nice to have",
    "qualifications",
    "education",
    "candidate requirements",
    "certifications",
]


def _polite_get(url: str) -> requests.Response:
    time.sleep(1.5)
    resp = _scrape_session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


def discover_job_urls() -> list:
    jobs: dict = {}
    current_url = Config.CAREERS_URL
    page_num = 0
    while current_url and page_num < 20:
        page_num += 1
        try:
            resp = _polite_get(current_url)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                break
            raise
        soup = BeautifulSoup(resp.text, "html.parser")
        for h2 in soup.find_all("h2"):
            a = h2.find("a", href=True)
            if not a or "/jobs/" not in a["href"]:
                continue
            full_url = urljoin(Config.SITE_BASE_URL, a["href"]).rstrip("/") + "/"
            slug = full_url.rstrip("/").split("/jobs/")[-1].split("/")[0]
            if not slug or slug == "page":
                continue
            if full_url not in jobs:
                jobs[full_url] = a.get_text(strip=True)
        next_link = None
        for a in soup.find_all("a", href=True):
            if (
                "next" in a.get_text(strip=True).lower() or "→" in a.get_text()
            ) and "/jobs/page/" in a["href"]:
                next_link = urljoin(Config.SITE_BASE_URL, a["href"])
                break
        if next_link and next_link != current_url:
            current_url = next_link
        else:
            break
    return [{"url": url, "title": title} for url, title in jobs.items()]


def _is_section_heading(text: str) -> bool:
    if not text or len(text) < 3 or len(text) > 80:
        return False
    tl = text.lower()
    skip = {
        "apply",
        "submit",
        "home",
        "careers",
        "full name",
        "email",
        "phone",
        "cover letter",
        "upload",
        "contact",
        "si2 technologies",
    }
    if tl in skip:
        return False
    if any(kw in tl for kw in SECTION_KEYWORDS):
        return True
    if ":" not in text and len(text) < 40 and text[0].isupper():
        return True
    return False


def _try_set_metadata(jd: JobDescription, label: str, value: str) -> bool:
    label_lower = label.lower().strip()
    for key_substr, attr_name in META_LABEL_MAP.items():
        if key_substr in label_lower:
            if attr_name == "_skip":
                return True
            if not getattr(jd, attr_name):
                setattr(jd, attr_name, value)
            return True
    return False


def parse_job_detail(url: str, fallback_title: str = "") -> JobDescription:
    resp = _polite_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    slug = url.rstrip("/").split("/")[-1]
    jd = JobDescription(
        slug=slug, url=url, scraped_date=datetime.now().strftime("%Y-%m-%d")
    )
    h1 = soup.find("h1")
    jd.title = h1.get_text(strip=True) if h1 else fallback_title
    content_div = soup.find("div", class_="entry-content") or soup.find("article")
    if not content_div:
        return jd
    current_heading = "Job Description"
    current_paragraphs: list = []
    current_bullets: list = []

    def flush():
        nonlocal current_heading, current_paragraphs, current_bullets
        if current_paragraphs or current_bullets:
            jd.sections.append(
                {
                    "heading": current_heading,
                    "paragraphs": list(current_paragraphs),
                    "bullets": list(current_bullets),
                }
            )
        current_paragraphs.clear()
        current_bullets.clear()

    processed = set()
    for elem in content_div.find_all(
        ["p", "ul", "ol", "h2", "h3", "h4", "h5", "div"], recursive=True
    ):
        if elem in processed:
            continue
        for child in elem.find_all(["p", "ul", "ol", "h2", "h3", "h4", "h5"]):
            processed.add(child)
        text_content = elem.get_text(strip=True)
        if not text_content:
            continue
        tag = elem.name.lower()
        if tag in ("h2", "h3", "h4", "h5") or (
            tag == "p" and _is_section_heading(text_content)
        ):
            if "apply for this position" in text_content.lower():
                break
            flush()
            current_heading = text_content.rstrip(":")
        elif tag == "p":
            m = re.match(r"^([A-Za-z\s]+)\s*[:]\s*(.*)", text_content)
            if m and _try_set_metadata(jd, m.group(1), m.group(2)):
                continue
            current_paragraphs.append(elem.decode_contents())
        elif tag in ("ul", "ol"):
            for li in elem.find_all("li", recursive=False):
                li_text = li.get_text(" ", strip=True)
                if li_text:
                    current_bullets.append(li_text)
    flush()
    return jd


# ══════════════════════════════════════════════════════════════════════════════
#  JD TEXT CONVERSION
# ══════════════════════════════════════════════════════════════════════════════


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _bullets_to_prose(bullets: list) -> str:
    cleaned = [b.strip(" .,") for b in bullets if b.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0] + "."
    return ", ".join(cleaned[:-1]) + ", and " + cleaned[-1] + "."


def jd_to_text(jd: JobDescription) -> str:
    lines = []
    meta_parts = []
    for label, val in [
        ("Job Title", jd.title),
        ("Location", jd.location),
        ("Job Type", jd.job_type),
        ("Department", jd.department),
        ("Experience Required", jd.experience),
    ]:
        if val:
            meta_parts.append(f"{label}: {val}")
    if meta_parts:
        lines.append(". ".join(meta_parts) + ".")
    for section in jd.sections:
        heading = section.get("heading", "").strip()
        paragraphs = [
            _strip_html(p) for p in section.get("paragraphs", []) if p.strip()
        ]
        bullets = [_strip_html(b) for b in section.get("bullets", []) if b.strip()]
        parts = []
        if paragraphs:
            parts.append(" ".join(paragraphs))
        if bullets:
            parts.append(_bullets_to_prose(bullets))
        if parts:
            body = " ".join(parts)
            lines.append(f"{heading}: {body}" if heading else body)
    return "\n\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  PDF GENERATOR  (requires reportlab)
# ══════════════════════════════════════════════════════════════════════════════


def _safe_xml(text: str) -> str:
    if not text:
        return ""
    t = BeautifulSoup(text, "html.parser").get_text()
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return t


def generate_job_pdf(jd: JobDescription, output_path: str) -> str:
    if not HAS_REPORTLAB:
        raise RuntimeError("reportlab is not installed. Cannot generate PDF.")
    C_PRIMARY = HexColor("#0F3A68")
    C_ACCENT = HexColor("#1976D2")
    C_TEXT = HexColor("#222222")
    C_SUBTLE = HexColor("#555555")
    C_DIVIDER = HexColor("#B0C4DE")

    base = getSampleStyleSheet()
    styles = {
        "CompanyName": ParagraphStyle(
            "CompanyName",
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=C_ACCENT,
            spaceAfter=0,
        ),
        "Title": ParagraphStyle(
            "JDTitle",
            parent=base["Title"],
            fontSize=18,
            leading=24,
            textColor=C_PRIMARY,
            spaceAfter=4,
            alignment=TA_LEFT,
            fontName="Helvetica-Bold",
        ),
        "SectionHeading": ParagraphStyle(
            "SectionHeading",
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=17,
            textColor=C_PRIMARY,
            spaceBefore=14,
            spaceAfter=5,
        ),
        "Body": ParagraphStyle(
            "Body",
            fontName="Helvetica",
            fontSize=9.5,
            leading=14.5,
            textColor=C_TEXT,
            alignment=TA_JUSTIFY,
            spaceAfter=5,
        ),
        "Bullet": ParagraphStyle(
            "Bullet",
            fontName="Helvetica",
            fontSize=9.5,
            leading=14,
            textColor=C_TEXT,
            leftIndent=18,
            bulletIndent=6,
            spaceAfter=3,
        ),
        "Footer": ParagraphStyle(
            "Footer",
            fontName="Helvetica",
            fontSize=7,
            textColor=C_SUBTLE,
            alignment=TA_CENTER,
        ),
    }

    story = []
    story.append(Paragraph("Si2 Technologies", styles["CompanyName"]))
    story.append(
        HRFlowable(width="100%", thickness=2.5, color=C_PRIMARY, spaceAfter=10)
    )
    story.append(Paragraph(_safe_xml(jd.title) or "Job Description", styles["Title"]))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_DIVIDER, spaceAfter=4))

    for section in jd.sections:
        paragraphs = section.get("paragraphs", [])
        bullets = section.get("bullets", [])
        if not paragraphs and not bullets:
            continue
        accent_hex = C_ACCENT.hexval()[2:]
        story.append(
            Paragraph(
                f'<font color="#{accent_hex}">|</font>&nbsp;&nbsp;{_safe_xml(section["heading"])}',
                styles["SectionHeading"],
            )
        )
        for p in paragraphs:
            story.append(Paragraph(_safe_xml(p), styles["Body"]))
        for b in bullets:
            story.append(Paragraph(f"\u2022  {_safe_xml(b)}", styles["Bullet"]))

    story.append(Spacer(1, 20))
    story.append(
        HRFlowable(
            width="100%", thickness=0.5, color=C_DIVIDER, spaceBefore=8, spaceAfter=6
        )
    )
    story.append(
        Paragraph(
            f"Source: {_safe_xml(jd.url)}  |  Scraped: {jd.scraped_date}  |  Si2 Technologies  |  Confidential",
            styles["Footer"],
        )
    )

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title=jd.title or "Job Description",
        author="Si2 Technologies - JD Pipeline",
    )
    doc.build(story)
    return output_path


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINE 3 — JD Scrape → PDF → Text Upload
# ══════════════════════════════════════════════════════════════════════════════


def run_jd_pipeline(auth: GraphAuthProvider) -> dict:
    logger.info("=== PIPELINE 3: JD SCRAPE → PDF → TEXT ===")
    headers = auth.get_headers()
    sp = SyncSharePointManager(headers)

    job_urls = discover_job_urls()
    logger.info("Found %d job listings.", len(job_urls))
    if not job_urls:
        return {
            "uploaded": 0,
            "skipped": 0,
            "failed": 0,
            "text_uploaded": 0,
            "text_skipped": 0,
        }

    os.makedirs(Config.SYNC_TEMP_JD_DIR, exist_ok=True)
    results = {
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "text_uploaded": 0,
        "text_skipped": 0,
    }

    for job_info in job_urls:
        url = job_info["url"]
        title = job_info.get("title", "")
        slug = url.rstrip("/").split("/")[-1]
        safe_slug = re.sub(r"[^\w\-]", "", slug)
        pdf_filename = f"JD_{safe_slug}.pdf"
        txt_filename = f"JD_{safe_slug}.txt"
        txt_remote = f"{Config.SHAREPOINT_TEXT_JD_FOLDER.strip('/')}/{txt_filename}"

        if sp.jd_pdf_exists(pdf_filename):
            results["skipped"] += 1
            if sp.file_exists(txt_remote):
                results["text_skipped"] += 1
            else:
                try:
                    jd = parse_job_detail(url, fallback_title=title)
                    text_content = jd_to_text(jd)
                    if text_content.strip():
                        sp.upload_jd_text(
                            text_content,
                            txt_filename,
                            metadata={"Title": jd.title, "JDTitle": jd.title},
                            skip_existing=False,
                        )
                        results["text_uploaded"] += 1
                except Exception as e:
                    logger.error("Text-only upload failed for %s: %s", slug, e)
            continue

        try:
            jd = parse_job_detail(url, fallback_title=title)
        except Exception as e:
            logger.error("Parse error for %s: %s", url, e)
            results["failed"] += 1
            continue

        # PDF upload
        local_pdf_path = os.path.join(Config.SYNC_TEMP_JD_DIR, pdf_filename)
        try:
            generate_job_pdf(jd, local_pdf_path)
            jd_metadata = {
                "JDTitle": jd.title,
                "JDLocation": jd.location,
                "JDJobType": " / ".join(
                    filter(None, [jd.job_type, jd.employment_type])
                ),
                "JDDepartment": jd.department,
                "JDExperience": jd.experience,
                "JDJobCategory": jd.job_category,
                "JDScrapedDate": jd.scraped_date,
                "JDSourceURL": jd.url,
            }
            sp.upload_jd_pdf(local_pdf_path, pdf_filename, jd_metadata)
            results["uploaded"] += 1
        except Exception as e:
            logger.error("PDF generate/upload failed for %s: %s", slug, e)
            results["failed"] += 1
        finally:
            if os.path.exists(local_pdf_path):
                try:
                    os.remove(local_pdf_path)
                except OSError:
                    pass

        # Text upload
        try:
            text_content = jd_to_text(jd)
            if text_content.strip():
                resp = sp.upload_jd_text(
                    text_content,
                    txt_filename,
                    metadata={"Title": jd.title, "JDTitle": jd.title},
                )
                if resp is None:
                    results["text_skipped"] += 1
                else:
                    results["text_uploaded"] += 1
        except Exception as e:
            logger.error("Text upload failed for %s: %s", slug, e)

    try:
        shutil.rmtree(Config.SYNC_TEMP_JD_DIR, ignore_errors=True)
    except OSError:
        pass

    logger.info(
        "Pipeline 3 done. PDFs: %d uploaded, %d skipped | Text: %d uploaded",
        results["uploaded"],
        results["skipped"],
        results["text_uploaded"],
    )
    return results
