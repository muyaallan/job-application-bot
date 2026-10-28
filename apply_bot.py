"""
Job bot v2: finds remote jobs across many sites, scores your fit, estimates your chances,
auto-fills applications, watches your inbox for interview emails, and pings your phone.

    python apply_bot.py find                 # build ranked_jobs.csv/json
    python apply_bot.py auto [--live]        # fill (and with --live, submit) clean forms
    python apply_bot.py apply --top 10       # visible browser, you press Submit
    python apply_bot.py inbox                # check email for interview invites -> notify
    python apply_bot.py summary              # evening digest -> notify

Schedule (cron):
    0 9  * * *  cd /path/to/bot && python apply_bot.py auto --live
    0 *  * * *  cd /path/to/bot && python apply_bot.py inbox
    0 20 * * *  cd /path/to/bot && python apply_bot.py summary
Secrets go in a .env file next to this script (see env.example.txt). Never commit it.
"""
import argparse, csv, datetime, email, html, imaplib, json, logging, os, random, re, sys, time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field
from email.header import decode_header, make_header

import requests
from bs4 import BeautifulSoup

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("job_bot.log"), logging.StreamHandler()])
log = logging.getLogger("jobbot")
UA = {"User-Agent": "Mozilla/5.0 (personal job search script)"}

# =================================================================== CONFIG ===
PROFILE = dict(
    first_name="Muya", last_name="Allan", email="wangechiallan130@gmail.com",
    phone="+254742583696", portfolio="https://makeent.netlify.app",
    github="https://github.com/muyaallan", linkedin="https://linkedin.com/in/scarsmask",
    location="Nairobi, Kenya", resume_path="Muya_Allan_Resume.pdf",
)

# Answers to common application questions. BLANK = the bot will NOT answer and will skip
# that job so you can do it. Fill these in yourself and honestly (the legal ones are yours).
ANSWERS = dict(
    work_authorization="No",
    sponsorship="Yes",
    salary="$35/hour",
    notice_period="Immediate",
    years_experience="7",
    how_heard="Job board",
    relocate="No",
    current_company="MAKE Ent Studio",
    current_title="Software Engineer & Founder",
)
YEARS_EXPERIENCE = 7           # used only for the chance estimate; set it honestly

SKILLS = {  # what you have (weight = how much it counts)
    "typescript": 3, "node.js": 3, "nodejs": 3, "react": 3, "next.js": 2, "fastify": 2,
    "electron": 3, "python": 2, "postgresql": 2, "postgres": 2, "sqlite": 1, "supabase": 1,
    "websocket": 2, "websockets": 2, "rest api": 2, "full-stack": 3, "full stack": 3,
    "fullstack": 3, "backend": 2, "back-end": 2, "llm": 3, "openai": 2, "gemini": 1, "ai": 2,
    "chrome extension": 2, "docker": 1, "tailwind": 1, "sql": 1, "javascript": 2, "fastapi": 1,
    "rust": 1, "c++": 1, "git": 1,
}
GAPS = {  # common requirements you don't list; they pull your fit % down
    "java": 3, "spring boot": 2, "kotlin": 2, "swift": 2, "ios": 2, "android": 2, "php": 2,
    "laravel": 2, "ruby": 2, "rails": 2, "angular": 2, "vue": 1, "golang": 3, "scala": 2,
    "elixir": 2, "c#": 3, ".net": 3, "aws": 1, "gcp": 1, "azure": 1, "kubernetes": 2,
    "terraform": 2, "kafka": 2, "pytorch": 2, "tensorflow": 2, "graphql": 1, "mongodb": 1,
    "redis": 1, "salesforce": 3, "sap": 3, "devops": 2, "machine learning engineer": 3,
}
BAD_TITLE_RE = re.compile(r"\b(staff|principal|director|manager|head of|vp|lead|intern|internship|new grad|early career|graduate|apprentice)\b")
TITLE_OK_RE = re.compile(r"engineer|developer|programmer|software|full.?stack|back.?end|front.?end|\bai\b|\bllm\b|architect|\bsde\b|\bswe\b")
MIN_FIT = 55            # find: keep jobs with fit % at least this
MIN_CHANCE = 6          # auto: only apply if estimated interview chance % is at least this

# Region filtering (you're in Kenya): block "Remote - US only" style listings.
GEO_BLOCK = ["us only", "u.s. only", "united states only", "must be located in the us", "us-based",
             "us based", "authorized to work in the u", "eligible to work in the u", "uk only",
             "eu only", "europe only", "canada only", "must reside in", "must be based in"]
BLOCK_LOC_RE = re.compile(r"\b(us|usa|u\.s\.a?|united states|canada|uk|united kingdom|europe|eu|germany|france|"
                          r"netherlands|spain|poland|portugal|ireland|australia|new zealand|india|brazil|latam|"
                          r"latin america|apac|americas|north america)\b")
WORLDWIDE = ["worldwide", "anywhere", "global", "africa", "emea", "kenya", "nairobi", "any location", "all locations"]

# Company slugs for public job-board APIs (last part of the careers URL). Add your own.
GREENHOUSE = ["vercel", "gitlab", "airbnb", "stripe", "datadog", "figma", "cloudflare", "mongodb", "elastic", "twilio"]
LEVER = ["netlify", "palantir", "spotify"]
ASHBY = ["linear", "ramp", "notion"]

INTERVIEW_WORDS = ["interview", "schedule a call", "schedule a chat", "schedule time", "your availability",
                   "calendly", "book a time", "phone screen", "technical screen", "coding challenge",
                   "take-home", "take home", "assessment", "hackerrank", "codility", "meet with our",
                   "speak with you", "intro call", "introductory call"]
REJECT_WORDS = ["unfortunately", "not moving forward", "other candidates", "regret to inform",
                "decided not to proceed", "will not be moving", "position has been filled"]
RECRUITING_HINTS = ["greenhouse", "lever.co", "ashby", "workable", "recruit", "talent", "hiring", "careers",
                    "calendly", "people team", "hr@", "jobs@"]

NOTIFY_EACH_APPLICATION = False   # False = just the evening summary; True = ping per application
HISTORY_FILE, SKIP_FILE, LOG_FILE, MAIL_FILE = ("applied_history.json", "skipped_history.json",
                                                  "activity_log.jsonl", "seen_emails.json")


# =================================================================== MODEL ===
@dataclass
class Job:
    title: str
    company: str
    url: str
    source: str
    location: str = ""
    description: str = ""
    fit: int = 0
    chance: int = 0
    years_req: int = 0
    region_clear: bool = False
    matched: list = field(default_factory=list)
    missing: list = field(default_factory=list)


