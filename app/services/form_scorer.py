# app/services/form_scorer.py — Rule-based form score calculator comparing JD requirements to candidate form responses.

import re

# ── Compiled Regex Patterns for Performance ───────────────────────────────────
# Pre-compiling regexes saves significant CPU cycles during bulk scoring.
RE_TARGET_CITY = [
    re.compile(r"location[:\s]+([A-Za-z\s]+)", re.IGNORECASE),
    re.compile(r"based\s+in[:\s]+([A-Za-z\s,]+)", re.IGNORECASE),
    re.compile(r"office[:\s]+(?:at|in)[:\s]+([A-Za-z\s]+)", re.IGNORECASE),
    re.compile(r"work\s+from[:\s]+([A-Za-z\s]+)", re.IGNORECASE),
]
RE_IMMEDIATE_JD = re.compile(
    r"immediate\s+(joiner|joining|availability)", re.IGNORECASE
)
RE_NOTICE_DAYS = re.compile(r"notice\s*period[^.]*?(\d+)\s*(day|days)", re.IGNORECASE)
RE_NOTICE_MONTHS = re.compile(
    r"notice\s*period[^.]*?(\d+)\s*(month|months)", re.IGNORECASE
)

RE_SALARY_RANGE = re.compile(
    r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*lpa", re.IGNORECASE
)
RE_SALARY_MAX = re.compile(
    r"(?:up\s*to|upto|max(?:imum)?)[:\s]+(\d+(?:\.\d+)?)\s*lpa", re.IGNORECASE
)
RE_SALARY_PLAIN = re.compile(r"(\d+(?:\.\d+)?)\s*lpa", re.IGNORECASE)

RE_FORM_DAY = re.compile(r"(\d+)\s*day")
RE_FORM_MONTH = re.compile(r"(\d+)\s*month")
RE_FORM_WEEK = re.compile(r"(\d+)\s*week")
RE_FORM_DIGIT_ONLY = re.compile(r"^\s*(\d+)\s*$")

RE_FORM_SALARY_LPA = re.compile(r"(\d+(?:\.\d+)?)\s*lpa")
RE_FORM_SALARY_L = re.compile(r"(\d+(?:\.\d+)?)\s*l\b")
RE_FORM_SALARY_RAW = re.compile(r"(\d+(?:\.\d+)?)")
RE_YES = re.compile(r"\byes\b", re.IGNORECASE)


# ── City Aliases ──────────────────────────────────────────────────────────────
CITY_ALIASES = {
    "baroda": "vadodara",
    "vdr": "vadodara",
    "ahmedabad": "ahmedabad",
    "amd": "ahmedabad",
    "bombay": "mumbai",
    "bangalore": "bengaluru",
    "blr": "bengaluru",
    "pune": "pune",
    "delhi": "delhi",
    "ncr": "delhi",
}


def _normalize_city(name: str) -> str:
    """Lowercase, strip, and resolve city aliases."""
    name = name.lower().strip()
    return CITY_ALIASES.get(name, name)


# ── JD Parsers ────────────────────────────────────────────────────────────────


def _extract_target_city(jd_text: str) -> str | None:
    if not jd_text:
        return None
    for pattern in RE_TARGET_CITY:
        m = pattern.search(jd_text)
        if m:
            city = m.group(1).strip().split(",")[0].split("\n")[0].strip()
            if 3 <= len(city) <= 30:
                return _normalize_city(city)
    return None


def _extract_max_notice_days(jd_text: str) -> int | None:
    if not jd_text:
        return None
    if RE_IMMEDIATE_JD.search(jd_text):
        return 0
    m = RE_NOTICE_DAYS.search(jd_text)
    if m:
        return int(m.group(1))
    m = RE_NOTICE_MONTHS.search(jd_text)
    if m:
        return int(m.group(1)) * 30
    return None


def _extract_max_salary_lpa(jd_text: str) -> float | None:
    if not jd_text:
        return None
    m = RE_SALARY_RANGE.search(jd_text)
    if m:
        return float(m.group(2))
    m = RE_SALARY_MAX.search(jd_text)
    if m:
        return float(m.group(1))
    m = RE_SALARY_PLAIN.search(jd_text)
    if m:
        return float(m.group(1))
    return None


# ── Form Response Parsers ─────────────────────────────────────────────────────


def _parse_notice_days(value: str) -> int | None:
    if not value:
        return None
    v = value.lower().strip()
    if "immediate" in v or ("serving" in v and "0" in v):
        return 0
    m = RE_FORM_DAY.search(v)
    if m:
        return int(m.group(1))
    m = RE_FORM_MONTH.search(v)
    if m:
        return int(m.group(1)) * 30
    m = RE_FORM_WEEK.search(v)
    if m:
        return int(m.group(1)) * 7
    m = RE_FORM_DIGIT_ONLY.search(v)
    if m:
        return int(m.group(1))
    return None


