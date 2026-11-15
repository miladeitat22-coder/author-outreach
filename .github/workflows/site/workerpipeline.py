"""Author outreach worker. Usage: python pipeline.py discover|analyze|write|comment|send|all

Free stack: Playwright (scrape), Gemini free tier (vision + writing), Supabase free (db),
Gmail SMTP app passwords (send), GitHub Actions (schedule).
Rules baked in: public pages only, text-only storage, per-inbox and per-account daily caps,
suppression list, unsubscribe line in every email, no captcha bypass (an account that hits one is paused).
"""
import base64, datetime as dt, json, os, random, re, smtplib, ssl, sys, time
from email.message import EmailMessage

import requests
from supabase import create_client

db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SENDERS = json.loads(os.environ.get("SENDERS_JSON", "{}"))      # {"a@gmail.com": "app-password"}
PORTFOLIO = os.environ.get("PORTFOLIO_URL", "https://example.netlify.app")
STUDIO = os.environ.get("STUDIO_NAME", "Your Studio")
OWNER_NAME = os.environ.get("OWNER_NAME", "Your Name")
MAX_PAGES = int(os.environ.get("MAX_PAGES_SAMPLED", 12))
MAX_NEW_WORKS = int(os.environ.get("MAX_NEW_WORKS_PER_RUN", 15))
AUTO_APPROVE = os.environ.get("AUTO_APPROVE", "1") == "1"        # 0 = drafts wait in the dashboard queue
GAP = (int(os.environ.get("GAP_MIN_S", 30)), int(os.environ.get("GAP_MAX_S", 40)))   # seconds between emails
SESSIONS = json.loads(os.environ.get("PLATFORM_SESSIONS_JSON", "{}"))   # {"account-label": [cookies]}


def log(kind, msg):
    print(f"[{kind}] {msg}")
    db.table("events").insert({"kind": kind, "message": msg[:500]}).execute()


# ---------- 1. DISCOVER ----------------------------------------------------
# One entry per site. Selectors must be tuned against the live site (open it in a
# browser, inspect, adjust). Keep to public listing/work pages and be polite.
SITES = {
    "webtoon": dict(listing="https://www.webtoons.com/en/canvas/list?genreTab=ALL&sortOrder=LIKEIT",
                    work_link=r"/canvas/.+/list\?title_no=\d+", author_sel=".author_area, .author",
                    page_img_sel="#_imageList img, .viewer_img img",
                    comment_box="textarea", comment_submit="button[type=submit], button:has-text('Post')"),
    "namicomi": dict(listing="https://namicomi.com/en/explore", work_link=r"/title/[\w-]+",
                     author_sel="a[href*='/user/'], .author", page_img_sel="img.page, .reader img",
                     comment_box="textarea", comment_submit="button:has-text('Post'), button:has-text('Send')"),
    "mangaplus": dict(listing="https://mangaplus-creators.shueisha.co.jp/", work_link=r"/(?:titles|title)/\w+",
                      author_sel=".author, .creator", page_img_sel="img",
                      comment_box="textarea", comment_submit="button[type=submit]"),
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def discover():
    from playwright.sync_api import sync_playwright
    added = 0
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(user_agent="Mozilla/5.0 (compatible; StudioOutreachBot/1.0; +%s)" % PORTFOLIO)
        for pid, cfg in SITES.items():
            if not db.table("platforms").select("enabled").eq("id", pid).single().execute().data["enabled"]:
                continue
            try:
                pg.goto(cfg["listing"], timeout=45000); time.sleep(2)
                links = {a.get_attribute("href") for a in pg.query_selector_all("a[href]")
                         if re.search(cfg["work_link"], a.get_attribute("href") or "")}
                for href in list(links)[:MAX_NEW_WORKS]:
                    url = requests.compat.urljoin(cfg["listing"], href)
                    if db.table("works").select("id").eq("url", url).execute().data:
                        continue
                    pg.goto(url, timeout=45000); time.sleep(random.uniform(2, 5))
                    title = (pg.title() or "").strip()
                    name_el = pg.query_selector(cfg["author_sel"])
                    name = name_el.inner_text().strip() if name_el else None
                    body = pg.inner_text("body")
                    emails = [e for e in EMAIL_RE.findall(body) if not e.endswith((".png", ".jpg"))]
                    handle = (name or url).lower()[:80]
                    a = db.table("authors").upsert({"platform": pid, "handle": handle, "real_name": name,
                                                    "email": emails[0] if emails else None,
                                                    "profile_url": url}, on_conflict="platform,handle").execute().data[0]
                    db.table("works").insert({"author_id": a["id"], "title": title, "url": url}).execute()
                    added += 1
            except Exception as e:
                log("error", f"discover {pid}: {e}")
        br.close()
    log("discover", f"{added} new works")


# ---------- 2. ANALYZE (every sampled page, then synthesis) ---------------------
def gemini(parts, json_out=False):
    body = {"contents": [{"parts": parts}]}
    if json_out:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    for attempt in range(4):
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                          params={"key": GEMINI_KEY}, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1)); continue
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raise RuntimeError("Gemini rate limit")