def clean(raw):
    return BeautifulSoup(html.unescape(raw or ""), "html.parser").get_text(" ")


def get_json(url, **kw):
    return requests.get(url, headers=UA, timeout=25, **kw).json()


# ================================================================== SOURCES ===
def from_greenhouse():
    for slug in GREENHOUSE:
        try:
            for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").get("jobs", []):
                yield Job(j["title"], slug, j["absolute_url"], "greenhouse",
                          (j.get("location") or {}).get("name", ""), clean(j.get("content")))
        except Exception as e:
            log.warning("greenhouse %s: %s", slug, e)


def from_lever():
    for slug in LEVER:
        try:
            data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            if not isinstance(data, list):
                log.warning("lever %s: no job board with that name, skipping", slug)
                continue
            for j in data:
                yield Job(j["text"], slug, j["hostedUrl"], "lever",
                          (j.get("categories") or {}).get("location") or "", j.get("descriptionPlain", ""))
        except Exception as e:
            log.warning("lever %s: %s", slug, e)


def from_ashby():
    for slug in ASHBY:
        try:
            for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").get("jobs", []):
                loc = j.get("location") or ""
                if j.get("isRemote"):
                    loc = f"Remote - {loc}"
                yield Job(j["title"], slug, j.get("jobUrl") or j.get("applyUrl", ""), "ashby", loc,
                          j.get("descriptionPlain") or clean(j.get("descriptionHtml")))
        except Exception as e:
            log.warning("ashby %s: %s", slug, e)


def from_remoteok():
    try:
        for j in get_json("https://remoteok.com/api")[1:]:
            yield Job(j.get("position", ""), j.get("company", ""), j.get("url", ""), "remoteok",
                      j.get("location") or "Remote", clean(j.get("description")) + " " + " ".join(j.get("tags", [])))
    except Exception as e:
        log.warning("remoteok: %s", e)


def from_remotive():
    try:
        for j in get_json("https://remotive.com/api/remote-jobs?category=software-dev").get("jobs", []):
            yield Job(j["title"], j["company_name"], j["url"], "remotive",
                      "Remote " + (j.get("candidate_required_location") or ""),
                      clean(j.get("description")) + " " + " ".join(j.get("tags", [])))
    except Exception as e:
        log.warning("remotive: %s", e)


def from_jobicy():
    try:
        for j in get_json("https://jobicy.com/api/v2/remote-jobs?count=50&industry=dev").get("jobs", []):
            yield Job(j.get("jobTitle", ""), j.get("companyName", ""), j.get("url", ""), "jobicy",
                      "Remote " + str(j.get("jobGeo") or ""), clean(j.get("jobDescription")))
    except Exception as e:
        log.warning("jobicy: %s", e)


def from_arbeitnow():
    try:
        for j in get_json("https://www.arbeitnow.com/api/job-board-api").get("data", []):
            if j.get("remote"):
                yield Job(j.get("title", ""), j.get("company_name", ""), j.get("url", ""), "arbeitnow",
                          "Remote " + (j.get("location") or ""),
                          clean(j.get("description")) + " " + " ".join(j.get("tags", [])))
    except Exception as e:
        log.warning("arbeitnow: %s", e)


def from_himalayas():
    try:
        for j in get_json("https://himalayas.app/jobs/api?limit=100").get("jobs", []):
            restr = j.get("locationRestrictions") or []
            yield Job(j.get("title", ""), j.get("companyName", ""),
                      j.get("applicationLink") or j.get("guid", ""), "himalayas",
                      ", ".join(restr) if restr else "Worldwide", clean(j.get("description")))
    except Exception as e:
        log.warning("himalayas: %s", e)


def from_wwr():
    for feed in ["remote-programming-jobs", "remote-full-stack-programming-jobs", "remote-back-end-programming-jobs"]:
        try:
            r = requests.get(f"https://weworkremotely.com/categories/{feed}.rss", headers=UA, timeout=25)
            for item in ET.fromstring(r.content).iter("item"):
                raw = item.findtext("title") or ""
                company, _, title = raw.partition(": ")
                yield Job(title or raw, company if title else "", item.findtext("link") or "", "weworkremotely",
                          "Remote " + (item.findtext("region") or ""), clean(item.findtext("description")))
        except Exception as e:
            log.warning("wwr %s: %s", feed, e)


def from_hn():
    try:
        hits = get_json("https://hn.algolia.com/api/v1/search_by_date",
                        params={"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring"})["hits"]
        thread = next(h for h in hits if "who is hiring" in h["title"].lower())
        for c in get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}").get("children", []):
            text = clean(c.get("text"))
            if text:
                first = text.strip().split("\n")[0][:120]
                link = next((a["href"] for a in BeautifulSoup(c["text"], "html.parser").find_all("a", href=True)), "")
                yield Job(first, first.split("|")[0].strip(), link or f"https://news.ycombinator.com/item?id={c['id']}",
                          "hackernews", "", text)
    except Exception as e:
        log.warning("hn: %s", e)


SOURCES = [from_greenhouse, from_lever, from_ashby, from_remoteok, from_remotive, from_jobicy,
           from_arbeitnow, from_himalayas, from_wwr, from_hn]
AUTO_SOURCES = ("greenhouse", "lever", "ashby")
REMOTE_ONLY_SOURCES = {"remoteok", "remotive", "weworkremotely", "jobicy", "himalayas", "arbeitnow"}


# ================================================================== MATCHING ===
def term_re(t):
    return re.compile(rf"(?<![\w.]){re.escape(t)}(?![\w])")


SKILL_RES = {k: (w, term_re(k)) for k, w in SKILLS.items()}
GAP_RES = {k: (w, term_re(k)) for k, w in GAPS.items()}


def is_remote(job):
    loc = job.location.lower()
    if re.search(r"not (a )?remote|(hybrid|on-?site|in-office)\b.{0,50}(required|days? (a|per) week|\d days)", job.description[:1500].lower()):
        return False
    if "hybrid" in loc or "on-site" in loc or "onsite" in loc:
        return False
    return (job.source in REMOTE_ONLY_SOURCES or "remote" in loc or "anywhere" in loc
            or "remote" in job.description[:800].lower())


