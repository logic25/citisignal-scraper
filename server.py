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
        elif action == "job_detail":
            result = scrape_job_detail(page, job_number, debug)
        else:
            result = {"error": f"Unknown action: {action}"}

        browser.close()
        pw.stop()
        return jsonify(result)

    except Exception as e:
        log(f"Scrape ERROR: {e}")
        return jsonify({"error": str(e)}), 500


def navigate_bis_search(page, bin_number=None, boro=None, block=None, lot=None):
    """Navigate to BIS via the search form (required to get past Akamai)."""
    search_url = "https://a810-bisweb.nyc.gov/bisweb/bsqpm01.jsp"
    log(f"Navigating to BIS search page...")
    page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
    time.sleep(1)

    if bin_number:
        log(f"Searching by BIN {bin_number}")
        # BIS has two BIN fields: "bin" (Property Profile section) and "allbin" (another section)
        # Try "bin" first (with go4 submit), fallback to "allbin" (with go8 submit)
        try:
            page.fill('input[name="bin"]', str(bin_number), timeout=10000)
            page.click('input[name="go4"]', timeout=5000)
            time.sleep(2)
            log(f"Searched via bin field, URL: {page.url}")
            return
        except Exception as e1:
            log(f"bin field failed ({e1}), trying allbin...")
            try:
                page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
                time.sleep(1)
                page.fill('input[name="allbin"]', str(bin_number), timeout=10000)
                page.click('input[name="go8"]', timeout=5000)
                time.sleep(2)
                log(f"Searched via allbin field, URL: {page.url}")
                return
            except Exception as e2:
                log(f"allbin also failed ({e2})")
                raise ValueError(f"Could not search by BIN: {e1} / {e2}")
    elif block and lot:
        boro_val = boro or "1"
        log(f"Searching by boro={boro_val} block={block} lot={lot}")
        page.select_option('select[name="allborough"]', boro_val)
        page.fill('input[name="allblock"]', str(block))
        page.fill('input[name="alllot"]', str(lot))
        page.click('input[name="go5"]', timeout=3000)
        time.sleep(2)
        log(f"Searched via block/lot, URL: {page.url}")
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
        # Step 1: Navigate to BIS search and get to property profile
        navigate_bis_search(page, bin_number, boro, block, lot)
        log(f"Jobs: On property profile, URL: {page.url}")

        # Step 2: Click the first "Jobs/Filings" link from the property profile
        # There are multiple links — we want the general one, not filtered
        try:
            # Look for any jobs link
            links = page.query_selector_all('a[href*="JobsQueryByLocationServlet"]')
            if links:
                # Click the first one that isn't filtered
                links[0].click()
                time.sleep(2)
                log(f"Jobs: Clicked jobs link, URL: {page.url}")
            else:
                raise Exception("No jobs link found on profile page")
        except Exception as click_err:
            log(f"Jobs: Click failed ({click_err}), trying direct URL")
            # Fallback with session cookies already set
            jobs_url = (f"https://a810-bisweb.nyc.gov/bisweb/JobsQueryByLocationServlet"
                        f"?requestid=0&allbin={bin_number}")
            page.goto(jobs_url, timeout=15000, wait_until="domcontentloaded")
            time.sleep(2)

        # Step 3: Change dropdown to "Show All Filings" to include PAAs
        try:
            selects = page.query_selector_all('select')
            for sel in selects:
                options = sel.query_selector_all('option')
                for opt in options:
                    text = (opt.text_content() or "").strip()
                    if "show" in text.lower() and ("filing" in text.lower() or "subsequent" in text.lower() or "paa" in text.lower()):
                        sel.select_option(label=text)
                        log(f"Jobs: Selected '{text}'")
                        break

            # Click APPLY
            apply_btn = page.query_selector('input[type="submit"][value="APPLY"]')
            if apply_btn:
                apply_btn.click()
                time.sleep(2)
                log("Jobs: Clicked APPLY to show all filings")
        except Exception as paa_err:
            log(f"Jobs: PAA dropdown failed: {paa_err} — continuing with default view")

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


