"""
Job alert bot.
Polls configured companies across Workday / SuccessFactors / custom career sites,
filters for relevant roles, and sends new matches to Telegram.

Run via GitHub Actions on a schedule. State (seen job IDs) is persisted to seen_jobs.json
and committed back to the repo by the workflow after each run.
"""

import json
import os
import re
import sys
import time
import requests

STATE_FILE = "seen_jobs.json"
COMPANIES_FILE = "companies.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Filter configuration
# ---------------------------------------------------------------------------

LOCATIONS_ALLOW = [
    "zurich", "zürich", "geneva", "genève", "basel", "zug", "baar",
    "pfäffikon", "lausanne", "switzerland", "london", "united kingdom"
]

SENIORITY_BLOCK_PATTERNS = [
    re.compile(r"\bvice president\b", re.IGNORECASE),
    re.compile(r"\bvp\b", re.IGNORECASE),
    re.compile(r"\bsvp\b", re.IGNORECASE),
    re.compile(r"\bdirector\b", re.IGNORECASE),
    re.compile(r"\bhead of\b", re.IGNORECASE),
    re.compile(r"\bmanaging director\b", re.IGNORECASE),
    re.compile(r"\bsenior\b", re.IGNORECASE),
    re.compile(r"\bprincipal\b", re.IGNORECASE),
    re.compile(r"\bchief\b", re.IGNORECASE),
    re.compile(r"\bexecutive director\b", re.IGNORECASE),
]

TITLE_BOOST_KEYWORDS = ["intern", "internship", "graduate", "trainee", "campus", "entry level", "entry-level"]

EXPERIENCE_YEARS_PATTERN = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?\s*years?\s*(?:of\s*)?experience", re.IGNORECASE)

# weighted keyword groups per category, used for relevance scoring
CATEGORY_KEYWORDS = {
    "FI": [
        "fixed income", "credit research", "credit analyst", "bond analyst",
        "portfolio analyst", "investment analyst", "corporate bonds",
        "government bonds", "investment grade", "high yield",
        "emerging market debt", "structured credit", "securitised",
        "securitized", "asset backed", "abs", "clo", "rates", "macro"
    ],
    "PC": [
        "private credit", "private debt", "direct lending", "leveraged finance",
        "capital solutions", "credit opportunities", "special situations",
        "asset based finance", "credit underwriting", "real estate debt",
        "infrastructure debt", "fund finance"
    ],
    "MA": [
        "multi asset", "multi-asset", "asset allocation", "portfolio construction",
        "investment solutions", "manager research", "ocio", "fiduciary management",
        "strategic asset allocation", "portfolio advisory"
    ],
    "SR": [
        "fixed income research", "credit strategy", "rates strategy",
        "macro strategy", "sovereign research", "structured credit research"
    ]
}

EXCLUDE_KEYWORDS = [
    "equity research", "equity analyst", "software engineer", "developer",
    "data scientist", "compliance officer", "legal counsel", "marketing manager",
    "accounting", "cybersecurity", "recruiter", "hr business partner"
]

RELEVANCE_SCORE_THRESHOLD = 1  # min keyword hits to count as a "maybe"
STRONG_MATCH_THRESHOLD = 3     # min keyword hits to count as a "yes"


# ---------------------------------------------------------------------------
# Platform fetchers
# ---------------------------------------------------------------------------