DESC_RESTRICT_RE = re.compile(
    r"(must|need to|required to|have to)\s+(be\s+)?(located|based|reside|live|resident)[^.\n]{0,60}\b((?-i:US|USA|U\.S\.)|united states|canada|uk|europe|eu|north america|latam|latin america)(?![A-Za-z])"
    r"|authori[sz]ed to work in the ((?-i:US|USA|U\.S\.)|united states|uk|eu|european union|canada)(?![A-Za-z])"
    r"|((?-i:US|U\.S\.)|united states|canadian|uk|eu)[- ](based )?(residents?|citizens?) only"
    r"|(open|available) (only )?to (candidates|applicants|residents) (located |based )?in (the )?((?-i:US|U\.S\.)|united states|canada|uk|europe|eu)(?![A-Za-z])", re.I)


_C = (r"us|usa|u\.s\.a?|united states|canada|uk|united kingdom|england|ireland|germany|france|netherlands|"
      r"spain|portugal|poland|romania|colombia|argentina|brazil|mexico|chile|australia|new zealand|india|"
      r"singapore|latam|latin america|europe|eu")
REGION_BLOCK_RES = [re.compile(p, re.I) for p in [
    rf"\bremote\s*[\(\[\-\u2013\u2014:,/@]*\s*(?:{_C})\b",              # Remote (US), Remote - USA, Remote @ Colombia
    rf"\b(?:{_C})\s*\(\s*remote\s*\)",                                   # Canada (Remote)
    rf"\b(?:{_C})\s*[/|\u2013\u2014-]+\s*remote\b",                      # US/Remote, US - Remote
    rf"\b(?:{_C})[\s-]*(?:only|based|citizens?|citizenship|residents?|work authori[sz]ation)\b",
    r"\bbased only\b",
    rf"\b(?:based|located|reside|residing|living|candidates)\s+(?:in|within)\s+(?:the\s+)?(?:{_C})\b",
    r"\b(?:no|not|without|unable to|cannot|can't|won't|do not|don't)\s+(?:\w+\s+){0,2}(?:visa )?sponsor",
    r"sponsorship (?:is )?not (?:available|offered)",
    r"work authori[sz]ation (?:is )?(?:required|needed)",
    rf"authori[sz]ed to work in the (?:{_C})\b",
    r"security clearance|government clearance",
    r"\b(?:pst|pdt|est|edt)\b", r"\b(?:eastern|pacific|mountain)\s+time", r"\b(?:east|west) coast",
    r"europe(?:an)?\s+time\s*zones?",
]]
STRONG_WORLDWIDE = ["anywhere in the world", "remote (global)", "remote (worldwide)", "worldwide remote",
                    "remote worldwide", "remote, worldwide", "fully remote worldwide", "any location",
                    "from any country", "remote, any location"]
STRONG_RE = re.compile(r"hire (talent |people )?(globally|worldwide|anywhere)|open to (candidates|applicants) "
                       r"(globally|worldwide)|fully distributed|\b(africa|kenya)\b", re.I)
TITLE_BAD_RE = re.compile(r"\b(ios|android|mobile|solutions|support|growth|security|machine learning|data scientist|"
                          r"analyst|writer|sales|marketing|recruit\w*|designer|new grad|early career|junior|"
                          r"graduate|devops|sre|qa|account|customer success|engineering manager)\b")


def region_ok(job):
    """Strict: a listing must not name a city/country/region you can't work from."""
    loc = job.location.lower()
    desc = job.description[:3500].lower()
    rest = re.sub(r"\s+", " ", re.sub(r"\bremote\b|[-\u2013\u2014,/()@]", " ", loc)).strip()
    loc_world = any(w in loc for w in WORLDWIDE)
    strong = any(p in desc for p in STRONG_WORLDWIDE) or bool(STRONG_RE.search(desc))
    if rest and not loc_world:          # e.g. "Remote - New York, NY (HQ)", "Remote Argentina"
        return False
    text = f"{job.location} {job.description[:3500]}"
    if not strong and (any(rx.search(text) for rx in REGION_BLOCK_RES) or DESC_RESTRICT_RE.search(job.description[:3000])):
        return False
    # We Work Remotely labels almost everything "Anywhere in the World", so that label is not trusted.
    job.region_clear = strong or (loc_world and job.source != "weworkremotely")
    return True


def analyse(job):
    text = f"{job.title} {job.description}".lower()
    have = gap = 0
    for k, (w, rx) in SKILL_RES.items():
        if rx.search(text):
            have += w
            job.matched.append(k)
    for k, (w, rx) in GAP_RES.items():
        if rx.search(text):
            gap += w
            job.missing.append(k)
    job.fit = round(100 * have / (have + gap) * min(1, have / 12)) if have + gap else 0
    yrs = [int(m.group(1)) for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", text)
           if "experience" in text[max(0, m.start() - 60):m.end() + 60] and int(m.group(1)) <= 15]
    job.years_req = max(yrs) if yrs else 0
    # Rough estimate, NOT a real probability: even strong cold applications seldom exceed ~20%.
    base = job.fit / 100 * 0.20
    if job.years_req > YEARS_EXPERIENCE:
        base *= max(0.2, 1 - 0.25 * (job.years_req - YEARS_EXPERIENCE))
    if not job.region_clear:
        base *= 0.6
    job.chance = round(base * 100)


def chance_label(c):
    return "Good" if c >= 12 else "Fair" if c >= 6 else "Low"


def line(j):
    return f"{j.title} @ {j.company} | Fit {j.fit}% | est. interview chance ~{j.chance}% ({chance_label(j.chance)})"


STATS = {}


def rej(reason):
    STATS[reason] = STATS.get(reason, 0) + 1
    return False


def passes(job):
    t = job.title.lower()
    if not job.url or BAD_TITLE_RE.search(t):
        return rej("senior/no-url")
    if not TITLE_OK_RE.search(t):
        return rej("title not engineering")
    if TITLE_BAD_RE.search(t):
        return rej("wrong specialty")
    if not is_remote(job):
        return rej("not remote")
    if not region_ok(job) or region_hard_block(job):
        return rej("region blocked")
    analyse(job)
    return job.fit >= MIN_FIT or rej("fit below %d" % MIN_FIT)


