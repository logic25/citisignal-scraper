"""BIS Web Scraper for CitiSignal.

Standalone Playwright service that fetches BIS property profiles,
job filings, DOF PTAPS property tax data, and DEP CIS water/sewer
account data. CitiSignal and BinCheck call this shared API.

Separate from dob-agent (Ordino) — each product has its own scraper.

Actions:
  profile    — BIS Property Profile (vacate orders, restrictions, counts)
  jobs       — BIS Jobs/Filings by location (all docs including PAAs)
  job_detail — Single job detail (all doc numbers for one job)
  dof_ptaps  — DOF Property Tax And Public Service bill lookup (input: bbl)
  dep_cis    — DEP Customer Information System water/sewer account (input: bbl or address)
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

# Optional egress proxy for when BIS's WAF blocks the datacenter IP.
# Strategy is direct-first: the proxy is only used to retry a request that
# came back blocked, so proxy bandwidth is spent only when actually needed.
#   PROXY_SERVER   e.g. "http://proxy-host:8000" (residential/rotating)
#   PROXY_USERNAME / PROXY_PASSWORD  optional credentials
PROXY_SERVER = os.environ.get("PROXY_SERVER", "")
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "")
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "")


def proxy_config():
    """Playwright proxy dict, or None when no proxy is configured."""
    if not PROXY_SERVER:
        return None
    cfg = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg


def log(msg):
    print(f"[BIS-SCRAPER] {datetime.utcnow().isoformat()} {msg}", flush=True)
    sys.stdout.flush()


def verify_auth(req):
    secret = req.headers.get("X-Scraper-Secret", "")
    if not secret or not SCRAPER_SECRET:
        return False
    return secret == SCRAPER_SECRET


# Signatures of WAF/bot-protection pages that BIS serves with HTTP 200.
# Without these checks a block looks identical to "building has no jobs".
BLOCK_SIGNATURES = [
    "Access Denied",             # Akamai hard denial
    "errors.edgesuite.net",      # Akamai error page
    "Pardon Our Interruption",   # Imperva/Distil challenge
    "Request unsuccessful",      # Incapsula
    "/_Incapsula_Resource",      # Incapsula challenge script
    "captcha",                   # generic challenge
]


def detect_block(html, page_url, expected_markers):
    """Return a reason string if the fetched page is a block/challenge page or
    not the BIS page we expected; None when the page is genuine.

    expected_markers: list of strings, ANY of which proves we reached the
    right BIS page (headers survive even when a building has zero records).
    """
    lowered = html.lower()
    for sig in BLOCK_SIGNATURES:
        if sig.lower() in lowered:
            return f"blocked ({sig})"
    if expected_markers and not any(m.lower() in lowered for m in expected_markers):
        # 200 OK but wrong page: bounced to homepage/search — a soft block
        # or lost session. Must NOT be treated as an empty result.
        return f"unexpected page (none of {expected_markers} found, url={page_url})"
    return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "citisignal-bis-scraper",
        "secret_configured": bool(SCRAPER_SECRET),
        "proxy_configured": bool(PROXY_SERVER),
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

    bbl = body.get("bbl")
    address = body.get("address")

    if action == "profile" and not bin_number:
        return jsonify({"error": "bin is required for profile action"}), 400
    if action == "jobs" and not bin_number:
        return jsonify({"error": "bin is required for jobs action"}), 400
    if action == "job_detail" and not job_number:
        return jsonify({"error": "job_number is required for job_detail action"}), 400
    if action == "dof_ptaps" and not bbl:
        return jsonify({"error": "bbl is required for dof_ptaps action"}), 400
    if action == "dep_cis" and not bbl and not address:
        return jsonify({"error": "bbl or address is required for dep_cis action"}), 400

    try:
        from playwright.sync_api import sync_playwright

        log(f"Scrape: action={action}, bin={bin_number}, job={job_number}")

        def run_attempt(pw, proxy=None):
            """Launch a browser (optionally through the proxy) and run the action."""
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                ],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = pw.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page(
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                if action == "profile":
                    return scrape_profile(page, bin_number, boro, block, lot, debug)
                elif action == "jobs":
                    return scrape_jobs_by_location(page, bin_number, debug, boro, block, lot)
                elif action in ("job_detail", "job"):
                    return scrape_job_detail(page, job_number, bin_number, debug)
                elif action == "dof_ptaps":
                    return fetch_dof_ptaps(page, bbl, debug)
                elif action == "dep_cis":
                    return fetch_dep_cis(page, bbl, address, debug)
                return {"error": f"Unknown action: {action}"}
            finally:
                browser.close()

        pw = sync_playwright().start()
        try:
            # Attempt 1: direct connection (free, usually works).
            result = run_attempt(pw)

            # Attempt 2: if blocked and a proxy is configured, retry through it.
            if result.get("blocked") and proxy_config():
                log("Blocked on direct connection — retrying via proxy...")
                result = run_attempt(pw, proxy=proxy_config())
                result["via_proxy"] = True
                if result.get("blocked"):
                    log("Still blocked via proxy.")
        finally:
            pw.stop()

        # Blocked/unexpected pages return 503 so callers' non-2xx failure
        # paths engage (CitiSignal increments scrape_fail_count) instead of
        # recording an empty scrape as a success.
        status = 503 if result.get("blocked") else 200
        return jsonify(result), status

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

    block_reason = detect_block(html, page.url, ["Property Profile"])
    if block_reason:
        log(f"Profile BLOCKED: {block_reason}")
        return {"error": block_reason, "blocked": True, "page_url": page.url}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

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

    # Jobs page markers survive even for buildings with zero filings, so a
    # marker miss means we never reached the jobs page — not an empty result.
    block_reason = detect_block(
        html, page.url,
        ["FILE DATE", "Jobs/Filings", "NO JOBS", "NO RECORDS"],
    )
    if block_reason:
        log(f"Jobs BLOCKED: {block_reason}")
        return {"error": block_reason, "blocked": True, "page_url": page.url, "jobs": []}

    if debug:
        return {"html": html[:50000], "html_length": len(html)}

    jobs = parse_bis_jobs_table(html)
    return {
        "bin": bin_number,
        "jobs": jobs,
        "job_count": len(jobs),
        "page_verified": True,  # zero jobs is now a trustworthy zero
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

    block_reason = detect_block(
        html, page.url,
        ["FILE DATE", "Jobs/Filings", "NO JOBS", "NO RECORDS"],
    )
    if block_reason:
        return {
            "error": f"{block_reason} — provide bin parameter for reliable results",
            "blocked": True, "page_url": page.url, "documents": [],
        }

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


# ============================================================================
# DOF PTAPS — Property Tax And Public Service bill lookup
# ============================================================================

def parse_bbl(bbl: str):
    """Split a 10-digit BBL into (boro, block_padded, lot_padded) strings.

    PTAPS expects:
      - boro: single digit (1-5)
      - block: 1-5 digits (no leading zeros required by the form)
      - lot:   1-4 digits
    """
    bbl = re.sub(r'[^0-9]', '', bbl)
    if len(bbl) < 10:
        raise ValueError(f"BBL must be 10 digits, got {len(bbl)}: {bbl!r}")
    boro  = bbl[0]
    block = bbl[1:6].lstrip('0') or '0'
    lot   = bbl[6:10].lstrip('0') or '0'
    return boro, block, lot


def fetch_dof_ptaps(page, bbl: str, debug: bool = False) -> dict:
    """Fetch DOF PTAPS property tax bill data for a BBL.

    DOF's Property Tax And Public Service portal (a836-pts-access.nyc.gov)
    is a classic ASP.NET WebForms app. We navigate the common-property-search
    form, submit by BBL, then read the resulting account summary page.

    Returned shape mirrors the existing Socrata fetchDOFCharges output so
    generate-dd-report/index.ts can swap in either source without changes to
    downstream rendering:

      {
        bbl, source, fetched_at,
        account: { account_number, owner_name, mailing_address },
        current_charges: { annual_tax, quarterly_amount, due_date, period },
        arrears: { balance_due, interest, total_due },
        exemptions: [ { program, amount } ],
        totals: { outstanding, interest, count },
        by_type: { TAX: { label, count, balance, oldest_due } },
        items: [ { code, code_label, balance, interest, due_date, ... } ],
        raw_sections: { ... }   # debug only
      }
    """
    try:
        boro, block, lot = parse_bbl(bbl)
    except ValueError as e:
        return {"error": str(e), "bbl": bbl}

    log(f"DOF PTAPS: BBL={bbl} → boro={boro} block={block} lot={lot}")

    # ── Step 1: land on the portal home and get a session cookie ──────────────
    portal_home = "https://a836-pts-access.nyc.gov/care/forms/htmlframe.aspx?mode=content/home.htm"
    log("DOF PTAPS: loading portal home for session...")
    try:
        page.goto(portal_home, timeout=20000, wait_until="domcontentloaded")
        time.sleep(1)
    except Exception as e:
        log(f"DOF PTAPS: portal home failed: {e}")
        return {"error": f"Portal home unreachable: {e}", "bbl": bbl}

    # ── Step 2: navigate to the BBL search form ───────────────────────────────
    search_url = "https://a836-pts-access.nyc.gov/care/search/commonsearch.aspx?mode=persprop"
    log(f"DOF PTAPS: navigating to search form...")
    try:
        page.goto(search_url, timeout=20000, wait_until="domcontentloaded")
        time.sleep(1)
    except Exception as e:
        log(f"DOF PTAPS: search form load failed: {e}")
        return {"error": f"Search form unreachable: {e}", "bbl": bbl}

    html = page.content()
    if debug:
        return {"html": html[:50000], "html_length": len(html), "step": "search_form"}

    # ── Step 3: fill and submit BBL fields ────────────────────────────────────
    # The form has three fields: borough (select), block (text), lot (text).
    # Field names vary by portal version — try both common naming conventions.
    try:
        # Try to select the Borough dropdown
        for sel in ['select[name*="Borough"]', 'select[name*="boro"]',
                    '#inpParid_boro', 'select#parcel_boro']:
            try:
                page.select_option(sel, boro, timeout=3000)
                log(f"DOF PTAPS: selected boro={boro} via {sel}")
                break
            except Exception:
                continue

        for sel in ['input[name*="Block"]', 'input[name*="block"]',
                    '#inpParid_block', 'input#parcel_block']:
            try:
                page.fill(sel, block, timeout=3000)
                log(f"DOF PTAPS: filled block={block} via {sel}")
                break
            except Exception:
                continue

        for sel in ['input[name*="Lot"]', 'input[name*="lot"]',
                    '#inpParid_lot', 'input#parcel_lot']:
            try:
                page.fill(sel, lot, timeout=3000)
                log(f"DOF PTAPS: filled lot={lot} via {sel}")
                break
            except Exception:
                continue

        # Submit
        for sel in ['input[type="submit"]', 'button[type="submit"]',
                    'input[value*="Search"]', 'input[name*="Submit"]']:
            try:
                page.click(sel, timeout=5000)
                log(f"DOF PTAPS: submitted via {sel}")
                time.sleep(3)
                break
            except Exception:
                continue

    except Exception as e:
        log(f"DOF PTAPS: form interaction error: {e}")
        return {"error": f"Form interaction failed: {e}", "bbl": bbl}

    # ── Step 4: parse the account summary ────────────────────────────────────
    html = page.content()
    current_url = page.url
    log(f"DOF PTAPS: post-submit URL={current_url}, html_len={len(html)}")

    if debug:
        return {"html": html[:60000], "html_length": len(html), "url": current_url}

    # Check for "no records found" or error pages
    if re.search(r'no records found|no match|not found|error occurred', html, re.IGNORECASE):
        return {
            "bbl": bbl, "source": "ptaps_live",
            "fetched_at": datetime.utcnow().isoformat(),
            "error": "No records found for this BBL in PTAPS",
            "totals": {"outstanding": 0, "interest": 0, "count": 0},
            "by_type": {}, "items": [],
        }

    return _parse_ptaps_html(html, bbl, current_url)


def _parse_ptaps_html(html: str, bbl: str, page_url: str = "") -> dict:
    """Parse the DOF PTAPS account summary HTML into a structured dict.

    The PTAPS portal renders a property account page with sections for:
      - Owner / mailing address
      - Current quarterly charges and annual tax
      - Outstanding balance / arrears
      - Tax exemptions

    We parse with regex since the page is served from a legacy ASP.NET app
    (no React, no JSON API) and the DOM structure is stable across BBLs.
    """
    def strip_tags(s: str) -> str:
        return re.sub(r'<[^>]+>', '', s).replace('&nbsp;', ' ').strip()

    def parse_dollar(s: str) -> float:
        if not s:
            return 0.0
        cleaned = re.sub(r'[^0-9.\-]', '', s.replace(',', ''))
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    # Owner / account info
    owner_name = None
    m = re.search(r'(?:Owner|Property Owner)[:\s]+</[^>]+>\s*<[^>]+>([^<]+)', html, re.IGNORECASE)
    if m:
        owner_name = strip_tags(m.group(1))

    account_number = None
    m = re.search(r'Account\s*(?:Number|#|No\.?)[:\s]*([\w\-]+)', html, re.IGNORECASE)
    if m:
        account_number = m.group(1).strip()

    # Annual tax / current quarterly bill
    annual_tax = 0.0
    m = re.search(r'Annual\s+(?:Property\s+)?Tax[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        annual_tax = parse_dollar(m.group(1))

    quarterly_amount = 0.0
    m = re.search(r'Quarterly\s+(?:Tax\s+)?(?:Amount|Bill)[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        quarterly_amount = parse_dollar(m.group(1))

    due_date = None
    m = re.search(r'Due\s+Date[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})', html, re.IGNORECASE)
    if m:
        due_date = m.group(1).strip()

    period = None
    m = re.search(r'(?:Tax\s+)?Period[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}[^<]{0,60})', html, re.IGNORECASE)
    if m:
        period = strip_tags(m.group(1))

    # Arrears / balance due
    balance_due = 0.0
    m = re.search(r'(?:Balance|Total)\s+Due[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        balance_due = parse_dollar(m.group(1))

    interest = 0.0
    m = re.search(r'Interest[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        interest = parse_dollar(m.group(1))

    # Exemptions — look for table rows with exemption program names
    exemptions = []
    for em in re.finditer(
        r'<tr[^>]*>.*?(?:STAR|SCHE|SCRIE|DRIE|ENHANCED|BASIC|Veterans|Clergy|Disability)[^<]*</.*?\$?([\d,\.]+)',
        html, re.IGNORECASE | re.DOTALL
    ):
        prog_text = strip_tags(em.group(0))
        amount = parse_dollar(em.group(1))
        if amount > 0:
            exemptions.append({"program": prog_text[:120], "amount": amount})

    # Normalize to the same shape as fetchDOFCharges Socrata output
    outstanding_total = balance_due
    items = []
    by_type: dict = {}

    # Annual tax line item (TAX code)
    if annual_tax > 0:
        items.append({
            "code": "TAX",
            "code_label": "Property Tax",
            "account_id": account_number,
            "balance": annual_tax,
            "interest": interest,
            "liability": annual_tax,
            "collected": 0.0,
            "due_date": due_date,
            "tax_year": None,
            "project_no": None,
            "cycle": None,
        })
        by_type["TAX"] = {
            "label": "Property Tax",
            "count": 1,
            "balance": annual_tax,
            "oldest_due": due_date,
        }

    # Arrears line item if separate from annual tax
    if balance_due > 0 and balance_due != annual_tax:
        items.append({
            "code": "ARR",
            "code_label": "Tax Arrears",
            "account_id": account_number,
            "balance": balance_due,
            "interest": interest,
            "liability": balance_due + interest,
            "collected": 0.0,
            "due_date": due_date,
            "tax_year": None,
            "project_no": None,
            "cycle": None,
        })
        by_type["ARR"] = {
            "label": "Tax Arrears",
            "count": 1,
            "balance": balance_due,
            "oldest_due": due_date,
        }
        outstanding_total = max(outstanding_total, balance_due)

    log(f"DOF PTAPS: annual_tax={annual_tax}, balance_due={balance_due}, interest={interest}, exemptions={len(exemptions)}")

    return {
        "bbl": bbl,
        "source": "ptaps_live",
        "fetched_at": datetime.utcnow().isoformat(),
        "page_url": page_url,
        "account": {
            "account_number": account_number,
            "owner_name": owner_name,
            "mailing_address": None,  # available on detail page, not summary
        },
        "current_charges": {
            "annual_tax": annual_tax,
            "quarterly_amount": quarterly_amount,
            "due_date": due_date,
            "period": period,
        },
        "arrears": {
            "balance_due": balance_due,
            "interest": interest,
            "total_due": round(balance_due + interest, 2),
        },
        "exemptions": exemptions,
        # ── Socrata-compatible fields (used by fetchDOFCharges shape in generate-dd-report) ──
        "totals": {
            "outstanding": round(outstanding_total, 2),
            "interest": round(interest, 2),
            "count": len(items),
        },
        "by_type": by_type,
        "items": items,
    }


# ============================================================================
# DEP CIS — Customer Information System water/sewer account lookup
# ============================================================================

def fetch_dep_cis(page, bbl: str = None, address: str = None, debug: bool = False) -> dict:
    """Fetch DEP CIS water/sewer account data for a property.

    DEP's NYCePay portal (https://a836-nycepay.nyc.gov/nycepay/) allows
    property owners and agents to look up water/sewer account balances by
    BBL or account number. The portal uses a React SPA over a REST-ish
    backend, so we navigate to the search form, submit by BBL, and read
    the account summary.

    Returned shape mirrors the WAT/SEW entries in fetchDOFCharges so that
    generate-dd-report/index.ts can substitute either source seamlessly:

      {
        bbl, address, source, fetched_at,
        account: { account_number, service_address, owner_name,
                   meter_number, meter_size, last_reading_date,
                   next_reading_date, next_bill_date },
        current_bill: { amount_due, due_date, billing_period },
        arrears: { past_due_balance, interest, total_due },
        consumption: { last_read, previous_read, consumption_hcf, read_type },
        totals: { outstanding, interest, count },
        by_type: { WAT: {...}, SEW: {...} },
        items: [ { code, code_label, balance, interest, due_date, ... } ]
      }
    """
    log(f"DEP CIS: bbl={bbl}, address={address}")

    # ── Step 1: load NYCePay portal ───────────────────────────────────────────
    portal_url = "https://a836-nycepay.nyc.gov/nycepay/"
    log("DEP CIS: loading NYCePay portal...")
    try:
        page.goto(portal_url, timeout=20000, wait_until="networkidle")
        time.sleep(2)
    except Exception as e:
        log(f"DEP CIS: portal load failed ({e}), trying alternate...")
        try:
            page.goto(portal_url, timeout=20000, wait_until="domcontentloaded")
            time.sleep(2)
        except Exception as e2:
            return {"error": f"NYCePay portal unreachable: {e2}", "bbl": bbl, "address": address}

    html = page.content()
    if debug:
        return {"html": html[:50000], "html_length": len(html), "url": page.url, "step": "portal"}

    # ── Step 2: find and use the BBL/account search ────────────────────────────
    # NYCePay has a search-by-BBL form. The field structure varies slightly
    # by portal version; we try the known selectors in order of specificity.

    bbl_submitted = False
    if bbl:
        try:
            boro, block, lot = parse_bbl(bbl)

            # Try selecting borough + block + lot (form-based entry)
            for boro_sel in ['select[name*="boro"]', 'select[name*="Borough"]',
                              '#borough', 'select.borough-select']:
                try:
                    page.select_option(boro_sel, boro, timeout=3000)
                    log(f"DEP CIS: selected boro={boro}")
                    break
                except Exception:
                    continue

            for block_sel in ['input[name*="block"]', 'input[name*="Block"]',
                               '#block', 'input.block-input']:
                try:
                    page.fill(block_sel, block, timeout=3000)
                    log(f"DEP CIS: filled block={block}")
                    break
                except Exception:
                    continue

            for lot_sel in ['input[name*="lot"]', 'input[name*="Lot"]',
                             '#lot', 'input.lot-input']:
                try:
                    page.fill(lot_sel, lot, timeout=3000)
                    log(f"DEP CIS: filled lot={lot}")
                    break
                except Exception:
                    continue

            # Submit the search
            for submit_sel in ['button[type="submit"]', 'input[type="submit"]',
                                'button:has-text("Search")', 'button:has-text("Find")',
                                'input[value*="Search"]']:
                try:
                    page.click(submit_sel, timeout=5000)
                    log(f"DEP CIS: submitted via {submit_sel}")
                    time.sleep(3)
                    bbl_submitted = True
                    break
                except Exception:
                    continue

        except Exception as e:
            log(f"DEP CIS: BBL form error: {e}")

    # Fallback: try address search if BBL form didn't work
    if not bbl_submitted and address:
        log(f"DEP CIS: trying address search for {address!r}")
        try:
            for addr_sel in ['input[name*="address"]', 'input[name*="Address"]',
                              '#address', 'input[placeholder*="address"]',
                              'input[placeholder*="Address"]']:
                try:
                    page.fill(addr_sel, address, timeout=3000)
                    page.press(addr_sel, 'Enter')
                    time.sleep(3)
                    bbl_submitted = True
                    log(f"DEP CIS: address submitted via {addr_sel}")
                    break
                except Exception:
                    continue
        except Exception as e:
            log(f"DEP CIS: address search error: {e}")

    # ── Step 3: parse the account page ────────────────────────────────────────
    html = page.content()
    current_url = page.url
    log(f"DEP CIS: post-submit URL={current_url}, html_len={len(html)}")

    if debug:
        return {"html": html[:60000], "html_length": len(html), "url": current_url}

    if not bbl_submitted or re.search(
        r'no account|not found|no results|error occurred', html, re.IGNORECASE
    ):
        # Return a structured empty shape so the caller can distinguish
        # "lookup succeeded, no balance" from a hard error
        return {
            "bbl": bbl, "address": address,
            "source": "cis_live",
            "fetched_at": datetime.utcnow().isoformat(),
            "error": "DEP CIS account not found or portal form could not be submitted",
            "totals": {"outstanding": 0, "interest": 0, "count": 0},
            "by_type": {}, "items": [],
        }

    return _parse_dep_cis_html(html, bbl, address, current_url)


def _parse_dep_cis_html(html: str, bbl: str = None, address: str = None, page_url: str = "") -> dict:
    """Parse the DEP NYCePay account summary HTML into a structured dict.

    The account page surfaces:
      - Account / meter info
      - Current bill amount + due date
      - Past-due balance
      - Last / next meter read dates
      - Consumption in HCF

    Output is normalized to match the WAT/SEW shape in fetchDOFCharges so
    generate-dd-report's existing charge-rendering logic accepts it without
    modification.
    """
    def strip_tags(s: str) -> str:
        return re.sub(r'<[^>]+>', '', s).replace('&nbsp;', ' ').strip()

    def parse_dollar(s: str) -> float:
        if not s:
            return 0.0
        cleaned = re.sub(r'[^0-9.\-]', '', s.replace(',', ''))
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    # Account metadata
    account_number = None
    m = re.search(r'Account\s*(?:Number|#|No\.?)[:\s]*([\w\-]+)', html, re.IGNORECASE)
    if m:
        account_number = m.group(1).strip()

    owner_name = None
    m = re.search(r'(?:Customer|Owner|Name)[:\s]*</[^>]+>\s*<[^>]+>([^<]+)', html, re.IGNORECASE)
    if m:
        owner_name = strip_tags(m.group(1))

    service_address = None
    m = re.search(r'(?:Service|Property)\s+Address[:\s]*</[^>]+>\s*<[^>]+>([^<]{5,100})', html, re.IGNORECASE)
    if m:
        service_address = strip_tags(m.group(1))

    meter_number = None
    m = re.search(r'Meter\s*(?:Number|#|No\.?)[:\s]*([\w\-]+)', html, re.IGNORECASE)
    if m:
        meter_number = m.group(1).strip()

    meter_size = None
    m = re.search(r'Meter\s+Size[:\s]*([\d\.]+"?)', html, re.IGNORECASE)
    if m:
        meter_size = m.group(1).strip()

    # Bill / balance data
    amount_due = 0.0
    m = re.search(r'(?:Amount|Total)\s+Due[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        amount_due = parse_dollar(m.group(1))

    due_date = None
    m = re.search(r'(?:Payment\s+)?Due\s+Date[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})', html, re.IGNORECASE)
    if m:
        due_date = m.group(1).strip()

    billing_period = None
    m = re.search(r'(?:Billing|Bill)\s+Period[:\s]*([A-Za-z0-9\s,/\-]{6,50})', html, re.IGNORECASE)
    if m:
        billing_period = strip_tags(m.group(1)).strip()

    past_due = 0.0
    m = re.search(r'Past\s+Due[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        past_due = parse_dollar(m.group(1))

    interest = 0.0
    m = re.search(r'Interest[:\s]*\$?([\d,\.]+)', html, re.IGNORECASE)
    if m:
        interest = parse_dollar(m.group(1))

    # Meter read info
    last_reading_date = None
    m = re.search(r'Last\s+Read(?:ing)?\s*Date?[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})', html, re.IGNORECASE)
    if m:
        last_reading_date = m.group(1).strip()

    next_reading_date = None
    m = re.search(r'Next\s+Read(?:ing)?\s*Date?[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})', html, re.IGNORECASE)
    if m:
        next_reading_date = m.group(1).strip()

    next_bill_date = None
    m = re.search(r'Next\s+Bill(?:ing)?\s*Date?[:\s]*([A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})', html, re.IGNORECASE)
    if m:
        next_bill_date = m.group(1).strip()

    last_read_val = None
    m = re.search(r'(?:Last\s+)?(?:Present|Current)\s+Read(?:ing)?[:\s]*([\d]+)', html, re.IGNORECASE)
    if m:
        last_read_val = m.group(1).strip()

    prev_read_val = None
    m = re.search(r'Previous\s+Read(?:ing)?[:\s]*([\d]+)', html, re.IGNORECASE)
    if m:
        prev_read_val = m.group(1).strip()

    consumption_hcf = None
    m = re.search(r'(?:Consumption|Usage)[:\s]*([\d\.]+)\s*(?:HCF|Ccf|ccf)', html, re.IGNORECASE)
    if m:
        consumption_hcf = m.group(1).strip()

    read_type = None
    m = re.search(r'Read\s+Type[:\s]*([A-Za-z\s]+?)(?:<|\n|$)', html, re.IGNORECASE)
    if m:
        read_type = strip_tags(m.group(1)).strip()

    # Normalize to Socrata-compatible shape (WAT + SEW split or combined)
    items = []
    by_type: dict = {}
    total_outstanding = max(amount_due, past_due)

    if amount_due > 0:
        items.append({
            "code": "WAT",
            "code_label": "Water/Sewer Charge (DEP)",
            "account_id": account_number,
            "balance": amount_due,
            "interest": interest,
            "liability": amount_due,
            "collected": 0.0,
            "due_date": due_date,
            "tax_year": None,
            "project_no": None,
            "cycle": billing_period,
        })
        by_type["WAT"] = {
            "label": "Water/Sewer Charge (DEP)",
            "count": 1,
            "balance": amount_due,
            "oldest_due": due_date,
        }

    if past_due > 0 and past_due != amount_due:
        items.append({
            "code": "WAT_ARR",
            "code_label": "Water/Sewer Arrears (DEP)",
            "account_id": account_number,
            "balance": past_due,
            "interest": interest,
            "liability": past_due + interest,
            "collected": 0.0,
            "due_date": due_date,
            "tax_year": None,
            "project_no": None,
            "cycle": billing_period,
        })
        by_type["WAT_ARR"] = {
            "label": "Water/Sewer Arrears (DEP)",
            "count": 1,
            "balance": past_due,
            "oldest_due": due_date,
        }

    log(f"DEP CIS: amount_due={amount_due}, past_due={past_due}, interest={interest}")

    return {
        "bbl": bbl,
        "address": address,
        "source": "cis_live",
        "fetched_at": datetime.utcnow().isoformat(),
        "page_url": page_url,
        "account": {
            "account_number": account_number,
            "service_address": service_address,
            "owner_name": owner_name,
            "meter_number": meter_number,
            "meter_size": meter_size,
            "last_reading_date": last_reading_date,
            "next_reading_date": next_reading_date,
            "next_bill_date": next_bill_date,
        },
        "current_bill": {
            "amount_due": amount_due,
            "due_date": due_date,
            "billing_period": billing_period,
        },
        "arrears": {
            "past_due_balance": past_due,
            "interest": interest,
            "total_due": round(past_due + interest, 2),
        },
        "consumption": {
            "last_read": last_read_val,
            "previous_read": prev_read_val,
            "consumption_hcf": consumption_hcf,
            "read_type": read_type,
        },
        # ── Socrata-compatible fields (WAT/SEW shape from fetchDOFCharges) ──
        "totals": {
            "outstanding": round(total_outstanding, 2),
            "interest": round(interest, 2),
            "count": len(items),
        },
        "by_type": by_type,
        "items": items,
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