def fetch_workday_jobs(company):
    """Query a Workday tenant's public CXS search API."""
    url = f"https://{company['wd_host']}/wday/cxs/{company['wd_tenant']}/{company['wd_site']}/jobs"
    jobs = []
    offset = 0
    limit = 20
    max_pages = 10  # safety cap

    for _ in range(max_pages):
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        try:
            resp = requests.post(url, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [warn] Workday fetch failed for {company['name']}: {e}")
            break

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for p in postings:
            external_path = p.get("externalPath", "")
            jobs.append({
                "id": f"workday:{company['wd_tenant']}:{external_path}",
                "title": p.get("title", ""),
                "location": p.get("locationsText", ""),
                "url": f"https://{company['wd_host']}/{company['wd_site']}{external_path}",
                "description": ""  # full description requires a second call per job; title+location is enough to filter on for most cases
            })

        offset += limit
        if offset >= data.get("total", 0):
            break
        time.sleep(0.5)

    return jobs


def fetch_workday_job_description(company, external_path):
    """Fetch the full job detail (including description) for one Workday posting.
    Only called for postings that already passed location/seniority, to limit API calls.
    """
    url = f"https://{company['wd_host']}/wday/cxs/{company['wd_tenant']}/{company['wd_site']}{external_path}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("jobPostingInfo", {}).get("jobDescription", "")
    except Exception as e:
        print(f"    [warn] Could not fetch description for {external_path}: {e}")
        return ""


def fetch_successfactors_jobs(company):
    """Scrape a SuccessFactors career site's public job list.
    SuccessFactors career sites vary in structure; this targets the common
    OData-style public endpoint where available, falling back to None (flag for manual check).
    """
    # NOTE: SuccessFactors doesn't have one universal public JSON contract like Workday.
    # This is a placeholder that should be validated per-tenant once we confirm
    # whether career5.successfactors.eu exposes a job search JSON endpoint for Pictet.
    print(f"  [todo] SuccessFactors fetcher not yet implemented for {company['name']}")
    return []


def fetch_greenhouse_jobs(company):
    """Query Greenhouse's public job board JSON API."""
    token = company["gh_token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [warn] Greenhouse fetch failed for {company['name']}: {e}")
        return []

    jobs = []
    for job in data.get("jobs", []):
        jobs.append({
            "id": f"greenhouse:{token}:{job['id']}",
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "description": job.get("content", "")  # HTML, fine for keyword scanning as-is
        })
    return jobs


def _unwrap_ddg_url(href):
    """DuckDuckGo HTML results sometimes wrap the real URL in a redirect like
    //duckduckgo.com/l/?uddg=<encoded-real-url>. Unwrap to the real destination."""
    from urllib.parse import urlparse, parse_qs, unquote
    if not href:
        return href
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return href


def fetch_ddg_site_jobs(company):
    """Search-based fallback for career sites that block datacenter IPs (e.g. UBS BrassRing).
    Runs `site:<domain> <terms>` queries against DuckDuckGo's HTML endpoint (no API key needed)
    and returns the indexed job pages as pseudo-postings. Freshness depends on DDG's crawl
    cadence (typically 1-2 days), so this trades instant detection for actually working from a server.
    """
    from bs4 import BeautifulSoup

    domain = company["ddg_site"]                       # e.g. "jobs.ubs.com"
    queries = company.get("ddg_queries", [""])         # list of search term strings
    endpoint = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    }

    jobs = []
    for terms in queries:
        q = f"site:{domain} {terms}".strip()
        try:
            resp = requests.get(endpoint, params={"q": q}, headers=headers, timeout=(10, 20))
            resp.raise_for_status()
        except Exception as e:
            print(f"  [warn] DDG search failed for {company['name']} (q='{q}'): {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for result in soup.select("div.result"):
            link_el = result.select_one("a.result__a")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            url = _unwrap_ddg_url(link_el.get("href", ""))
            snippet_el = result.select_one("a.result__snippet") or result.select_one(".result__snippet")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""

            # only keep results actually on the target domain (site: can leak subdomains/dupes)
            if domain not in url:
                continue

            # stable id from the URL (strip query noise where possible)
            job_id = f"ddg:{domain}:{url.split('?')[0].split('#')[0]}"
            jobs.append({
                "id": job_id,
                "title": title,
                "location": "",  # DDG doesn't reliably expose location; rely on title/snippet + query terms
                "url": url,
                "description": snippet,
            })
        time.sleep(1.0)  # be polite to DDG between queries

    # dedupe by id
    seen = set()
    deduped = []
    for j in jobs:
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        deduped.append(j)
    return deduped


def fetch_custom_jobs(company):
    """Dispatch custom-site fetchers by company.
    UBS: BrassRing blocks datacenter IPs, so use the DuckDuckGo site-search fallback."""
    if company.get("custom_type") == "ddg_site":
        return fetch_ddg_site_jobs(company)
    if company.get("custom_type") == "brassring":
        return fetch_brassring_jobs(company)
    print(f"  [todo] Custom fetcher not yet implemented for {company['name']}")
    return []


def fetch_brassring_jobs(company):
    """Fetch jobs from a BrassRing / IBM Talent Gateway board (e.g. UBS jobs.ubs.com).

    Two-step flow confirmed against UBS's live MatchedJobs endpoint:
      1. GET the board home page with a Session, to receive tg_session + tg_rft cookies.
      2. POST to /TgNewUI/Search/Ajax/MatchedJobs, sending the RFT token (from tg_rft cookie)
         as an 'RFT' header and the tg_session cookie value as 'encryptedsessionvalue' in the body.
    Jobs come back as JSON; each job's fields live in a list of {QuestionName, Value} objects.
    """
    host = company["br_host"]              # e.g. "jobs.ubs.com"
    partner_id = company["br_partner_id"]  # e.g. "25008"
    site_id = company["br_site_id"]        # e.g. "5176"
    origin = f"https://{host}"
    board_url = f"{origin}/TGNewUI/Search/Home/Home?partnerid={partner_id}&siteid={site_id}"
    api_url = f"{origin}/TgNewUI/Search/Ajax/MatchedJobs"

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        # Step 1: establish session, collect cookies (tg_session, tg_rft)
        # Tight (connect, read) timeouts: if UBS stalls server-side traffic, abort fast.
        session.get(board_url, timeout=(10, 20)).raise_for_status()
        # Warm-up: hit the search results page too, so any additional cookies
        # (e.g. Akamai/F5 TS* bot-guard cookies) get set on the session before the AJAX call.
        warmup_url = (f"{origin}/TGnewUI/Search/home/HomeWithPreLoad"
                      f"?partnerid={partner_id}&siteid={site_id}&PageType=searchResults")
        try:
            session.get(warmup_url, timeout=(10, 20))
        except Exception:
            pass  # non-fatal; the token GET above is what matters
    except Exception as e:
        print(f"  [warn] BrassRing session init failed for {company['name']}: {e}")
        return []

    tg_session = session.cookies.get(f"tg_session_{partner_id}_{site_id}") or session.cookies.get("tg_session")
    tg_rft = session.cookies.get("tg_rft")

    if not tg_session or not tg_rft:
        print(f"  [warn] BrassRing tokens missing for {company['name']} "
              f"(tg_session={'ok' if tg_session else 'missing'}, tg_rft={'ok' if tg_rft else 'missing'}). "
              f"Board may use different cookie names.")
        return []

    all_jobs = []
    for loc in company.get("br_search_locations", [""]):
        payload = {
            "PartnerId": partner_id,
            "SiteId": site_id,
            "Keyword": "",
            "Location": loc,
            "KeywordCustomSolrFields": "FORMTEXT21,AutoReq,Department,JobTitle",
            "LocationCustomSolrFields": "FORMTEXT2,FORMTEXT23,Location",
            "TurnOffHttps": False,
            "Latitude": 0,
            "Longitude": 0,
            "PowerSearchOptions": {"PowerSearchOption": []},
            "encryptedsessionvalue": tg_session,
        }
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/json; charset=UTF-8",
            "Origin": origin,
            "Referer": board_url,
            "RFT": tg_rft,
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            resp = session.post(api_url, json=payload, headers=headers, timeout=(10, 20))
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [warn] BrassRing MatchedJobs failed for {company['name']} (loc={loc}): {e}")
            try:
                print(f"    [debug] response status={resp.status_code}, body[:500]={resp.text[:500]!r}")
            except Exception:
                pass
            continue

        raw_jobs = data.get("Jobs", {}).get("Job", [])
        for raw in raw_jobs:
            def q(name):
                target = name.casefold()
                for question in raw.get("Questions", []):
                    if str(question.get("QuestionName", "")).casefold() == target:
                        return str(question.get("Value", "")).strip()
                return ""

            job_id = q("reqid") or raw.get("JobId", "")
            link = raw.get("Link", "")
            if link and not link.startswith("http"):
                url = f"{origin}/{link.lstrip('/')}"
            elif link:
                url = link
            else:
                url = (f"{origin}/TGnewUI/Search/home/HomeWithPreLoad?PageType=JobDetails"
                       f"&jobid={job_id}&partnerid={partner_id}&siteid={site_id}")

            all_jobs.append({
                "id": f"brassring:{host}:{site_id}:{job_id}",
                "title": q("jobtitle"),
                "location": q("location") or ", ".join(v for v in (q("city"), q("country")) if v),
                "url": url,
                "description": q("description"),
            })
        time.sleep(0.4)

    # dedupe (same job can surface under multiple location searches)
    seen = set()
    deduped = []
    for j in all_jobs:
        if j["id"] in seen:
            continue
        seen.add(j["id"])
        deduped.append(j)
    return deduped


FETCHERS = {
    "workday": fetch_workday_jobs,
    "greenhouse": fetch_greenhouse_jobs,
    "successfactors": fetch_successfactors_jobs,
    "custom": fetch_custom_jobs,
}


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def location_ok(location_text):
    loc = (location_text or "").lower()
    return any(allowed in loc for allowed in LOCATIONS_ALLOW)


def seniority_ok(title):
    t = title or ""
    if any(pattern.search(t) for pattern in SENIORITY_BLOCK_PATTERNS):
        return False
    return True


def score_relevance(title, description, categories):
    text = f"{title} {description}".lower()

    for bad in EXCLUDE_KEYWORDS:
        if bad in text:
            return -1, []

    hits = []
    for cat in categories:
        for kw in CATEGORY_KEYWORDS.get(cat, []):
            if kw in text:
                hits.append(kw)

    title_lower = (title or "").lower()
    if any(boost_kw in title_lower for boost_kw in TITLE_BOOST_KEYWORDS):
        hits.append("(title boost: intern/graduate/trainee)")
        hits.append("(title boost: intern/graduate/trainee)")  # counts as +2

    return len(hits), hits


def detect_experience_flag(title, description):
    text = f"{title} {description}"
    matches = EXPERIENCE_YEARS_PATTERN.findall(text)
    years = [int(m) for m in matches if m.isdigit()]
    if years and min(years) >= 2:
        return f"⚠️ Posting mentions {min(years)}+ years experience — may not be entry-level"
    return None


def classify_job(job, company):
    if not location_ok(job["location"]):
        return None
    if not seniority_ok(job["title"]):
        return None

    extra_terms = company.get("extra_filter_terms")
    if extra_terms:
        text = f"{job['title']} {job.get('description','')}".lower()
        if not any(term.lower() in text for term in extra_terms):
            return None

    score, hits = score_relevance(job["title"], job.get("description", ""), company["category"])
    if score < 0:
        return None
    if score >= STRONG_MATCH_THRESHOLD:
        verdict = "yes"
    elif score >= RELEVANCE_SCORE_THRESHOLD:
        verdict = "maybe"
    else:
        return None

    year_mention = None
    text = f"{job['title']} {job.get('description','')}"
    m = re.search(r"20(2[5-9]|3[0-9])", text)
    if m:
        year_mention = m.group(0)

    experience_flag = detect_experience_flag(job["title"], job.get("description", ""))

    return {"verdict": verdict, "hits": hits, "year_mention": year_mention, "experience_flag": experience_flag}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [warn] Telegram credentials missing, skipping send. Message was:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=15)
    except Exception as e:
        print(f"  [warn] Telegram send failed: {e}")