def find_jobs():
    seen, out = set(), []
    for src in SOURCES:
        n = total = 0
        STATS.clear()
        log.info("fetching %s ...", src.__name__)
        try:
            for job in src():
                total += 1
                if job.url not in seen:
                    seen.add(job.url)
                    if passes(job):
                        out.append(job); n += 1
        except Exception as e:
            log.warning("%s crashed: %s", src.__name__, e)
        log.info("%s: fetched %d, kept %d (dropped: %s)", src.__name__, total, n, dict(STATS) or "-")
    out.sort(key=lambda j: (j.chance, j.fit), reverse=True)
    for j in out:
        j.description = j.description[:1500]
    json.dump([asdict(j) for j in out], open("ranked_jobs.json", "w"), indent=2)
    with open("ranked_jobs.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fit_%", "est_chance_%", "chance", "title", "company", "source", "location",
                    "years_req", "you_match", "you_lack", "url"])
        for j in out:
            w.writerow([j.fit, j.chance, chance_label(j.chance), j.title, j.company, j.source, j.location,
                        j.years_req or "", ", ".join(j.matched), ", ".join(j.missing), j.url])
    log.info("Saved %d remote matches to ranked_jobs.csv/.json", len(out))
    return out


# ============================================================ STORAGE/NOTIFY ===
def load_json(path):
    return json.load(open(path)) if os.path.exists(path) else []


def save_json(path, data):
    json.dump(data, open(path, "w"), indent=2)


def log_event(outcome, title, company="", url="", detail="", fit=0, chance=0):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(dict(ts=time.time(), outcome=outcome, title=title, company=company, url=url,
                                detail=detail, fit=fit, chance=chance)) + "\n")


def notify(msg, title="Job bot", priority="default"):
    """Push via ntfy.sh (NTFY_TOPIC) and/or Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)."""
    log.info("NOTIFY [%s]: %s", title, msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=msg[:3500].encode("utf-8"), timeout=15,
                          headers={"Title": title, "Priority": priority})
        except Exception as e:
            log.warning("ntfy failed: %s", e)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15,
                          data={"chat_id": chat, "text": f"{title}\n{msg}"[:3900]})
        except Exception as e:
            log.warning("telegram failed: %s", e)


# ======================================================================= LLM ===
CANDIDATE_SUMMARY = """Muya Allan, software engineer & founder, Nairobi, Kenya (remote).
Strong in TypeScript, JavaScript, React, Next.js, Node.js, Fastify, Python, SQL, PostgreSQL, SQLite, WebSockets.
Built OnCue (2026-present): multi-client AI assistance platform - web dashboard, Electron desktop overlay,
Chrome extension, Node.js/Fastify backend; real-time speech-to-text (Deepgram) + LLM integration; JWT/refresh-token
auth, server-side authorization, rate limiting, subscription enforcement, usage/cost tracking, NSIS installers.
Founder of MAKE Ent Studio (2019-present): React/TypeScript/Tailwind/Supabase platform with CMS, payments,
role-based access. Founder of CompLab: tools for video editors/VFX artists. Also AI output evaluation work.
Uses AI coding assistants with rigorous review and testing."""


def _llm_raw(prompt, max_tokens=1500):
    """Cerebras first, then Claude, else None."""
    if os.environ.get("CEREBRAS_API_KEY"):
        try:
            r = requests.post("https://api.cerebras.ai/v1/chat/completions", timeout=60,
                              headers={"Authorization": f"Bearer {os.environ['CEREBRAS_API_KEY']}"},
                              json={"model": os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
                                    "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
        except Exception as e:
            log.warning("Cerebras failed: %s (check CEREBRAS_MODEL vs https://api.cerebras.ai/v1/models)", e)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                              headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                                       "anthropic-version": "2023-06-01"},
                              json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
                                    "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
        except Exception as e:
            log.warning("Claude failed: %s", e)
    return None


_FIX = {"\u2011": "-", "\u2010": "-", "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u2026": "..."}
LLM_RULES = ("\n\nSTYLE RULES: Plain text only: no markdown, no asterisks, no bullet symbols, only ordinary hyphens "
             "and quotes. OnCue began in 2026, so never say how long you have worked on it or on anything else. "
             "Never state years of experience, team sizes, customer or user counts, metrics or scale. Never claim "
             "collaboration, cross-functional work, stakeholders, mentoring or managing people - the candidate "
             "summary does not say so. Describe only what the candidate built, using only the summary's facts.")


def sanitize(text):
    if not text:
        return text
    for k, v in _FIX.items():
        text = text.replace(k, v)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"(?m)^\s*[#>*-]\s+", "", text)
    return text.strip()


def llm(prompt, max_tokens=1500):
    return sanitize(_llm_raw(prompt + LLM_RULES, max_tokens))


def job_ctx(job):
    return f"CANDIDATE:\n{CANDIDATE_SUMMARY}\n\nJOB: {job.title} at {job.company}\n{job.description[:3000]}"


def cover_letter_file(job, letter):
    """Render the approved letter to a PDF for ATS upload. Needs: python -m pip install fpdf2"""
    if not letter:
        return None
    try:
        from fpdf import FPDF
    except Exception:
        log.warning("fpdf2 not installed - cover letter will not be uploaded as a file")
        return None
    fixes = {"\u2011": "-", "\u2010": "-", "\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "}
    for k, v in fixes.items():
        letter = letter.replace(k, v)
    letter = letter.encode("latin-1", "replace").decode("latin-1")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, letter)
    os.makedirs("cover_letters", exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]+", "_", "%s_%s_Cover_Letter_%s" % (PROFILE["first_name"], PROFILE["last_name"], job.company))[:80]
    path = os.path.abspath(os.path.join("cover_letters", name + ".pdf"))
    pdf.output(path)
    return path


def cover_letter(job):
    """Cached, grounded cover letter. Unattended live runs only use letters approved in drafts.json."""
    try:
        drafts = json.load(open("drafts.json", encoding="utf-8-sig"))
    except Exception:
        drafts = {}
    live = bool(os.environ.get("BOT_LIVE"))
    key = "%s|__cover_letter__" % job.url
    d = drafts.get(key)
    if d is None:
        text = str(_cover_letter_raw(job) or "").strip()
        known = (CANDIDATE_SUMMARY + " " + job.title + " " + job.company).lower()
        bad = (not text) or any(t.lower() not in known for t in re.findall(r"\d+(?:[.,]\d+)*%?", text))
        d = {"company": job.company, "title": job.title, "question": "COVER LETTER", "answer": "" if bad else text, "approved": False}
        drafts[key] = d
        with open("drafts.json", "w", encoding="utf-8") as fh:
            json.dump(drafts, fh, ensure_ascii=False, indent=1)
    return d["answer"] if d.get("answer") and (d.get("approved") or not live) else ""