def page_images(url, sel):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch(); pg = br.new_page(); pg.goto(url, timeout=45000); time.sleep(3)
        srcs = [i.get_attribute("data-url") or i.get_attribute("src") for i in pg.query_selector_all(sel)]
        br.close()
    srcs = [s for s in srcs if s and s.startswith("http")]
    step = max(1, len(srcs) // MAX_PAGES)
    return srcs[::step][:MAX_PAGES], len(srcs)


def analyze():
    rows = db.table("works").select("*, authors(platform)").eq("status", "new").limit(MAX_NEW_WORKS).execute().data
    for w in rows:
        try:
            cfg = SITES.get(w["authors"]["platform"], {"page_img_sel": "img"})
            srcs, total = page_images(w["url"], cfg["page_img_sel"])
            notes = []
            for i in range(0, len(srcs), 4):                       # 4 pages per call
                parts = [{"text": "Describe each comic page: scene, characters, emotion, art technique, "
                                  "memorable visual moments. Be specific, under 120 words per page."}]
                for s in srcs[i:i + 4]:
                    img = requests.get(s, headers={"Referer": w["url"]}, timeout=30)
                    parts.append({"inline_data": {"mime_type": img.headers.get("content-type", "image/jpeg"),
                                                  "data": base64.b64encode(img.content).decode()}})
                notes.append(gemini(parts))
            prof = json.loads(gemini([{"text":
                "From these page notes of one comic, return JSON with keys: logline, genre, tone, art_style, "
                "strengths (3 items), moments (3 specific scenes worth animating), animation_ideas (3), "
                "summary (120 words). Only state what the notes support.\n\n" + "\n---\n".join(notes)}], True))
            db.table("works").update({"profile": prof, "summary": prof.get("summary"), "pages": total,
                                      "status": "analyzed", "analyzed_at": dt.datetime.utcnow().isoformat()}
                                     ).eq("id", w["id"]).execute()
            log("analyze", f"{w['title']}")
        except Exception as e:
            db.table("works").update({"status": "skipped"}).eq("id", w["id"]).execute()
            log("error", f"analyze {w['url']}: {e}")


# ---------- 3. WRITE (approved at once, or queued as drafts if AUTO_APPROVE=0) ---
WRITER = """You write outreach for {studio}, a studio that turns comics and books into animated visuals.
Author: {name}. Work: "{title}". Analysis: {profile}
Write JSON {{"comment": "...", "subject": "...", "email": "..."}}.
Rules: address the author by name. Refer to two concrete moments from the analysis. Elegant, warm, confident,
plain words a senior creative director would use: premium means precise, not flowery. No stacked superlatives,
no invented facts, no claims about a reader count or sales. comment: max 60 words, public-safe, no links.
email: 110-160 words, offer a free 15-second animated sample of one moment, link {portfolio}, sign off
as {owner} of {studio}."""


def write():
    works = db.table("works").select("*, authors(*)").eq("status", "analyzed").limit(MAX_NEW_WORKS).execute().data
    for w in works:
        a = w["authors"]
        out = json.loads(gemini([{"text": WRITER.format(studio=STUDIO, name=a["real_name"] or a["handle"],
                      title=w["title"], profile=json.dumps(w["profile"]), portfolio=PORTFOLIO, owner=OWNER_NAME)}], True))
        base = {"work_id": w["id"], "author_id": a["id"], "status": "approved" if AUTO_APPROVE else "draft"}
        db.table("outreach").insert({**base, "channel": "comment", "body": out["comment"]}).execute()
        if a.get("email"):
            db.table("outreach").insert({**base, "channel": "email", "subject": out["subject"], "body": out["email"]}).execute()
        db.table("works").update({"status": "written"}).eq("id", w["id"]).execute()
        log("write", f"drafts ready: {w['title']}")


# ---------- 4. COMMENT (logged-in accounts you own; one comment per work) ----------
def reset_daily():
    today = dt.date.today().isoformat()
    db.table("senders").update({"sent_today": 0, "reset_on": today}).lt("reset_on", today).execute()
    db.table("platform_accounts").update({"posted_today": 0, "reset_on": today}).lt("reset_on", today).execute()


def comment(budget_s=900):
    from playwright.sync_api import sync_playwright
    reset_daily()
    t0 = time.time()
    with sync_playwright() as p:
        br = p.chromium.launch()
        while time.time() - t0 < budget_s:
            q = (db.table("outreach").select("*, works(url, authors(platform))").eq("status", "approved")
                 .eq("channel", "comment").limit(1).execute().data)
            if not q:
                break
            o = q[0]; plat = o["works"]["authors"]["platform"]
            accts = [a for a in db.table("platform_accounts").select("*").eq("platform", plat).eq("active", True)
                     .execute().data if a["posted_today"] < a["daily_cap"] and a["label"] in SESSIONS]
            if not accts:
                db.table("outreach").update({"status": "failed", "error": "no account with room"}).eq("id", o["id"]).execute(); continue
            acct = min(accts, key=lambda a: a["posted_today"]); cfg = SITES[plat]
            ctx = br.new_context(); ctx.add_cookies(SESSIONS[acct["label"]]); pg = ctx.new_page()
            try:
                pg.goto(o["works"]["url"], timeout=45000); time.sleep(random.uniform(3, 6))
                if pg.query_selector("iframe[src*='captcha'], .g-recaptcha, #captcha"):
                    db.table("platform_accounts").update({"active": False}).eq("id", acct["id"]).execute()
                    raise RuntimeError("captcha shown, account paused (not bypassed)")
                box = pg.query_selector(cfg["comment_box"])
                if not box:
                    raise RuntimeError("comment box not found (not logged in, or selector needs tuning)")
                box.click(); pg.keyboard.type(o["body"], delay=random.randint(30, 80)); time.sleep(random.uniform(1, 3))
                pg.click(cfg["comment_submit"]); time.sleep(4)
                db.table("outreach").update({"status": "sent", "sender": acct["label"],
                                             "sent_at": dt.datetime.utcnow().isoformat()}).eq("id", o["id"]).execute()
                db.table("platform_accounts").update({"posted_today": acct["posted_today"] + 1}).eq("id", acct["id"]).execute()
                log("comment", f"{acct['label']} -> {o['works']['url']}")
            except Exception as e:
                db.table("outreach").update({"status": "failed", "error": str(e)[:300]}).eq("id", o["id"]).execute()
                log("error", f"comment {acct['label']}: {e}")
            finally:
                ctx.close()
            time.sleep(random.uniform(90, 240))
        br.close()


# ---------- 5. SEND (round-robin across all inboxes, GAP seconds apart) ----------
def send(budget_s=1800):
    reset_daily()
    inboxes = [s for s in db.table("senders").select("*").eq("active", True).execute().data if s["address"] in SENDERS]
    t0 = time.time()
    while inboxes and time.time() - t0 < budget_s:
        ready = [s for s in inboxes if s["sent_today"] < s["daily_cap"]]
        if not ready:
            break
        q = (db.table("outreach").select("*, authors(email, real_name)").eq("status", "approved")
             .eq("channel", "email").limit(1).execute().data)
        if not q:
            break
        o, to = q[0], q[0]["authors"]["email"]
        if db.table("suppression").select("email").eq("email", to).execute().data:
            db.table("outreach").update({"status": "rejected", "error": "suppressed"}).eq("id", o["id"]).execute(); continue
        s = min(ready, key=lambda x: x["sent_today"])                  # least-used inbox goes next
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = f"{OWNER_NAME} <{s['address']}>", to, o["subject"]
        m["List-Unsubscribe"] = f"<mailto:{s['address']}?subject=unsubscribe>"
        m.set_content(o["body"] + "\n\nIf this isn't for you, reply \"unsubscribe\" and I won't write again.")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as sm:
                sm.login(s["address"], SENDERS[s["address"]]); sm.send_message(m)
            db.table("outreach").update({"status": "sent", "sender": s["address"],
                                         "sent_at": dt.datetime.utcnow().isoformat()}).eq("id", o["id"]).execute()
            s["sent_today"] += 1
            db.table("senders").update({"sent_today": s["sent_today"]}).eq("id", s["id"]).execute()
            time.sleep(random.uniform(*GAP))
        except Exception as e:                                          # inbox problem: park it for this run
            db.table("outreach").update({"status": "approved", "error": f"{s['address']}: {str(e)[:200]}"}).eq("id", o["id"]).execute()
            log("error", f"send {s['address']}: {e}"); inboxes.remove(s)


if __name__ == "__main__":
    steps = {"discover": discover, "analyze": analyze, "write": write, "comment": comment, "send": send}
    for name in (steps if sys.argv[1] == "all" else [sys.argv[1]]):
        steps[name]()