def format_alert(company, job, classification):
    tag = "🟢 STRONG MATCH" if classification["verdict"] == "yes" else "🟡 possible match"
    priority = company.get("priority_flag")
    header = f"{priority} {tag}" if priority else tag
    year = f"\nStart year mentioned: {classification['year_mention']}" if classification["year_mention"] else "\nNo start year mentioned in posting"
    exp_flag = f"\n{classification['experience_flag']}" if classification.get("experience_flag") else ""
    return (
        f"{header}\n"
        f"<b>{company['name']}</b>\n"
        f"{job['title']}\n"
        f"📍 {job['location']}\n"
        f"{year}{exp_flag}\n"
        f"{job['url']}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    with open(COMPANIES_FILE, "r") as f:
        config = json.load(f)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    new_seen_ids = set(seen_ids)

    alerts_sent = 0
    funnel = {"new": 0, "passed_location": 0, "passed_seniority": 0, "passed_relevance": 0}

    for company in config["companies"]:
        if company.get("status") != "confirmed":
            continue

        platform = company["platform"]
        fetcher = FETCHERS.get(platform)
        if not fetcher:
            print(f"  [warn] No fetcher for platform '{platform}' ({company['name']})")
            continue

        print(f"Checking {company['name']} ({platform})...")
        jobs = fetcher(company)
        print(f"  -> {len(jobs)} postings found")

        for job in jobs:
            if job["id"] in seen_ids:
                continue

            new_seen_ids.add(job["id"])
            funnel["new"] += 1

            # DDG site-search jobs carry location in the query/title, not a structured field,
            # so skip the structured location filter for them (query already constrains location).
            is_ddg = company.get("custom_type") == "ddg_site"
            if not is_ddg and not location_ok(job["location"]):
                continue
            funnel["passed_location"] += 1

            if not seniority_ok(job["title"]):
                continue
            funnel["passed_seniority"] += 1

            # fetch full description only for the narrow set of candidates that made it this far,
            # and only when needed (brand-filtered shared tenants) or when the description is empty
            if platform == "workday" and company.get("extra_filter_terms") and not job.get("description"):
                external_path = job["id"].split(":", 2)[-1]
                job["description"] = fetch_workday_job_description(company, external_path)
                time.sleep(0.3)

            classification = classify_job(job, company)
            if classification is None:
                continue
            funnel["passed_relevance"] += 1

            message = format_alert(company, job, classification)
            send_telegram(message)
            alerts_sent += 1
            time.sleep(0.3)  # be gentle with Telegram's rate limits

    state["seen_ids"] = list(new_seen_ids)
    save_state(state)

    print(f"Funnel: {funnel['new']} new -> {funnel['passed_location']} in target locations -> "
          f"{funnel['passed_seniority']} right seniority -> {funnel['passed_relevance']} relevant -> {alerts_sent} sent")
    print(f"Done. {alerts_sent} alert(s) sent. {len(new_seen_ids)} total job IDs tracked.")


if __name__ == "__main__":
    main()