def _cover_letter_raw(job):
    text = llm("Write a concise cover letter (150-200 words, plain text, no placeholders, no flattery, no "
               "invented facts) using only the candidate summary; pick the 2 most relevant projects/skills "
               "for THIS job; sign with the candidate's name. Output only the letter.\n\n" + job_ctx(job))
    if text:
        return text
    skills = ", ".join(job.matched[:5]) or "full-stack development"
    return (f"Hi {job.company} team,\n\nI'm applying for the {job.title} role. I built OnCue, a multi-client AI "
            f"platform (web, Electron, Chrome extension, Node.js/Fastify backend with real-time speech-to-text "
            f"and LLMs), which lines up with your focus on {skills}. Portfolio: {PROFILE['portfolio']}\n\n"
            f"Thanks,\n{PROFILE['first_name']} {PROFILE['last_name']}")


def llm_answer(question, job):
    text = llm("Answer this job-application question in under 120 words, first person, plain text. Use ONLY "
               "facts in the candidate summary; invent nothing. If it can't be answered from the summary, "
               "reply exactly UNKNOWN.\n\n" + job_ctx(job) + f"\n\nQUESTION: {question}", 1200)
    return None if not text or "UNKNOWN" in text else text


# ================================================================ FORM FILLING ===
DECLINE = "__DECLINE__"
DEMOGRAPHIC_RE = re.compile(r"gender|race|ethnic|veteran|disabilit|pronoun|sexual orientation|hispanic|latino|transgender")
VALUES = {"linkedin": PROFILE["linkedin"], "github": PROFILE["github"], "portfolio": PROFILE["portfolio"],
          "location": PROFILE["location"], "phone": PROFILE["phone"], **ANSWERS}
RULES = [(re.compile(p), k) for p, k in [
    (r"authori[sz]ed to work|legally (able|eligible|authori)|right to work|work authori", "work_authorization"),
    (r"sponsor", "sponsorship"),
    (r"salary|compensation|pay expectation|expected pay|hourly rate|desired rate", "salary"),
    (r"notice period|how soon|when can you start|start date|available to start", "notice_period"),
    (r"years of (professional |relevant )?experience|how many years", "years_experience"),
    (r"how did you (hear|find)|where did you (hear|find)", "how_heard"),
    (r"relocat", "relocate"),
    (r"current (company|employer|organi[sz]ation)", "current_company"),
    (r"current (job )?(title|role|position)", "current_title"),
    (r"linkedin", "linkedin"), (r"github", "github"), (r"portfolio|personal (website|site)|website", "portfolio"),
    (r"current location|where are you (based|located)|city|country of residence|location", "location"),
    (r"\b(phone|telephone|cell)\b|mobile (phone|number|no\b)", "phone"),
]]
STANDARD_NAMES = {"first_name", "last_name", "email", "phone", "name", "comments", "org", "resume", "cover_letter"}
LABEL_JS = """const el=arguments[0];let t='';
if(el.id){const l=document.querySelector('label[for="'+el.id+'"]');if(l)t=l.innerText;}
if(!t){const l=el.closest('label');if(l)t=l.innerText;}
if(!t){let p=el.parentElement;for(let i=0;i<4&&p&&!t;i++,p=p.parentElement){
const l=p.querySelector('label,.application-label,legend');if(l)t=l.innerText;}}
if(!t)t=el.getAttribute('aria-label')||el.getAttribute('placeholder')||'';return t;"""
GROUP_JS = """const g=arguments[0].closest('fieldset,[role="radiogroup"],.application-question,.field,.form-field');
if(!g)return '';const l=g.querySelector('legend,.application-label');
return l?l.innerText:g.innerText.split('\\n')[0];"""
OPT_JS = """const e=arguments[0];let l=e.id?document.querySelector('label[for="'+e.id+'"]'):null;
if(!l)l=e.closest('label');return l?l.innerText:(e.value||'');"""


GROUND = ("\n\nANSWER RULES: use ONLY facts stated in the candidate summary. Never invent dates, employers, degrees, titles, numbers or experience. "
          "If the question cannot be answered truthfully from the summary, reply with exactly: UNKNOWN")


DRAFTS_FILE = "drafts.json"