def _parse_salary_lpa(value: str) -> float | None:
    if not value:
        return None
    v = value.lower().strip()
    m = RE_FORM_SALARY_LPA.search(v)
    if m:
        return float(m.group(1))
    m = RE_FORM_SALARY_L.search(v)
    if m:
        return float(m.group(1))
    m = RE_FORM_SALARY_RAW.search(v)
    if m:
        val = float(m.group(1))
        return val / 100000 if val > 1000 else val
    return None


# ── Main Scoring Function ─────────────────────────────────────────────────────


def calculate_form_score(
    form_responses: dict, jd_text: str = "", custom_weights: dict = None
) -> dict:
    if not form_responses:
        return {"score": None, "breakdown": []}

    # ── WEIGHTS & NORMALIZATION ───────────────────────────────────────────────
    DEFAULT_WEIGHTS = {
        "Location": 25,
        "Willing to Relocate": 20,
        "Notice Period": 25,
        "Expected Salary": 15,
        "Immediate Joiner": 15,
    }

    base_weights = custom_weights if custom_weights else DEFAULT_WEIGHTS
    total_input = sum(base_weights.values())

    if total_input > 0:
        WEIGHTS = {k: (v / total_input) * 100 for k, v in base_weights.items()}
    else:
        WEIGHTS = DEFAULT_WEIGHTS

    # ── Extract JD targets ────────────────────────────────────────────────────
    target_city = _extract_target_city(jd_text)
    max_notice_days = _extract_max_notice_days(jd_text)
    max_salary_lpa = _extract_max_salary_lpa(jd_text)

    # ── Multi-key form field lookups (Optimized) ──────────────────────────────
    # Pre-process the dictionary keys once to avoid O(N*M) lowercasing operations
    normalized_responses = {k.lower().strip(): v for k, v in form_responses.items()}

    def _get(keys):
        """Return the first non-empty value matching any key (case-insensitive)."""
        for k_lower, v in normalized_responses.items():
            for key in keys:
                if key.lower() in k_lower:
                    if v and str(v).strip():
                        return str(v).strip()
        return None

    location_val = _get(["current location", "city", "location"])
    relocate_val = _get(["relocate", "relocation"])
    notice_val = _get(["notice period", "notice", "joining time"])
    salary_val = _get(["expected salary", "expected ctc", "ctc", "salary"])
    immediate_val = _get(["immediate joiner", "immediately"])

    results = {}

    # 1. Location
    if location_val and target_city:
        candidate_city = _normalize_city(location_val)
        match = target_city in candidate_city or candidate_city in target_city
        results["Location"] = {
            "points": WEIGHTS["Location"] if match else 0,
            "max": WEIGHTS["Location"],
            "value": location_val,
            "match": match,
        }
    else:
        results["Location"] = None

    # 2. Willing to Relocate
    if relocate_val:
        match = RE_YES.search(relocate_val) is not None
        results["Willing to Relocate"] = {
            "points": WEIGHTS["Willing to Relocate"] if match else 0,
            "max": WEIGHTS["Willing to Relocate"],
            "value": relocate_val,
            "match": match,
        }
    else:
        results["Willing to Relocate"] = None

    # 3. Notice Period
    if notice_val and max_notice_days is not None:
        candidate_notice = _parse_notice_days(notice_val)
        if candidate_notice is not None:
            match = candidate_notice <= max_notice_days
            results["Notice Period"] = {
                "points": WEIGHTS["Notice Period"] if match else 0,
                "max": WEIGHTS["Notice Period"],
                "value": notice_val,
                "match": match,
            }
        else:
            results["Notice Period"] = None
    else:
        results["Notice Period"] = None

    # 4. Expected Salary
    if salary_val and max_salary_lpa is not None:
        candidate_salary = _parse_salary_lpa(salary_val)
        if candidate_salary is not None:
            match = candidate_salary <= max_salary_lpa
            results["Expected Salary"] = {
                "points": WEIGHTS["Expected Salary"] if match else 0,
                "max": WEIGHTS["Expected Salary"],
                "value": salary_val,
                "match": match,
            }
        else:
            results["Expected Salary"] = None
    else:
        results["Expected Salary"] = None

    # 5. Immediate Joiner
    if immediate_val:
        match = RE_YES.search(immediate_val) is not None
        results["Immediate Joiner"] = {
            "points": WEIGHTS["Immediate Joiner"] if match else 0,
            "max": WEIGHTS["Immediate Joiner"],
            "value": immediate_val,
            "match": match,
        }
    else:
        results["Immediate Joiner"] = None

    # ── Calculate Score with Weight Redistribution ────────────────────────────
    present = {k: v for k, v in results.items() if v is not None}

    if not present:
        return {"score": None, "breakdown": []}

    total_weight = sum(v["max"] for v in present.values())
    total_earned = sum(v["points"] for v in present.values())
    score = round((total_earned / total_weight) * 100) if total_weight > 0 else 0

    breakdown = [
        {
            "parameter": k,
            "value": v["value"],
            "match": v["match"],
            "points": v["points"],
            "max": v["max"],
        }
        for k, v in present.items()
    ]

    return {"score": score, "breakdown": breakdown}