def scrape_job_detail(page, job_number, debug=False):
    """Scrape BIS job detail page for all doc numbers.

    BIS doesn't have a direct job search from the main page.
    We use the JobsQueryByNumberServlet URL but navigate through
    the search form first to establish a session cookie.
    """
    # First visit search page to get session cookies
    search_url = "https://a810-bisweb.nyc.gov/bisweb/bsqpm01.jsp"
    log(f"Job detail: visiting search page for session...")
    page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
    time.sleep(1)

    # Now navigate to the job query URL with the session established
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
        return {"error": "Access denied by Akamai", "blocked": True}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    jobs = parse_bis_jobs_table(html)

    # Try to extract estimated cost and other details from the page
    estimated_cost = None
    m = re.search(r'(?:Estimated\s*(?:Job\s*)?Cost|Initial\s*Cost)[^$]*\$\s*([\d,]+)', html, re.IGNORECASE)
    if m:
        estimated_cost = m.group(1).replace(',', '')

    # Check for withdrawn in page text
    withdrawn = "WITHDRAWN" in html.upper()

    return {
        "job_number": job_number,
        "documents": jobs,
        "doc_count": len(jobs),
        "estimated_cost": estimated_cost,
        "withdrawn": withdrawn,
        "scraped_at": datetime.utcnow().isoformat(),
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
            # Extract all cell contents, stripping HTML tags
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

            # Filter out empty cells from spacer images
            cells = [c for c in cells if c and not c.startswith('\n')]

            # BIS columns: FILE DATE, JOB#, DOC#, JOB TYPE, JOB STATUS, STATUS DATE, LIC#, [LIC TYPE], APPLICANT, IN AUDIT, ZONING
            job_num = job_match.group(1)
            filing_date = date_match.group(1)

            # Find doc number — typically 2 digits after the job number
            doc_num = "01"
            for c in cells:
                if re.match(r'^\d{1,2}$', c) and c != job_num[:2]:
                    doc_num = c.zfill(2)
                    break

            # Find job type (A1, A2, A3, NB, DM, etc.)
            job_type = None
            for c in cells:
                if re.match(r'^[A-Z][A-Z0-9]?[0-9]?$', c) and len(c) <= 3:
                    job_type = c
                    break

            # Find job status — contains multi-word status like "X SIGNED OFF"
            job_status = None
            job_status_code = None
            for c in cells:
                if any(s in c.upper() for s in ['SIGNED OFF', 'APPROVED', 'IN PROCESS', 'PERMIT ISSUED', 'WITHDRAWN']):
                    job_status = c
                    parts = c.split()
                    if parts:
                        job_status_code = parts[0]
                    break

            # Find status date (second date in the row)
            dates = re.findall(r'(\d{2}/\d{2}/\d{4})', row)
            status_date = dates[1] if len(dates) > 1 else None

            # Find license number and type
            lic_match = re.search(r'(\d{7})\s*(PE|RA)', ' '.join(cells))
            license_number = lic_match.group(1) if lic_match else None
            license_type = lic_match.group(2) if lic_match else None

            # Find applicant — last significant text cell
            applicant = None
            for c in reversed(cells):
                if (c and len(c) > 2 and not re.match(r'^\d', c) and
                    c.upper() not in ['NOT APPLICABLE', 'GRANTED', 'Y', 'N', '']):
                    applicant = c
                    break

            # Zoning approval
            zoning = None
            for c in cells:
                if 'GRANTED' in c.upper() or 'NOT APPLICABLE' in c.upper():
                    zoning = c
                    break

            current_job = {
                "filing_date": filing_date,
                "job_number": job_num,
                "doc_number": doc_num,
                "job_type": job_type,
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