def draft_answer(label, job):
    """Grounded LLM draft cached per job+question. Unattended live runs only use drafts marked approved."""
    try:
        drafts = json.load(open(DRAFTS_FILE, encoding="utf-8-sig"))
    except Exception:
        drafts = {}
    live = bool(os.environ.get("BOT_LIVE"))
    key = "%s|%s" % (job.url, label.strip()[:200])
    d = drafts.get(key)
    if d:
        return d["answer"] if d.get("answer") and (d.get("approved") or not live) else None
    out = llm_answer(label + GROUND, job)
    bad = (not out) or ("UNKNOWN" in str(out).upper()[:30])
    if not bad:
        known = CANDIDATE_SUMMARY.lower()
        bad = any(t.lower() not in known for t in re.findall(r"\d+(?:[.,]\d+)*%?", str(out)))
    drafts[key] = {"company": job.company, "title": job.title, "question": label.strip(), "answer": "" if bad else str(out).strip(), "approved": False}
    with open(DRAFTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(drafts, fh, ensure_ascii=False, indent=1)
    return None if (bad or live) else str(out).strip()


def pick_answer(label, kind, job=None):
    lab = label.lower().strip()
    try:
        for e in json.load(open("answers.json", encoding="utf-8-sig")):
            if re.search(e["pattern"], lab, re.I) and str(e.get("answer", "")).strip() and not str(e.get("answer", "")).upper().startswith("YOUR"):
                return e["answer"]
    except Exception:
        pass
    # --- safety guards (patch4): never guess legal/visa answers, fix country + name questions ---
    if re.search(r"agreement|non-?compete|restrictive covenant|post-employment|conflict of interest|criminal|convicted", lab):
        return None   # legal questions are always yours to answer
    if (re.search(r"sponsor|authori[sz]ed to work|work authori|right to work|legally (able|eligible)", lab)
            and not re.search(r"united states|u\.s\.|\busa?\b", lab)):
        return None   # your configured answers are about the US only
    if re.search(r"preferred (first )?name|name you.{0,20}prefer", lab):
        return PROFILE["first_name"]
    if re.search(r"^\W*country\b|country (in which|of residence|where)|choose the country|current country", lab):
        return "Kenya"
    if re.search(r"previously (worked|employed)|worked (at|for) .{0,40} before|former employee|consulted for", lab):
        return "No"
    if re.search(r"work(ing)? from|payroll tax|where are you (based|located)|country of residence", lab):
        return "Nairobi, Kenya"
    if DEMOGRAPHIC_RE.search(lab):
        return DECLINE if kind in ("select", "radio") else None
    for rx, key in RULES:
        if rx.search(lab):
            return VALUES.get(key) or None
    if kind in ("input", "text") and re.search(r"open.?source|github|projects? (you|that)|link (to )?(any|your)", lab):
        return PROFILE.get("github") or PROFILE.get("portfolio") or None
    if kind == "textarea" and job:
        return draft_answer(label, job)
    return None


def fill(driver, by, key, value):
    from selenium.webdriver.common.by import By
    try:
        el = driver.find_element(getattr(By, by), key)
        el.clear(); el.send_keys(value)
        return True
    except Exception:
        return False


def choose_option(el, ans):
    from selenium.webdriver.support.ui import Select
    s = Select(el)
    opts = [o.text.strip() for o in s.options if o.text.strip() and (o.get_attribute("value") or "")]
    if ans == DECLINE:
        pick = next((t for t in opts if re.search(r"decline|prefer not|don.?t wish|not to (say|answer|disclose)", t, re.I)), None)
    else:
        a = ans.lower()
        pick = (next((t for t in opts if t.lower() == a), None) or next((t for t in opts if t.lower().startswith(a)), None)
                or next((t for t in opts if a in t.lower()), None))
    if pick:
        s.select_by_visible_text(pick)
    return bool(pick)


def answer_custom(driver, job):
    """Fill extra questions we have a configured/LLM answer for; leave the rest empty."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select
    css = ("input:not([type]), input[type='text'], input[type='url'], input[type='number'], "
           "input[type='tel'], textarea, select")
    for el in driver.find_elements(By.CSS_SELECTOR, css):
        try:
            key = el.get_attribute("name") or el.get_attribute("id") or ""
            if not el.is_displayed() or key in STANDARD_NAMES or key.startswith("urls["):
                continue
            kind = el.tag_name.lower()
            if kind == "select":
                if Select(el).first_selected_option.get_attribute("value"):
                    continue
            elif (el.get_attribute("value") or "").strip():
                continue
            label = driver.execute_script(LABEL_JS, el).strip()
            ans = pick_answer(label, kind, job) if label else None
            if ans and ans == PROFILE["phone"] and not re.search(r"phone|telephone|mobile (number|no)|cell|contact number", label, re.I):
                ans = None
            log.info("form answer [%s] %s -> %s", job.company, label[:70].replace("\n", " ").encode("ascii", "replace").decode(), str(ans)[:300].replace("\n", " ").encode("ascii", "replace").decode())
            with open("answers_review.txt", "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\nQ: %s\nA: %s\n\n" % (job.company, job.title, label, ans))
            if not ans and (el.get_attribute("required") or el.get_attribute("aria-required") == "true" or "*" in label):
                UNANSWERED.append(label[:60].replace("\n", " "))
            if ans:
                choose_option(el, ans) if kind == "select" else el.send_keys(ans)
        except Exception:
            continue
    groups = {}
    for r in driver.find_elements(By.CSS_SELECTOR, "input[type='radio']"):
        groups.setdefault(r.get_attribute("name"), []).append(r)
    for radios in groups.values():
        try:
            if any(r.is_selected() for r in radios):
                continue
            ans = pick_answer(driver.execute_script(GROUP_JS, radios[0]) or "", "radio", job)
            if not ans:
                continue
            for r in radios:
                t = driver.execute_script(OPT_JS, r).strip().lower()
                hit = (re.search(r"decline|prefer not|don.?t wish", t) if ans == DECLINE
                       else t == ans.lower() or t.startswith(ans.lower()))
                if hit:
                    driver.execute_script("arguments[0].click()", r)
                    break
        except Exception:
            continue


def prefill(driver, job, letter):
    """Returns (form_recognised, resume_uploaded)."""
    from selenium.webdriver.common.by import By
    p = PROFILE
    if job.source == "ashby":
        found = fill(driver, "ID", "_systemfield_name", p["first_name"] + " " + p["last_name"])
        fill(driver, "ID", "_systemfield_email", p["email"])
        fill(driver, "ID", "phone", p["phone"])
        for tel in driver.find_elements(By.CSS_SELECTOR, "input[type=tel]"):
            try:
                tel.clear(); tel.send_keys(p["phone"])
            except Exception:
                pass
    elif fill(driver, "ID", "first_name", p["first_name"]):  # Greenhouse
        fill(driver, "ID", "last_name", p["last_name"]); fill(driver, "ID", "email", p["email"])
        fill(driver, "ID", "phone", p["phone"]); found = True
    else:  # Lever / generic
        found = fill(driver, "NAME", "name", f"{p['first_name']} {p['last_name']}")
        fill(driver, "NAME", "email", p["email"]); fill(driver, "NAME", "phone", p["phone"])
        fill(driver, "NAME", "urls[LinkedIn]", p["linkedin"]); fill(driver, "NAME", "urls[GitHub]", p["github"])
        fill(driver, "NAME", "urls[Portfolio]", p["portfolio"]); fill(driver, "NAME", "comments", letter)
    try:
        (driver.find_elements(By.CSS_SELECTOR, "#_systemfield_resume") or driver.find_elements(By.CSS_SELECTOR, "input[type='file']"))[0].send_keys(os.path.abspath(p["resume_path"]))
        uploaded = True
    except Exception:
        uploaded = False
    cl = cover_letter_file(job, letter)
    if cl:
        for sel in ("#cover_letter", "input[type='file'][name*='cover' i]", "input[type='file'][id*='cover' i]"):
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                try:
                    els[0].send_keys(cl)
                except Exception:
                    pass
                break
    answer_custom(driver, job)
    return found, uploaded


CAPTCHA_SEL = "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], .h-captcha, .g-recaptcha, [data-sitekey]"


UNANSWERED = []


US_ONLY_RE = re.compile(r"remote\s*[\(\[\-,/\u2013]\s*(us|usa|u\.s\.|united states|uk|eu|europe|canada|north america|americas|latam)\b|\b(us|usa|u\.s\.|uk|eu|canada|north america)[ -](only|based|residents?|time ?zones?)|\b(united states|europe|canada|uk|eu|north america) only\b|must (be|reside|live).{0,40}(us|united states|europe|canada|uk|eu)\b|authori[sz]ed to work in the (us|united states|uk|eu)", re.I)


def region_hard_block(job):
    text = (str(getattr(job, "title", "")) + " " + str(getattr(job, "location", ""))).lower()
    if re.search(r"global|worldwide|anywhere|africa|kenya|emea", text):
        return False
    return bool(US_ONLY_RE.search(text))


def captcha_present(driver):
    """True only for a visible captcha (checkbox/challenge). Invisible reCAPTCHA badges don't count."""
    from selenium.webdriver.common.by import By
    for el in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], .h-captcha"):
        try:
            src = (el.get_attribute("src") or "").lower()
            if "size=invisible" in src or not el.is_displayed() or el.size.get("height", 0) < 40:
                continue
            return True
        except Exception:
            continue
    return False


