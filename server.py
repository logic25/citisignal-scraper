"""BIS Web Scraper for CitiSignal.

Standalone Playwright service that scrapes BIS property profiles
and job filings. CitiSignal's edge functions call this API.

Separate from dob-agent (Ordino) — each product has its own scraper.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

PORT = int(os.environ.get("PORT", 8080))
SCRAPER_SECRET = os.environ.get("SCRAPER_SECRET", "")


def log(msg):
    print(f"[BIS-SCRAPER] {datetime.utcnow().isoformat()} {msg}", flush=True)
    sys.stdout.flush()


def verify_auth(req):
    secret = req.headers.get("X-Scraper-Secret", "")
    if not secret or not SCRAPER_SECRET:
        return False
    return secret == SCRAPER_SECRET


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "citisignal-bis-scraper",
        "secret_configured": bool(SCRAPER_SECRET),
    })


@app.route("/api/scrape", methods=["POST"])
def scrape_bis():
    """Scrape BIS website using Playwright.

    Actions:
    - profile: Property Profile page (vacate orders, restrictions, counts)
    - jobs: Jobs/Filings by location (all docs including PAAs)
    - job_detail: Single job detail (all doc numbers for one job)
    """
    if not verify_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.json or {}
    action = body.get("action", "profile")
    bin_number = body.get("bin")
    boro = body.get("boro")
    block = body.get("block")
    lot = body.get("lot")
    job_number = body.get("job_number")
    debug = body.get("debug", False)

    if action == "profile" and not bin_number:
        return jsonify({"error": "bin is required for profile action"}), 400
    if action == "jobs" and not bin_number:
        return jsonify({"error": "bin is required for jobs action"}), 400
    if action == "job_detail" and not job_number:
        return jsonify({"error": "job_number is required for job_detail action"}), 400

    try:
        from playwright.sync_api import sync_playwright

        log(f"Scrape: action={action}, bin={bin_number}, job={job_number}")

        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )

        result = {}

        if action == "profile":
            result = scrape_profile(page, bin_number, boro, block, lot, debug)
        elif action == "jobs":
            result = scrape_jobs_by_location(page, bin_number, debug, boro, block, lot)
        elif action in ("job_detail", "job"):
            result = scrape_job_detail(page, job_number, bin_number, debug)
        else:
            result = {"error": f"Unknown action: {action}"}

        browser.close()
        pw.stop()
        return jsonify(result)

    except Exception as e:
        log(f"Scrape ERROR: {e}")
        return jsonify({"error": str(e)}), 500


def navigate_bis_search(page, bin_number=None, boro=None, block=None, lot=None):
    """Navigate to BIS Property Profile via the search form.

    Direct URLs to PropertyProfileOverviewServlet get redirected
    to the homepage. We must go through the search form.
    """
    search_url = "https://a810-bisweb.nyc.gov/bisweb/bsqpm01.jsp"
    log(f"Navigating to BIS search page...")
    page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
    time.sleep(1)

    if bin_number:
        log(f"Searching by BIN {bin_number}")
        try:
            page.fill('input[name="bin"]', str(bin_number), timeout=10000)
            page.click('input[name="go4"]', timeout=5000)
            time.sleep(2)
            log(f"After BIN search, URL: {page.url}")
        except Exception as e:
            log(f"BIN search form failed ({e}), trying direct URL with cookies...")
            url = f"https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?allbin={bin_number}&requestid=0"
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            time.sleep(2)
            log(f"After direct URL, URL: {page.url}")
    elif block and lot:
        boro_val = boro or "1"
        log(f"Searching by BBL: boro={boro_val} block={block} lot={lot}")
        page.select_option('select[name="allborough"]', boro_val)
        page.fill('input[name="allblock"]', str(block))
        page.fill('input[name="alllot"]', str(lot))
        page.click('input[name="go5"]', timeout=5000)
        time.sleep(2)
        log(f"After BBL search, URL: {page.url}")
    else:
        raise ValueError("Need bin or block+lot to search BIS")


def scrape_profile(page, bin_number, boro, block, lot, debug=False):
    """Scrape BIS Property Profile Overview page."""
    try:
        navigate_bis_search(page, bin_number, boro, block, lot)
    except Exception as e:
        log(f"Search form error: {e}")
        url = f"https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?boro={boro or '1'}&block={block or ''}&lot={lot or ''}"
        page.goto(url, timeout=15000, wait_until="domcontentloaded")
        time.sleep(2)

    html = page.content()

    if "Access Denied" in html:
        return {"error": "Access denied by Akamai", "blocked": True}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    # Verify we got the property profile page
    if "Property Profile" not in html:
        return {"error": "Did not reach Property Profile page", "page_url": page.url}

    # Vacate order
    vacate_order = False
    vacate_type = None
    if "FULL VACATE EXISTS" in html.upper():
        vacate_order = True
        vacate_type = "full"
    elif "PARTIAL VACATE EXISTS" in html.upper():
        vacate_order = True
        vacate_type = "partial"

    # Counts
    counts = {}

    m = re.search(r'Complaints</a></b></td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>', html, re.DOTALL)
    counts["complaints_total"] = int(m.group(1)) if m else 0
    counts["complaints_open"] = int(m.group(2)) if m else 0

    m = re.search(r'Violations-DOB</a></b></td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>', html, re.DOTALL)
    counts["violations_dob_total"] = int(m.group(1)) if m else 0
    counts["violations_dob_open"] = int(m.group(2)) if m else 0

    m = re.search(r'Violations-OATH/ECB</a></b></td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>', html, re.DOTALL)
    counts["violations_ecb_total"] = int(m.group(1)) if m else 0
    counts["violations_ecb_open"] = int(m.group(2)) if m else 0

    m = re.search(r'Total Jobs</b></td>\s*<td[^>]*>\s*(\d+)\s*</td>', html, re.DOTALL)
    counts["jobs_total"] = int(m.group(1)) if m else 0

    m = re.search(r'Actions</a></b></td>\s*<td[^>]*>\s*(\d+)\s*</td>', html, re.DOTALL)
    counts["actions_total"] = int(m.group(1)) if m else 0

    # Restrictions
    restrictions = {}
    patterns = [
        ("landmark_status", r'Landmark Status:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("special_status", r'Special Status:</b></td>\s*<td[^>]*[^>]*>(.*?)</td>'),
        ("local_law", r'Local Law:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("loft_law", r'Loft Law:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("sro_restricted", r'SRO Restricted:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("ta_restricted", r'TA Restricted:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("ub_restricted", r'UB Restricted:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("environmental_restrictions", r'Environmental Restrictions:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("city_owned", r'City Owned:</b></td>\s*<td[^>]*[^>]*>(.*?)</td>'),
        ("legal_adult_use", r'Legal Adult Use:</b></td>\s*<td[^>]*>(.*?)</td>'),
        ("hpd_multiple_dwelling", r'HPD Multiple Dwelling:</b></td>\s*<td[^>]*[^>]*>(.*?)\s*</td>'),
        ("building_classification", r'Department of Finance Building Classification:</b></td>\s*<td[^>]*[^>]*>(.*?)</td>'),
    ]
    for key, pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        val = m.group(1).strip().replace('&nbsp;', ' ').strip() if m else None
        restrictions[key] = val

    m = re.search(r'Special District:.*?</b></td>\s*<td[^>]*[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
    restrictions["special_district"] = m.group(1).strip() if m else None

    # Cross streets
    cross_streets = None
    m = re.search(r'Cross Street\(s\):</b></td>\s*<td[^>]*[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
    if m:
        cross_streets = m.group(1).replace('&nbsp;', ' ').strip()

    result = {
        "bin": bin_number,
        "vacate_order": vacate_order,
        "vacate_type": vacate_type,
        "counts": counts,
        "restrictions": restrictions,
        "cross_streets": cross_streets,
        "scraped_at": datetime.utcnow().isoformat(),
    }
    log(f"Profile done. Vacate={vacate_order}, Complaints={counts.get('complaints_total', 0)}")
    return result


def scrape_jobs_by_location(page, bin_number, debug=False, boro=None, block=None, lot=None):
    """Scrape BIS Jobs/Filings page for a BIN, including PAAs."""
    try:
        # Visit BIS homepage first for session cookies
        log("Jobs: Visiting BIS homepage for cookies...")
        page.goto("https://a810-bisweb.nyc.gov/bisweb/bispi00.jsp",
                   timeout=15000, wait_until="domcontentloaded")
        time.sleep(1)

        # Go directly to Jobs page with Show All Filings (BXS1PRA3 includes PAAs)
        jobs_url = (f"https://a810-bisweb.nyc.gov/bisweb/JobsQueryByLocationServlet"
                    f"?allbin={bin_number}&allinquirytype=BXS1PRA3&requestid=0")
        log(f"Jobs: Navigating to {jobs_url}")
        page.goto(jobs_url, timeout=15000, wait_until="domcontentloaded")
        time.sleep(2)
        log(f"Jobs: Arrived at {page.url}")

        # PAAs are already included via allinquirytype=BXS1PRA3 in the URL

    except Exception as e:
        log(f"Jobs: Navigation error: {e}")
        return {"error": str(e), "jobs": []}

    html = page.content()

    if "Access Denied" in html:
        return {"error": "Access denied by Akamai", "blocked": True}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    jobs = parse_bis_jobs_table(html)
    return {
        "bin": bin_number,
        "jobs": jobs,
        "job_count": len(jobs),
        "scraped_at": datetime.utcnow().isoformat(),
    }


def scrape_job_detail(page, job_number, bin_number=None, debug=False):
    """Get all docs for a specific job number.

    JobsQueryByNumberServlet is blocked by Akamai, so we can't
    scrape the job detail page directly. Instead, if we have a BIN,
    we scrape all jobs for the property and filter to just this job.

    If no BIN is provided, we try the direct URL as a fallback.
    """
    if bin_number:
        # Use the working jobs-by-location approach and filter
        log(f"Job detail: using jobs-by-location for BIN {bin_number}, filtering to {job_number}")
        result = scrape_jobs_by_location(page, bin_number, debug)
        if debug:
            return result
        if result.get("error"):
            return {"error": result["error"], "documents": [], "job_number": job_number}

        # Filter to just this job number
        all_jobs = result.get("jobs", [])
        matching = [j for j in all_jobs if str(j.get("job_number", "")) == str(job_number)]

        withdrawn = any(j.get("withdrawn") for j in matching)

        return {
            "job_number": job_number,
            "documents": matching,
            "doc_count": len(matching),
            "withdrawn": withdrawn,
            "scraped_at": datetime.utcnow().isoformat(),
        }

    # Fallback: try direct URL (may be blocked by Akamai)
    search_url = "https://a810-bisweb.nyc.gov/bisweb/bsqpm01.jsp"
    log(f"Job detail: visiting search page for session...")
    page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
    time.sleep(1)

    job_url = (f"https://a810-bisweb.nyc.gov/bisweb/JobsQueryByNumberServlet"
               f"?passjobnumber={job_number}&passjoession=0&requestid=0")
    log(f"Job detail: navigating to {job_url}")
    try:
        page.goto(job_url, timeout=15000, wait_until="domcontentloaded")
        time.sleep(2)
    except Exception as e:
        log(f"Job detail navigation error: {e}")
        return {"error": str(e), "documents": []}

    html = page.content()

    if "Access Denied" in html:
        return {"error": "Access denied by Akamai — provide bin parameter for reliable results", "blocked": True}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    jobs = parse_bis_jobs_table(html)
    withdrawn = "WITHDRAWN" in html.upper()

    return {
        "job_number": job_number,
        "documents": jobs,
        "doc_count": len(jobs),
        "withdrawn": withdrawn,
        "scraped_at": datetime.utcnow().isoformat(),
    }


JOB_TYPE_MAP = {
    "A1": "Alteration Type 1",
    "A2": "Alteration Type 2",
    "A3": "Alteration Type 3",
    "NB": "New Building",
    "DM": "Demolition",
    "SI": "Sign",
    "FO": "Foundation",
    "SH": "Scaffold",
    "FN": "Fence",
    "EQ": "Equipment",
}


def parse_bis_jobs_table(html: str) -> list:
    """Parse BIS job/filing table HTML into structured data.

    BIS table columns:
    FILE DATE | JOB # | DOC # | JOB TYPE | JOB STATUS | STATUS DATE | LIC # | APPLICANT | IN AUDIT | ZONING APPROVAL

    Each job has a data row followed by a description row.
    Description row contains the work description and floor info.
    """
    jobs = []

    # Split into rows
    rows = re.split(r'<tr[^>]*>', html, flags=re.IGNORECASE)

    current_job = None
    for i, row in enumerate(rows):
        # Look for rows containing a job number link
        job_match = re.search(r'passjobnumber=(\d+)', row)
        date_match = re.search(r'(\d{2}/\d{2}/\d{4})', row)

        if job_match and date_match:
            # Extract ALL cell contents including empty ones (positional)
            raw_cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).replace('&nbsp;', ' ').strip() for c in raw_cells]

            job_num = job_match.group(1)

            # BIS columns are positional (0-indexed):
            # 0: FILE DATE, 1: JOB # (link), 2: DOC #, 3: JOB TYPE,
            # 4: JOB STATUS, 5: STATUS DATE, 6: LIC #, 7: LIC TYPE (PE/RA),
            # 8: APPLICANT, 9: IN AUDIT, 10: ZONING APPROVAL
            #
            # But some cells may be empty or merged. Use safe indexing.
            def cell(idx):
                return cells[idx].strip() if idx < len(cells) and cells[idx].strip() else None

            filing_date = cell(0) or date_match.group(1)
            doc_num = (cell(2) or "01").zfill(2)
            job_type = cell(3)
            job_status = cell(4)
            status_date = cell(5)

            # License: could be in cell 6, sometimes "0034627 RA" spans cells 6+7
            lic_raw = (cell(6) or "") + " " + (cell(7) or "")
            lic_match = re.search(r'(\d{6,7})\s*(PE|RA)', lic_raw)
            license_number = lic_match.group(1) if lic_match else cell(6)
            license_type = lic_match.group(2) if lic_match else None

            # Applicant name — cell 8 if lic has type, otherwise could shift
            applicant = None
            if lic_match:
                # License took cells 6+7, applicant is cell 8
                applicant = cell(8)
            else:
                # License might be just cell 6, type in 7, applicant in 8
                # Or license+type in 6, applicant in 7
                candidate = cell(8) or cell(7)
                # Make sure it's a name, not a date or status code
                if candidate and not re.match(r'^\d{2}/\d{2}', candidate) and candidate.upper() not in ['Y', 'N', '']:
                    applicant = candidate

            # Zoning approval — last cell or second to last
            zoning = None
            for idx in range(len(cells) - 1, max(8, len(cells) - 3), -1):
                c = cell(idx)
                if c and ('GRANTED' in c.upper() or 'NOT APPLICABLE' in c.upper()):
                    zoning = c
                    break

            # Extract status code
            job_status_code = None
            if job_status:
                parts = job_status.split()
                if parts:
                    job_status_code = parts[0]

            current_job = {
                "filing_date": filing_date,
                "job_number": job_num,
                "doc_number": doc_num,
                "job_type": JOB_TYPE_MAP.get(job_type, job_type) if job_type else None,
                "job_type_code": job_type,
                "job_status": job_status,
                "job_status_code": job_status_code,
                "status_date": status_date,
                "license_number": license_number,
                "license_type": license_type,
                "applicant": applicant,
                "zoning_approval": zoning,
                "description": None,
                "floors": None,
                "withdrawn": False,
                "source": "BIS_SCRAPE",
            }
            jobs.append(current_job)

        elif current_job:
            # Check if this is a description row
            text = re.sub(r'<[^>]+>', ' ', row).strip()
            text = re.sub(r'\s+', ' ', text).strip()

            if text and len(text) > 10 and 'FILE DATE' not in text and 'JOB #' not in text:
                current_job["description"] = text

                # Extract floor info
                floor_match = re.search(r'Work on Floor\(s\):\s*(.*?)(?:\s*$)', text, re.IGNORECASE)
                if floor_match:
                    current_job["floors"] = floor_match.group(1).strip()

                # Check for withdrawn
                if "WITHDRAWN" in text.upper():
                    current_job["withdrawn"] = True
                    # Also update status if not already set
                    if current_job["job_status"] and "WITHDRAWN" not in current_job["job_status"].upper():
                        current_job["job_status"] = current_job["job_status"] + " (WITHDRAWN)"

                current_job = None

    log(f"Parsed {len(jobs)} jobs from HTML")
    return jobs


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
