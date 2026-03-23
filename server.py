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
            result = scrape_jobs_by_location(page, bin_number, debug)
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
        page.fill('input[name="allbin"]', str(bin_number))
    elif block and lot:
        boro_val = boro or "1"
        log(f"Searching by boro={boro_val} block={block} lot={lot}")
        page.select_option('select[name="allborough"]', boro_val)
        page.fill('input[name="allblock"]', str(block))
        page.fill('input[name="alllot"]', str(lot))
    else:
        raise ValueError("Need bin or block+lot to search BIS")

    page.click('input[type="submit"][value="GO"]')
    time.sleep(2)
    log(f"After search, URL: {page.url}")


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


def scrape_jobs_by_location(page, bin_number, debug=False):
    """Scrape BIS Jobs/Filings page for a BIN."""
    try:
        navigate_bis_search(page, bin_number)
        # Click "Jobs/Filings" link from the property profile
        page.click('a[href*="JobsQueryByLocationServlet"]', timeout=5000)
        time.sleep(2)
    except Exception as e:
        log(f"Jobs navigation error: {e}")
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
    """Scrape BIS job detail page for all doc numbers."""
    # Navigate to BIS first, then use job search
    search_url = "https://a810-bisweb.nyc.gov/bisweb/bsqpm01.jsp"
    page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
    time.sleep(1)

    # Fill job number field
    try:
        page.fill('input[name="allnumbhous"]', "")  # clear other fields
        # BIS search page may have a job number search option
        # Navigate to job search page instead
        page.goto("https://a810-bisweb.nyc.gov/bisweb/bsqpm02.jsp",
                   timeout=15000, wait_until="domcontentloaded")
        time.sleep(1)
        page.fill('input[name="passjobnumber"]', str(job_number))
        page.click('input[type="submit"][value="GO"]')
        time.sleep(2)
    except Exception as e:
        log(f"Job search error: {e}")
        return {"error": str(e), "documents": []}

    html = page.content()

    if "Access Denied" in html:
        return {"error": "Access denied by Akamai", "blocked": True}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    jobs = parse_bis_jobs_table(html)
    return {
        "job_number": job_number,
        "documents": jobs,
        "doc_count": len(jobs),
        "scraped_at": datetime.utcnow().isoformat(),
    }


def parse_bis_jobs_table(html: str) -> list:
    """Parse BIS job/filing table HTML into structured data."""
    jobs = []
    rows = re.split(r'<tr[^>]*>', html, flags=re.IGNORECASE)

    current_job = None
    for row in rows:
        date_match = re.search(r'(\d{2}/\d{2}/\d{4})', row)
        job_match = re.search(r'passjobnumber=(\d{9})', row)

        if date_match and job_match:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

            if len(cells) >= 8:
                current_job = {
                    "filing_date": cells[0] if cells[0] else None,
                    "job_number": job_match.group(1),
                    "doc_number": cells[2] if len(cells) > 2 else "01",
                    "job_type": cells[3] if len(cells) > 3 else None,
                    "job_status": cells[4] if len(cells) > 4 else None,
                    "status_date": cells[5] if len(cells) > 5 else None,
                    "license_number": cells[6] if len(cells) > 6 else None,
                    "applicant": cells[7] if len(cells) > 7 else None,
                    "in_audit": cells[8].strip().upper() == "Y" if len(cells) > 8 else False,
                    "zoning_approval": cells[9] if len(cells) > 9 else None,
                    "description": None,
                    "withdrawn": False,
                    "source": "BIS_SCRAPE",
                }
                if current_job["job_status"]:
                    parts = current_job["job_status"].split()
                    if parts:
                        current_job["job_status_code"] = parts[0]
                jobs.append(current_job)

        elif current_job and not job_match:
            text = re.sub(r'<[^>]+>', '', row).strip()
            if text and len(text) > 10 and not text.startswith('FILE DATE'):
                current_job["description"] = text
                if "WITHDRAWN" in text.upper():
                    current_job["withdrawn"] = True
                current_job = None

    log(f"Parsed {len(jobs)} jobs from HTML")
    return jobs


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