def empty_required(driver):
    from selenium.webdriver.common.by import By
    missing = []
    for el in driver.find_elements(By.CSS_SELECTOR, "input[required], textarea[required], select[required], [aria-required='true']"):
        try:
            t = (el.get_attribute("type") or "").lower()
            name = el.get_attribute("name") or el.get_attribute("id") or t
            if t in ("hidden", "submit", "button") or (not el.is_displayed() and t != "file"):
                continue
            if t == "radio":
                ok = bool(driver.find_elements(By.CSS_SELECTOR, f"input[name='{name}']:checked"))
            elif t == "file":
                ok = bool((el.get_attribute("value") or "").strip()) or os.path.basename(PROFILE["resume_path"]) in driver.page_source
            elif t == "checkbox":
                ok = el.is_selected()
            else:
                ok = bool((el.get_attribute("value") or "").strip())
            if not ok:
                missing.append(driver.execute_script(LABEL_JS, el).strip()[:40] or name)
        except Exception:
            continue
    return missing


def click_submit(driver):
    from selenium.webdriver.common.by import By
    for sel in ["#btn-submit", "#submit_app", "button[type='submit']", "input[type='submit']"]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
        if els:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'})", els[0])
            els[0].click()
            return True
    return False


def looks_submitted(driver):
    from selenium.webdriver.common.by import By
    time.sleep(6)
    body = driver.find_element(By.TAG_NAME, "body").text.lower()
    phrases = ("thank you for applying", "application has been submitted", "application submitted",
               "we have received your application", "thanks for applying", "application received")
    return "confirmation" in driver.current_url or any(p in body for p in phrases)


def apply_url(job):
    if job.source == "ashby" and not job.url.rstrip("/").endswith("/application"):
        return job.url.rstrip("/") + "/application"
    if job.source == "lever" and not job.url.rstrip("/").endswith("/apply"):
        return job.url.rstrip("/") + "/apply"
    return job.url


# ==================================================================== MODES ===
def autopilot(jobs, max_apply, min_chance, live, retry_skipped):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    done = load_json(HISTORY_FILE)
    skipped = [] if retry_skipped else load_json(SKIP_FILE)
    queue = [j for j in jobs if j.source in AUTO_SOURCES and j.chance >= min_chance
             and j.url not in done and j.url not in skipped]
    log.info("Autopilot: %d eligible, live=%s", len(queue), live)
    os.environ["BOT_LIVE"] = "1" if live else ""
    opts = Options()
    opts.add_argument("--headless=new"); opts.add_argument("--window-size=1280,2200")
    driver = webdriver.Chrome(options=opts)
    count = 0
    try:
        for job in queue:
            if count >= max_apply:
                break
            reason = None
            try:
                driver.get(apply_url(job)); time.sleep(4)
                if captcha_present(driver):
                    reason = "captcha"
                else:
                    UNANSWERED.clear(); found, uploaded = prefill(driver, job, cover_letter(job))
                    if not (found and uploaded):
                        reason = "form not recognised"
                    else:
                        missing = empty_required(driver) + list(dict.fromkeys(UNANSWERED))
                        if missing:
                            reason = "needs your answer: " + "; ".join(missing[:3])
                if reason is None:
                    if not live:
                        log_event("dry_run", job.title, job.company, job.url, "", job.fit, job.chance)
                        count += 1
                    elif click_submit(driver) and looks_submitted(driver):
                        done.append(job.url); save_json(HISTORY_FILE, done)
                        log_event("applied", job.title, job.company, job.url, "", job.fit, job.chance)
                        count += 1
                        if NOTIFY_EACH_APPLICATION:
                            notify(f"Applied: {line(job)}\n{job.url}")
                    else:
                        reason = "submitted but no confirmation seen - check your inbox"
            except Exception as e:
                reason = f"error: {str(e)[:80]}"
            if reason:
                skipped.append(job.url); save_json(SKIP_FILE, skipped)
                log_event("needs_you", job.title, job.company, job.url, reason, job.fit, job.chance)
            time.sleep(random.uniform(20, 40))
    finally:
        driver.quit()
        save_json(SKIP_FILE, skipped)
    log.info("Autopilot finished: %d %s", count, "applied" if live else "dry-run matches")
    mode = "live" if live else "dry run"
    notify("Autopilot (%s): %d %s, %d eligible, %d skipped total" % (mode, count, "applied" if live else "would apply", len(queue), len(skipped)), title="Job bot - autopilot")


def assisted_apply(jobs, top):
    from selenium import webdriver
    history = load_json(HISTORY_FILE)
    driver = webdriver.Chrome()
    try:
        for job in [j for j in jobs if j.url not in history][:top]:
            print(f"\n{line(job)}")
            letter = cover_letter(job)
            driver.get(apply_url(job))
            input("Click Apply if needed, then press Enter to auto-fill...")
            prefill(driver, job, letter)
            print("\n--- cover letter ---\n" + letter + "\n")
            ans = input("Review, solve captcha, SUBMIT yourself. Submitted? [y/n/q] ").lower()
            if ans == "y":
                history.append(job.url); save_json(HISTORY_FILE, history)
                log_event("applied", job.title, job.company, job.url, "manual submit", job.fit, job.chance)
            elif ans == "q":
                break
    finally:
        driver.quit()


def classify_email(subject, body, sender, companies=()):
    """Returns 'interview', 'rejection' or None."""
    text = f"{subject}\n{body}".lower()
    s = f"{sender} {text}".lower()
    if any(w in text for w in REJECT_WORDS):
        return "rejection"
    if any(w in text for w in INTERVIEW_WORDS) and (
            any(c and c.lower() in s for c in companies) or any(h in s for h in RECRUITING_HINTS)):
        return "interview"
    return None


def email_body(msg):
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                txt = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
            except Exception:
                continue
            return (clean(txt) if part.get_content_type() == "text/html" else txt)[:2500]
    return ""


def check_inbox(days=3):
    pw = os.environ.get("IMAP_APP_PASSWORD")
    if not pw:
        log.warning("Set IMAP_APP_PASSWORD (Gmail app password) in .env to enable interview alerts.")
        return
    user, host = os.environ.get("IMAP_USER", PROFILE["email"]), os.environ.get("IMAP_HOST", "imap.gmail.com")
    companies = {e["company"] for e in (json.loads(l) for l in open(LOG_FILE)) if e["outcome"] == "applied"} \
        if os.path.exists(LOG_FILE) else set()
    seen = set(load_json(MAIL_FILE))
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, pw); M.select("INBOX", readonly=True)
        since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = M.search(None, "SINCE", since)
        for num in data[0].split()[-200:]:
            _, md = M.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(md[0][1])
            mid = msg.get("Message-ID", num.decode())
            if mid in seen:
                continue
            seen.add(mid)
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            sender = str(make_header(decode_header(msg.get("From", ""))))
            kind = classify_email(subject, email_body(msg), sender, companies)
            if kind == "interview":
                log_event("interview", subject, sender, "", "")
                notify(f"Possible interview / next step!\nFrom: {sender}\nSubject: {subject}\n"
                       f"Open your inbox and reply soon.", "INTERVIEW", "high")
            elif kind == "rejection":
                log_event("rejection", subject, sender, "", "")
    finally:
        save_json(MAIL_FILE, list(seen)[-2000:])
        try:
            M.logout()
        except Exception:
            pass


def daily_summary(hours=24):
    cutoff = time.time() - hours * 3600
    ev = []
    if os.path.exists(LOG_FILE):
        for l in open(LOG_FILE):
            try:
                e = json.loads(l)
                if e["ts"] >= cutoff:
                    ev.append(e)
            except (ValueError, KeyError):
                pass
    by = lambda o: [e for e in ev if e["outcome"] == o]
    applied, dry, needs, ints, rej = by("applied"), by("dry_run"), by("needs_you"), by("interview"), by("rejection")
    fmt = lambda e: f"- {e['title']} @ {e['company']} | Fit {e['fit']}% | ~{e['chance']}%\n  {e['url']}"
    out = [f"Evening summary (last {hours}h)",
           f"Applied {len(applied)} | Needs you {len(needs)} | Interview emails {len(ints)} | "
           f"Rejections {len(rej)} | Total applied ever {len(load_json(HISTORY_FILE))}"]
    if ints:
        out += ["\nINTERVIEW / NEXT STEP:"] + [f"- {e['title']} (from {e['company']})" for e in ints]
    if applied:
        out += ["\nApplied:"] + [fmt(e) for e in applied[:8]]
    if dry:
        out += ["\nWould apply (dry run):"] + [fmt(e) for e in dry[:5]]
    if needs:
        out += ["\nNeeds you:"] + [f"{fmt(e)}\n  why: {e['detail']}" for e in needs[:6]]
    try:
        done = set(load_json(HISTORY_FILE))
        ranked = [j for j in json.load(open("ranked_jobs.json")) if j["url"] not in done]
        manual = [j for j in ranked if j["source"] not in AUTO_SOURCES][:5]
        if manual:
            out += ["\nBest matches to apply to yourself:"] + [
                f"- {j['title']} @ {j['company']} | Fit {j['fit']}% | ~{j['chance']}% ({chance_label(j['chance'])})\n  {j['url']}"
                for j in manual]
    except Exception:
        pass
    out.append("\n(Chance % is a rough estimate, not a prediction.)")
    notify("\n".join(out), "Job bot - evening summary", "high" if ints else "default")


RISK_RES = [
    (re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|several|many|numerous|\d+)\+?\s+(years?|yrs)\b", re.I),
     "states a number of years"),
    (re.compile(r"\b(collaborat\w*|cross-functional|stakeholders?|mentor\w*|managed|team of|led a team|teams)\b", re.I),
     "mentions teams/collaboration (not in your summary)"),
    (re.compile(r"\b(at scale|millions?|thousands?|high-throughput|enterprise)\b", re.I), "scale/size claim"),
]


def review_drafts():
    """Approve, reject or rewrite AI drafts. Live runs only use approved drafts."""
    try:
        drafts = json.load(open(DRAFTS_FILE, encoding="utf-8-sig"))
    except Exception:
        print("No drafts yet. Run: python apply_bot.py auto")
        return
    pending = [(k, d) for k, d in drafts.items() if d.get("answer") and not d.get("approved")]
    print(f"{len(pending)} draft(s) waiting.  [a]pprove  [r]eject  [e]dit  [s]kip  [q]uit")

    def save():
        with open(DRAFTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(drafts, fh, ensure_ascii=False, indent=1)

    for k, d in pending:
        print("\n" + "=" * 70)
        print(f"{d.get('company')} - {d.get('title')}\nQ: {d.get('question')}\n\n{d['answer']}")
        flags = [m for rx, m in RISK_RES if rx.search(d["answer"])]
        if flags:
            print("\n!! CHECK:", "; ".join(flags), "- is every claim true?")
        while True:
            c = input("\n[a/r/e/s/q] > ").strip().lower()
            if c == "a":
                d["approved"] = True
            elif c == "r":
                d["answer"] = ""
            elif c == "e":
                new = sanitize(input("Type your replacement answer on ONE line (blank = cancel): "))
                if not new:
                    continue
                d["answer"], d["approved"] = new, True
            elif c == "q":
                save()
                return
            elif c != "s":
                continue
            save()
            break
    print("\nDone. Approved drafts will be used by: python apply_bot.py auto --live")



if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["find", "apply", "auto", "inbox", "summary", "review"])
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max", type=int, default=5, help="auto: max applications per run")
    ap.add_argument("--min-chance", type=int, default=MIN_CHANCE, help="auto: min estimated chance %%")
    ap.add_argument("--live", action="store_true", help="auto: actually submit (default: dry run)")
    ap.add_argument("--retry-skipped", action="store_true", help="auto: retry jobs skipped earlier")
    args = ap.parse_args()
    if args.mode == "review":
        review_drafts(); sys.exit()
    if args.mode == "inbox":
        check_inbox(); sys.exit()
    if args.mode == "summary":
        daily_summary(); sys.exit()
    results = find_jobs()
    if args.mode in ("apply", "auto") and not os.path.exists(PROFILE["resume_path"]):
        sys.exit(f"Put your resume at {PROFILE['resume_path']} first.")
    if args.mode == "apply":
        assisted_apply(results, args.top)
    elif args.mode == "auto":
        autopilot(results, args.max, args.min_chance, args.live, args.retry_skipped)