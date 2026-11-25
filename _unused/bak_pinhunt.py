#!/usr/bin/env python3
"""
pinhunt - track Pinterest smartlink-spam pins in a local SQLite database.

Only the Python standard library is used. Final ad/smartlink destinations are
NEVER requested: shorteners are followed with HEAD/GET-without-redirect and
google.com/url wrappers are decoded offline.

Typical use
-----------
    python3 pinhunt.py add seeds.txt                 # pin/board URLs or IDs (file or args)
    python3 pinhunt.py run                           # default full procedure
    python3 pinhunt.py report                        # rebuild report.md + pins.csv

Individual steps
----------------
    fetch      fetch details for pins not yet fetched
    resolve    unwrap outbound links (shorteners, google.com/url)
    profiles   fetch poster profiles
    search     search Pinterest for bait phrases       (--query to add your own)
    expand     related pins, poster boards/created pins, user-added boards
    recheck    re-fetch already-known pins to detect removals (--days N)
    archive    save an evidence folder per bait pin (JSON, redirect chain, profile,
               page HTML, media via gallery-dl, SHA-256 manifest); --accounts, --wayback
    status     quick counts
"""
import argparse, collections, csv, datetime as dt, hashlib, importlib.util, json, os, re, shutil
import sqlite3, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DEFAULT_QUERIES = [
    "WATCH NOW▶️🔞 10M+ Monthly Views",
    "WATCH NOW▶🔞 10M+ Monthly Views",
    "10M+ Monthly Views",
    "WATCH VIDEO 🥵",
    "▶️WATCH VIDEO▶️",
    "CLICK ON SITE FOR ENJOY",
    "open Now 🌙",
    "Tap the image link to get the exact look",
]

BAIT_RE = re.compile(
    r"WATCH\s*(NOW|VIDEO)|10M\+\s*Monthly|🔞|🥵|CLICK ON SITE|open\s*Now|"
    r"Tap the image link|watch-now-10m", re.I)

REDIRECTORS = {
    "tinyurl.com", "www.tinyurl.com", "bit.ly", "bitly.com", "t.co", "cutt.ly", "is.gd",
    "rb.gy", "shorturl.at", "tiny.cc", "ow.ly", "goo.gl", "t.ly", "rebrand.ly", "s.id",
    "shorturl.gg", "v.gd", "lnkd.in", "buff.ly",
}
SUSPECT_HOSTS = ("tinyurl.", "netlify.app", "bit.ly", "cutt.ly", "rb.gy", "shorturl", "is.gd",
                 "t.ly", "vercel.app", "github.io", "blogspot.", "sites.google.", "pages.dev",
                 "web.app", "firebaseapp.com", "tiny.cc", "s.id")

PIN_ID_RE = re.compile(r"\d{5,20}")   # real pin IDs are numeric

RESERVED_PATHS = {"pin", "search", "ideas", "today", "_", "settings", "business", "login",
                  "explore", "categories", "topics", "shopping", "videos", "news_hub", "about"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pins(
  pin_id TEXT PRIMARY KEY, source TEXT, first_seen TEXT, status TEXT DEFAULT 'new',
  fetched_at TEXT, error TEXT, created_at TEXT, pinner TEXT, native_creator TEXT,
  is_repin INTEGER, board_id TEXT, board_name TEXT, title TEXT, description TEXT,
  repin_count INTEGER, link TEXT, image_signature TEXT, shard INTEGER, bait INTEGER,
  expanded INTEGER DEFAULT 0, raw TEXT);
CREATE TABLE IF NOT EXISTS links(
  link TEXT PRIMARY KEY, resolved_at TEXT, final_url TEXT, final_domain TEXT, path TEXT,
  key TEXT, chain TEXT, kind TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, first_seen TEXT, fetched_at TEXT, status TEXT DEFAULT 'new',
  error TEXT, full_name TEXT, created_at TEXT, pin_count INTEGER, board_count INTEGER,
  follower_count INTEGER, following_count INTEGER, website_url TEXT, about TEXT,
  crawled INTEGER DEFAULT 0, raw TEXT);
CREATE TABLE IF NOT EXISTS boards(
  board_key TEXT PRIMARY KEY, board_id TEXT, url TEXT, owner TEXT, name TEXT,
  pin_count INTEGER, created_at TEXT, first_seen TEXT, crawled INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, kind TEXT, PRIMARY KEY(src, dst, kind));
CREATE TABLE IF NOT EXISTS searches(query TEXT, page INTEGER, run_at TEXT, results INTEGER,
  bait_hits INTEGER);
"""


# ----------------------------------------------------------------- helpers
def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def iso(rfc):
    if not rfc:
        return None
    try:
        return parsedate_to_datetime(rfc).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return str(rfc)


def parse_iso(s):
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def txt(x, n=200):
    if isinstance(x, dict):
        x = x.get("format") or x.get("text") or x.get("name") or ""
    return str(x or "").replace("\r", " ").replace("\n", " ").strip()[:n]


def uname(x):
    return x.get("username") if isinstance(x, dict) else None


def log(*a):
    print(*a, flush=True)


def looks_bait(title="", desc="", link="", board=""):
    if BAIT_RE.search(f"{title} {desc} {board}"):
        return True
    l = (link or "").lower()
    return any(h in l for h in SUSPECT_HOSTS)


def parse_inputs(items):
    """Return (pin_ids, boards[(owner, slug)]) from URLs, IDs or files of them."""
    lines = []
    for it in items:
        if os.path.isfile(it):
            with open(it, encoding="utf-8") as f:
                lines += f.read().split()
        else:
            lines += it.split()
    pins, boards = [], []
    for raw in lines:
        s = raw.strip().strip("[]()<>\"'")
        if not s:
            continue
        m = re.search(r"pinterest\.[a-z.]+/pin/(?:[^/?#]*--)?(\d{5,20})", s)
        if m:
            pins.append(m.group(1)); continue
        if re.fullmatch(r"\d{9,20}", s):
            pins.append(s); continue
        m = re.search(r"pinterest\.[a-z.]+/([^/?#]+)/([^/?#]+)/?(?:[?#].*)?$", s)
        if m and m.group(1).lower() not in RESERVED_PATHS and not m.group(2).startswith("_"):
            boards.append((m.group(1), m.group(2)))
    return list(dict.fromkeys(pins)), list(dict.fromkeys(boards))


# ----------------------------------------------------------------- Pinterest API
class NotFound(Exception):
    pass


class Api:
    def __init__(self, delay=1.5):
        self.delay, self.last = delay, 0.0

    def call(self, resource, options, source="/", tries=4):
        q = urllib.parse.urlencode({"source_url": source,
                                    "data": json.dumps({"options": options, "context": {}})})
        url = f"https://www.pinterest.com/resource/{resource}/get/?{q}"
        for i in range(tries):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA, "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-Pinterest-PWS-Handler": "www/[username].js"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.load(r).get("resource_response") or {}
                err = resp.get("error") or {}
                if isinstance(err, dict) and err.get("http_status") == 404:
                    raise NotFound(resource)
                return resp
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(resource)
                if e.code in (403, 429, 500, 502, 503) and i < tries - 1:
                    back = 20 * (i + 1)
                    log(f"    [{e.code} on {resource}; backing off {back}s]")
                    time.sleep(back); continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if i < tries - 1:
                    time.sleep(5); continue
                raise

    def paged(self, resource, options, source="/", max_pages=5):
        bookmark = None
        for _ in range(max_pages):
            o = dict(options)
            if bookmark:
                o["bookmarks"] = [bookmark]
            resp = self.call(resource, o, source)
            data = resp.get("data") or []
            if isinstance(data, dict):
                data = data.get("results") or []
            for it in data:
                if isinstance(it, dict):
                    yield it
            bookmark = resp.get("bookmark")
            if not bookmark or bookmark == "-end-" or not data:
                return


# ----------------------------------------------------------------- link unwrapping
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def unwrap(link):
    """Follow shorteners / decode google.com/url. Never requests the final destination."""
    chain, url, err = [link], link, None
    for _ in range(10):
        p = urllib.parse.urlparse(url)
        host = p.netloc.lower()
        if host in ("google.com", "www.google.com") and p.path == "/url":
            qs = urllib.parse.parse_qs(p.query)
            nxt = (qs.get("q") or qs.get("url") or [None])[0]
            if not nxt:
                break
            url = nxt; chain.append(url + "  [decoded offline]"); continue
        if host not in REDIRECTORS:
            break
        moved = False
        for method in ("HEAD", "GET"):
            try:
                time.sleep(0.5)
                r = _opener.open(urllib.request.Request(url, method=method,
                                 headers={"User-Agent": UA}), timeout=20)
                err = f"{host} answered {r.status} without redirecting"
                r.close(); break
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location")
                if e.code in (301, 302, 303, 307, 308) and loc:
                    url = urllib.parse.urljoin(url, loc); chain.append(url); moved = True; err = None
                    break
                if e.code == 405 and method == "HEAD":
                    continue
                err = f"HTTP {e.code} at {host}"; break
            except Exception as e:
                err = f"{type(e).__name__}: {e}"; break
        if not moved:
            break
    return chain, url, err


def classify(link, chain, final):
    fp = urllib.parse.urlparse(final)
    host = fp.netloc.lower()
    key = (urllib.parse.parse_qs(fp.query).get("key") or [""])[0]
    via_google = any("decoded offline" in c for c in chain)
    lhost = urllib.parse.urlparse(link).netloc.lower()
    if key and "tinyurl" in lhost and via_google:
        kind = "tinyurl > google/url > smartlink"
    elif key:
        kind = "smartlink (?key=)"
    elif host.endswith("netlify.app"):
        kind = "netlify tracker page"
    elif host in REDIRECTORS:
        kind = "shortener (unresolved)"
    else:
        kind = f"other: {host}"
    return host, fp.path, key, kind


# ----------------------------------------------------------------- database
class DB:
    def __init__(self, path):
        # wait up to 2 min for locks held by other processes (viewer, second pinhunt run)
        self.c = sqlite3.connect(path, timeout=120)
        self.c.row_factory = sqlite3.Row
        try:
            self.c.execute("PRAGMA journal_mode=WAL")   # readers no longer block writers
            self.c.execute("PRAGMA busy_timeout=120000")
        except sqlite3.OperationalError:
            pass
        self.c.executescript(SCHEMA)
        for table, col in (("pins", "archived_at"), ("pins", "archive_dir"), ("users", "archived_at")):
            cols = {r[1] for r in self.c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        self.c.commit()

    def q(self, sql, *a):
        return self.c.execute(sql, a).fetchall()

    def x(self, sql, *a):
        self.c.execute(sql, a)

    def commit(self):
        self.c.commit()

    def add_pin(self, pid, source):
        cur = self.c.execute("INSERT OR IGNORE INTO pins(pin_id, source, first_seen) VALUES(?,?,?)",
                             (str(pid), source, now()))
        return cur.rowcount > 0

    def add_user(self, u):
        if u:
            self.x("INSERT OR IGNORE INTO users(username, first_seen) VALUES(?,?)", u, now())

    def add_board(self, key, **kw):
        self.x("INSERT OR IGNORE INTO boards(board_key, first_seen) VALUES(?,?)", key, now())
        for k, v in kw.items():
            if v is not None:
                self.x(f"UPDATE boards SET {k}=? WHERE board_key=?", v, key)

    def add_edge(self, src, dst, kind):
        self.x("INSERT OR IGNORE INTO edges VALUES(?,?,?)", str(src), str(dst), kind)

    def add_link(self, link):
        if link:
            self.x("INSERT OR IGNORE INTO links(link) VALUES(?)", link)


def ingest_items(db, items, source, src_pin=None, edge_kind=None):
    """Insert bait-looking pins from search/feed items. Returns number of new pins."""
    new = 0
    for it in items:
        if not it.get("id") or it.get("type", "pin") != "pin":
            continue
        if not PIN_ID_RE.fullmatch(str(it["id"])):   # skip promoted/encoded items like "e1_Afq..."
            continue
        title = txt(it.get("title") or it.get("grid_title"))
        desc = txt(it.get("description"), 500)
        board = txt((it.get("board") or {}).get("name"))
        if looks_bait(title, desc, it.get("link"), board):
            if db.add_pin(it["id"], source):
                new += 1
            if src_pin:
                db.add_edge(src_pin, it["id"], edge_kind)
    db.commit()
    return new


# ----------------------------------------------------------------- steps
def step_fetch(db, api, ids=None):
    rows = ids or [r["pin_id"] for r in db.q("SELECT pin_id FROM pins WHERE status='new'")]
    skipped = [p for p in rows if not PIN_ID_RE.fullmatch(p)]
    rows = [p for p in rows if PIN_ID_RE.fullmatch(p)]
    if skipped:
        db.x(f"UPDATE pins SET status='invalid' WHERE pin_id IN ({','.join('?' * len(skipped))})", *skipped)
        db.commit()
        log(f"[fetch] skipped {len(skipped)} non-numeric IDs (promoted/encoded items); "
            f"run `clean` to delete them")
    log(f"[fetch] {len(rows)} pins")
    for n, pid in enumerate(rows, 1):
        try:
            d = api.call("PinResource", {"id": pid, "field_set_key": "detailed"},
                         f"/pin/{pid}/").get("data") or {}
            if not d:
                raise NotFound("empty")
        except NotFound:
            db.x("UPDATE pins SET status='removed', fetched_at=? WHERE pin_id=?", now(), pid)
            log(f"  {n}/{len(rows)} {pid}: removed / not found"); db.commit(); continue
        except Exception as e:
            db.x("UPDATE pins SET status='error', error=?, fetched_at=? WHERE pin_id=?",
                 f"{type(e).__name__}: {e}", now(), pid)
            log(f"  {n}/{len(rows)} {pid}: error {e}"); db.commit(); continue
        board = d.get("board") or {}
        link = d.get("link") or d.get("tracked_link") or ""
        title = txt(d.get("title") or d.get("grid_title"))
        desc = txt(d.get("description") or d.get("closeup_unified_description"), 500)
        bname = txt(board.get("name"), 100)
        pinner = uname(d.get("pinner"))
        db.x("""UPDATE pins SET status='ok', error=NULL, fetched_at=?, created_at=?, pinner=?,
                native_creator=?, is_repin=?, board_id=?, board_name=?, title=?, description=?,
                repin_count=?, link=?, image_signature=?, shard=?, bait=?, raw=? WHERE pin_id=?""",
             now(), iso(d.get("created_at")), pinner, uname(d.get("native_creator")),
             None if d.get("is_repin") is None else int(bool(d.get("is_repin"))),
             board.get("id"), bname, title, desc, d.get("repin_count"), link,
             d.get("image_signature"), int(pid) >> 46, int(looks_bait(title, desc, link, bname)),
             json.dumps(d)[:200000], pid)
        db.add_user(pinner)
        db.add_link(link)
        if board.get("id"):
            db.add_board(board.get("url") or board["id"], board_id=board["id"], url=board.get("url"),
                         owner=pinner, name=bname, created_at=iso(board.get("created_at")))
        log(f"  {n}/{len(rows)} {pid}: {pinner} | {title[:45]!r} | {link[:50]}")
        db.commit()


def step_resolve(db, force=False):
    rows = db.q("SELECT link FROM links" + ("" if force else " WHERE resolved_at IS NULL"))
    log(f"[resolve] {len(rows)} links")
    for r in rows:
        link = r["link"]
        chain, final, err = unwrap(link)
        host, path, key, kind = classify(link, chain, final)
        db.x("""UPDATE links SET resolved_at=?, final_url=?, final_domain=?, path=?, key=?, chain=?,
                kind=?, error=? WHERE link=?""",
             now(), final, host, path, key, json.dumps(chain), kind, err, link)
        log(f"  {link[:45]:<45} -> {host} {path[:20]} key={key[:8]} {('['+err+']') if err else ''}")
        db.commit()


def step_profiles(db, api, force=False):
    rows = db.q("SELECT username FROM users" + ("" if force else " WHERE status='new'"))
    log(f"[profiles] {len(rows)} users")
    for r in rows:
        u = r["username"]
        try:
            d = api.call("UserResource", {"username": u, "field_set_key": "profile"},
                         f"/{u}/").get("data") or {}
            if not d:
                raise NotFound("empty")
            db.x("""UPDATE users SET status='ok', fetched_at=?, error=NULL, full_name=?, created_at=?,
                    pin_count=?, board_count=?, follower_count=?, following_count=?, website_url=?,
                    about=?, raw=? WHERE username=?""",
                 now(), txt(d.get("full_name"), 80), iso(d.get("created_at")), d.get("pin_count"),
                 d.get("board_count"), d.get("follower_count"), d.get("following_count"),
                 d.get("website_url"), txt(d.get("about"), 300), json.dumps(d)[:100000], u)
            log(f"  {u}: created {iso(d.get('created_at'))} pins={d.get('pin_count')} "
                f"boards={d.get('board_count')} followers={d.get('follower_count')}")
        except NotFound:
            db.x("UPDATE users SET status='removed', fetched_at=? WHERE username=?", now(), u)
            log(f"  {u}: removed / not found")
        except Exception as e:
            db.x("UPDATE users SET status='error', error=?, fetched_at=? WHERE username=?",
                 str(e), now(), u)
            log(f"  {u}: error {e}")
        db.commit()


def step_search(db, api, queries, max_pages):
    log(f"[search] {len(queries)} queries x up to {max_pages} pages")
    for q in queries:
        page, total_new = 0, 0
        try:
            bookmark = None
            for page in range(1, max_pages + 1):
                opts = {"query": q, "scope": "pins", "page_size": 25, "rs": "typed"}
                if bookmark:
                    opts["bookmarks"] = [bookmark]
                resp = api.call("BaseSearchResource", opts,
                                f"/search/pins/?q={urllib.parse.quote(q)}")
                data = resp.get("data") or {}
                results = (data.get("results") if isinstance(data, dict) else data) or []
                new = ingest_items(db, results, f"search:{q}")
                total_new += new
                db.x("INSERT INTO searches VALUES(?,?,?,?,?)", q, page, now(), len(results), new)
                db.commit()
                bookmark = resp.get("bookmark")
                if not bookmark or bookmark == "-end-" or not results:
                    break
        except Exception as e:
            log(f"  {q!r}: error {e}")
        log(f"  {q!r}: {page} pages, {total_new} new candidate pins")


def step_expand(db, api, max_related, max_pages):
    # 1. related pins of bait pins that have a link
    rows = db.q("""SELECT pin_id FROM pins WHERE status='ok' AND bait=1 AND link<>'' AND expanded=0
                   ORDER BY first_seen LIMIT ?""", max_related)
    log(f"[expand] related pins for {len(rows)} bait pins")
    for r in rows:
        pid, new = r["pin_id"], 0
        for res, opts in [
            ("RelatedModulesResource", {"pin_id": pid, "context_pin_ids": [], "search_query": "",
                                        "source": "deep_linking", "top_level_source": "deep_linking",
                                        "page_size": 25}),
            ("RelatedPinFeedResource", {"pin": pid, "page_size": 25, "add_vase": True}),
        ]:
            try:
                items = list(api.paged(res, opts, f"/pin/{pid}/", max_pages=2))
                if items:
                    new = ingest_items(db, items, f"related:{pid}", pid, "related"); break
            except Exception:
                continue
        db.x("UPDATE pins SET expanded=1 WHERE pin_id=?", pid); db.commit()
        log(f"  {pid}: {new} new")

    # 2. boards + created pins of every poster of a bait pin
    users = db.q("""SELECT DISTINCT u.username FROM users u JOIN pins p ON p.pinner=u.username
                    WHERE p.bait=1 AND u.crawled=0 AND u.status<>'removed'""")
    log(f"[expand] crawling {len(users)} poster accounts")
    for r in users:
        u, new = r["username"], 0
        try:
            boards = list(api.paged("BoardsResource", {"username": u, "page_size": 50,
                          "privacy_filter": "all", "sort": "last_pinned_to",
                          "field_set_key": "profile_grid_item"}, f"/{u}/", max_pages=2))
            for b in boards:
                if not b.get("id"):
                    continue
                db.add_board(b.get("url") or b["id"], board_id=b["id"], url=b.get("url"), owner=u,
                             name=txt(b.get("name"), 100), pin_count=b.get("pin_count"),
                             created_at=iso(b.get("created_at")))
                try:
                    items = api.paged("BoardFeedResource", {"board_id": b["id"], "board_url": b.get("url"),
                                      "page_size": 25, "field_set_key": "react_grid_pin"},
                                      b.get("url") or "/", max_pages=max_pages)
                    new += ingest_items(db, items, f"board:{b.get('url')}")
                except Exception as e:
                    log(f"    board {b.get('url')}: {e}")
                db.x("UPDATE boards SET crawled=1 WHERE board_key=?", b.get("url") or b["id"])
            try:
                items = api.paged("UserActivityPinsResource", {"username": u, "page_size": 25,
                                  "field_set_key": "grid_item"}, f"/{u}/_created/", max_pages=max_pages)
                new += ingest_items(db, items, f"created_by:{u}")
            except Exception:
                pass
            db.x("UPDATE users SET crawled=1 WHERE username=?", u)
        except Exception as e:
            log(f"  {u}: error {e}")
        db.commit()
        log(f"  {u}: {new} new")

    # 3. boards added by hand (e.g. thibaultlabs/watch-now-10m+monthly-views)
    boards = db.q("SELECT * FROM boards WHERE crawled=0 AND owner IS NOT NULL AND url IS NOT NULL")
    log(f"[expand] crawling {len(boards)} queued boards")
    for b in boards:
        new, bid = 0, b["board_id"]
        try:
            if not bid:
                parts = [p for p in b["url"].split("/") if p]
                d = api.call("BoardResource", {"username": parts[0], "slug": parts[1],
                             "field_set_key": "detailed"}, b["url"]).get("data") or {}
                bid = d.get("id")
                db.add_board(b["board_key"], board_id=bid, name=txt(d.get("name"), 100),
                             pin_count=d.get("pin_count"), created_at=iso(d.get("created_at")))
            if bid:
                items = api.paged("BoardFeedResource", {"board_id": bid, "board_url": b["url"],
                                  "page_size": 25, "field_set_key": "react_grid_pin"},
                                  b["url"], max_pages=max_pages)
                new = ingest_items(db, items, f"board:{b['url']}")
        except Exception as e:
            db.x("UPDATE boards SET error=? WHERE board_key=?", str(e), b["board_key"])
            log(f"  {b['url']}: error {e}")
        db.x("UPDATE boards SET crawled=1 WHERE board_key=?", b["board_key"]); db.commit()
        log(f"  {b['url']}: {new} new")


# ----------------------------------------------------------------- archive
def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gallery_dl_cmd():
    exe = shutil.which("gallery-dl")
    if exe:
        return [exe]
    if importlib.util.find_spec("gallery_dl"):
        return [sys.executable, "-m", "gallery_dl"]
    return None


def _run_gallery_dl(url, dest, archive_file, timeout=900):
    """Download media + metadata with gallery-dl. Returns (ok, log_text)."""
    cmd = _gallery_dl_cmd()
    if not cmd:
        return False, "gallery-dl not installed"
    os.makedirs(dest, exist_ok=True)
    args = cmd + ["--write-metadata", "--write-info-json", "-D", dest,
                  "--download-archive", archive_file, url]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"gallery-dl timed out after {timeout}s"
    except Exception as e:
        return False, f"gallery-dl failed: {e}"


def _media_urls(raw):
    """Fallback: image/video URLs straight from the pin JSON (all hosted on pinimg.com)."""
    urls = []
    def best_image(images):
        if not isinstance(images, dict):
            return None
        for k in ("orig", "originals", "1200x", "736x", "564x"):
            if isinstance(images.get(k), dict) and images[k].get("url"):
                return images[k]["url"]
        return None
    u = best_image(raw.get("images"))
    if u:
        urls.append(u)
    for slot in ((raw.get("carousel_data") or {}).get("carousel_slots") or []):
        u = best_image(slot.get("images"))
        if u:
            urls.append(u)
    vl = ((raw.get("videos") or {}).get("video_list") or {})
    mp4 = [v.get("url") for k, v in vl.items() if isinstance(v, dict) and str(v.get("url", "")).endswith(".mp4")]
    if mp4:
        urls.append(sorted(mp4, key=len)[-1])
    for page in (((raw.get("story_pin_data") or {}).get("pages")) or []):
        for b in page.get("blocks") or []:
            u = best_image((b.get("image") or {}).get("images"))
            if u:
                urls.append(u)
    return list(dict.fromkeys(urls))


def _download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)


def _wayback(url):
    try:
        req = urllib.request.Request("https://web.archive.org/save/" + url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            loc = r.headers.get("Content-Location") or r.geturl()
            return {"requested": url, "status": r.status,
                    "snapshot": loc if loc.startswith("http") else "https://web.archive.org" + loc}
    except Exception as e:
        return {"requested": url, "error": f"{type(e).__name__}: {e}"}


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def step_archive(db, root="archive", force=False, html=True, media=True, accounts=False,
                 wayback=False, only=None):
    os.makedirs(root, exist_ok=True)
    gdl_archive = os.path.join(root, "gallery-dl-archive.sqlite3")
    if media and not _gallery_dl_cmd():
        log("[archive] gallery-dl not found - using built-in image download fallback "
            "(install with: pip install gallery-dl)")
    where = "p.status='ok' AND p.bait=1" + ("" if force else " AND p.archived_at IS NULL")
    sql = f"""SELECT p.*, l.final_url, l.final_domain, l.key, l.kind, l.chain, l.error AS link_error,
              l.resolved_at FROM pins p LEFT JOIN links l ON l.link=p.link WHERE {where}"""
    rows = [dict(r) for r in db.q(sql)]
    if only:
        rows = [r for r in rows if r["pin_id"] in set(only)]
    log(f"[archive] {len(rows)} pins -> {root}/pins/")
    for n, r in enumerate(rows, 1):
        pid = r["pin_id"]
        d = os.path.join(root, "pins", pid)
        os.makedirs(d, exist_ok=True)
        captured = now()
        raw = json.loads(r["raw"]) if r["raw"] else {}
        notes = []

        _write_json(os.path.join(d, "pin.json"), raw)
        chain = json.loads(r["chain"]) if r["chain"] else []
        _write_json(os.path.join(d, "redirect_chain.json"), {
            "link": r["link"], "chain": chain, "final_url": r["final_url"],
            "final_domain": r["final_domain"], "smartlink_key": r["key"], "kind": r["kind"],
            "error": r["link_error"], "resolved_at": r["resolved_at"],
            "note": "final destination was never requested; google.com/url decoded offline"})
        prof = db.q("SELECT * FROM users WHERE username=?", r["pinner"] or "")
        prof = dict(prof[0]) if prof else {}
        _write_json(os.path.join(d, "poster_profile.json"),
                    json.loads(prof["raw"]) if prof.get("raw") else prof)
        boards = [dict(b) for b in db.q("SELECT * FROM boards WHERE owner=?", r["pinner"] or "")]
        _write_json(os.path.join(d, "poster_boards.json"), boards)

        if html:
            try:
                _download(f"https://www.pinterest.com/pin/{pid}/", os.path.join(d, "page.html"))
            except Exception as e:
                notes.append(f"page.html failed: {e}")

        if media:
            ok, out = _run_gallery_dl(f"https://www.pinterest.com/pin/{pid}/",
                                      os.path.join(d, "media"), gdl_archive)
            with open(os.path.join(d, "gallery-dl.log"), "w", encoding="utf-8") as f:
                f.write(out)
            got = os.path.isdir(os.path.join(d, "media")) and any(
                not x.endswith(".json") for x in os.listdir(os.path.join(d, "media")))
            if not got:
                notes.append("gallery-dl: " + ("no media saved" if ok else out.strip().splitlines()[-1:][0]
                                               if out.strip() else "failed"))
                os.makedirs(os.path.join(d, "media"), exist_ok=True)
                for i, u in enumerate(_media_urls(raw)):
                    ext = os.path.splitext(urllib.parse.urlparse(u).path)[1] or ".bin"
                    try:
                        _download(u, os.path.join(d, "media", f"fallback_{i}{ext}"))
                    except Exception as e:
                        notes.append(f"fallback download {u} failed: {e}")

        wb = []
        if wayback:
            wb.append(_wayback(f"https://www.pinterest.com/pin/{pid}/"))
            if r["pinner"]:
                wb.append(_wayback(f"https://www.pinterest.com/{r['pinner']}/"))
            _write_json(os.path.join(d, "wayback.json"), wb)

        S = [f"# Pin {pid}", "",
             f"- Archived: {captured}",
             f"- URL: https://www.pinterest.com/pin/{pid}/",
             f"- Created: {r['created_at']}",
             f"- Poster: `{r['pinner']}` (account created {prof.get('created_at')}, "
             f"{prof.get('pin_count')} pins, {prof.get('board_count')} boards, "
             f"{prof.get('follower_count')} followers, status {prof.get('status')})",
             f"- Native creator: `{r['native_creator']}`  repin: {r['is_repin']}",
             f"- Board: `{r['board_name']}`",
             f"- Title: `{r['title']}`",
             f"- Description: `{r['description']}`",
             f"- Repins at capture: {r['repin_count']}",
             f"- Image signature: {r['image_signature']}",
             f"- Discovered via: {r['source']} (first seen {r['first_seen']})", "",
             "## Outbound link", "", f"- Chain type: {r['kind']}"]
        S += [f"  {i}. `{c}`" for i, c in enumerate(chain, 1)]
        S += [f"- Final domain: `{r['final_domain']}`", f"- Smartlink key: `{r['key']}`",
              "- The final destination was never requested by this tool.", ""]
        for w in wb:
            S.append(f"- Wayback: {w.get('snapshot') or w.get('error')}  ({w['requested']})")
        if notes:
            S += ["", "## Notes", ""] + [f"- {x}" for x in notes]

        manifest = {}
        with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(S) + "\n")
        for base, _, files in os.walk(d):
            for fn in files:
                if fn == "manifest.json":
                    continue
                p = os.path.join(base, fn)
                manifest[os.path.relpath(p, d)] = {"sha256": _sha256(p), "bytes": os.path.getsize(p)}
        _write_json(os.path.join(d, "manifest.json"), {"pin_id": pid, "archived_at": captured,
                                                       "files": manifest})
        db.x("UPDATE pins SET archived_at=?, archive_dir=? WHERE pin_id=?", captured, d, pid)
        db.commit()
        log(f"  {n}/{len(rows)} {pid}: {len(manifest)} files" + (f"  ({'; '.join(notes)})" if notes else ""))

    if accounts:
        users = db.q("""SELECT DISTINCT u.* FROM users u JOIN pins p ON p.pinner=u.username
                        WHERE p.bait=1 AND u.status='ok'""" + ("" if force else " AND u.archived_at IS NULL"))
        log(f"[archive] {len(users)} poster accounts -> {root}/accounts/ (this can be large)")
        for u in users:
            name = u["username"]
            d = os.path.join(root, "accounts", name)
            os.makedirs(d, exist_ok=True)
            _write_json(os.path.join(d, "profile.json"), json.loads(u["raw"]) if u["raw"] else dict(u))
            _write_json(os.path.join(d, "boards.json"),
                        [dict(b) for b in db.q("SELECT * FROM boards WHERE owner=?", name)])
            ok, out = _run_gallery_dl(f"https://www.pinterest.com/{name}/", os.path.join(d, "media"),
                                      gdl_archive, timeout=3600)
            with open(os.path.join(d, "gallery-dl.log"), "w", encoding="utf-8") as f:
                f.write(out)
            if wayback:
                _write_json(os.path.join(d, "wayback.json"), [_wayback(f"https://www.pinterest.com/{name}/")])
            db.x("UPDATE users SET archived_at=? WHERE username=?", now(), name); db.commit()
            log(f"  {name}: {'ok' if ok else 'gallery-dl reported problems (see gallery-dl.log)'}")

    rows = [dict(r) for r in db.q("""SELECT p.pin_id, p.created_at, p.pinner, p.title, p.status, p.archive_dir,
                                     p.archived_at, l.final_domain, l.key FROM pins p
                                     LEFT JOIN links l ON l.link=p.link
                                     WHERE p.archived_at IS NOT NULL ORDER BY p.created_at""")]
    I = ["# Archive index", "", f"Updated {now()} — {len(rows)} pins", "",
         "| created | pin | poster | destination | key | still live? | folder |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        rel = os.path.relpath(r["archive_dir"], root) if r["archive_dir"] else ""
        I.append(f"| {(r['created_at'] or '')[:10]} | {r['pin_id']} | {r['pinner']} | {r['final_domain'] or ''} | "
                 f"{(r['key'] or '')[:10]} | {'no' if r['status'] == 'removed' else 'yes'} | [{rel}]({rel}/summary.md) |")
    with open(os.path.join(root, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(I) + "\n")
    log(f"[archive] index: {os.path.join(root, 'index.md')}")


# ----------------------------------------------------------------- report
def batches(usernames):
    by = collections.defaultdict(set)
    for u in usernames:
        m = re.match(r"^([A-Za-z_.]+?)[_.-]?(\d{4,})$", u or "")
        if m:
            by[m.group(2)].add(u)
    return {k: sorted(v) for k, v in by.items() if len(v) > 1}


def report(db, out_md="report.md", out_csv="pins.csv", since=None):
    rows = db.q("""SELECT p.*, l.final_url, l.final_domain, l.path, l.key, l.kind, l.error AS link_error,
                   u.full_name, u.created_at AS acct_created, u.pin_count AS acct_pins,
                   u.board_count AS acct_boards, u.follower_count AS acct_followers,
                   u.status AS acct_status
                   FROM pins p LEFT JOIN links l ON l.link=p.link
                   LEFT JOIN users u ON u.username=p.pinner""")
    rows = [dict(r) for r in rows]
    for r in rows:
        a, b = parse_iso(r.get("acct_created") or ""), parse_iso(r.get("created_at") or "")
        r["warmup_days"] = round((b - a).total_seconds() / 86400, 1) if a and b else None

    cols = ["pin_id", "status", "source", "first_seen", "created_at", "pinner", "full_name",
            "native_creator", "is_repin", "board_name", "title", "description", "repin_count", "link",
            "final_url", "final_domain", "key", "kind", "link_error", "acct_created", "acct_pins",
            "acct_boards", "acct_followers", "acct_status", "warmup_days", "shard", "image_signature"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    ok = [r for r in rows if r["status"] == "ok"]
    bait = [r for r in ok if r["bait"]]
    linked = [r for r in bait if r["link"]]
    L = []
    P = L.append
    P(f"# Pinterest smartlink-spam report\n\nGenerated {now()}\n")
    st = collections.Counter(r["status"] for r in rows)
    P("## Totals\n")
    P(f"- Pins in database: **{len(rows)}** ({', '.join(f'{k}: {v}' for k, v in st.items())})")
    P(f"- Bait pins: **{len(bait)}**, with outbound link: **{len(linked)}**")
    P(f"- Distinct posters of bait pins: **{len({r['pinner'] for r in bait if r['pinner']})}**")
    P(f"- Distinct outbound links: **{len({r['link'] for r in linked})}**, "
      f"final domains: **{len({r['final_domain'] for r in linked if r['final_domain']})}**, "
      f"smartlink keys: **{len({r['key'] for r in linked if r['key']})}**")
    if since:
        P(f"- New pins found this run: **{sum(1 for r in rows if (r['first_seen'] or '') >= since)}**")

    P("\n## Link chain types\n\n| count | chain |\n|---:|---|")
    for k, n in collections.Counter(r["kind"] or "(unresolved)" for r in linked).most_common():
        P(f"| {n} | {k} |")

    P("\n## Final domains\n\n| domain | pins | keys | posters | first pin | last pin |\n|---|---:|---:|---:|---|---|")
    by_dom = collections.defaultdict(list)
    for r in linked:
        by_dom[r["final_domain"] or "(unresolved)"].append(r)
    for dom, rs in sorted(by_dom.items(), key=lambda kv: -len(kv[1])):
        dates = sorted(r["created_at"] for r in rs if r["created_at"])
        P(f"| {dom} | {len(rs)} | {len({r['key'] for r in rs if r['key']})} | "
          f"{len({r['pinner'] for r in rs})} | {dates[0][:10] if dates else ''} | "
          f"{dates[-1][:10] if dates else ''} |")

    P("\n## Domain timeline (bait pins per ISO week)\n")
    weeks = collections.defaultdict(collections.Counter)
    for r in linked:
        d = parse_iso(r["created_at"] or "")
        if d:
            y, w, _ = d.isocalendar(); weeks[f"{y}-W{w:02d}"][r["final_domain"] or "?"] += 1
    for wk in sorted(weeks):
        P(f"- {wk}: " + ", ".join(f"{d} ×{n}" for d, n in weeks[wk].most_common()))

    P("\n## Title variants\n\n| count | title |\n|---:|---|")
    for k, n in collections.Counter(r["title"] for r in bait).most_common(25):
        P(f"| {n} | `{k}` |")

    P("\n## Account batches (usernames sharing a numeric suffix)\n")
    bt = batches({r["pinner"] for r in bait if r["pinner"]})
    if bt:
        for suf, us in sorted(bt.items(), key=lambda kv: -len(kv[1])):
            doms = {r["final_domain"] for r in linked if r["pinner"] in us and r["final_domain"]}
            P(f"- **{suf}**: {', '.join(us)}  → {', '.join(sorted(doms)) or 'no links'}")
    else:
        P("- none found yet")

    P("\n## Warm-up (days from account creation to bait pin)\n")
    gaps = sorted(r["warmup_days"] for r in linked if r["warmup_days"] is not None)
    if gaps:
        hist = collections.Counter(int(g // 7) for g in gaps)
        P(f"- n={len(gaps)}, median **{gaps[len(gaps)//2]}** days, min {gaps[0]}, max {gaps[-1]}")
        for wk in sorted(hist):
            P(f"  - {wk*7:>3}–{wk*7+6:<3} days: {'█' * hist[wk]} {hist[wk]}")
    else:
        P("- no data yet")

    P("\n## Poster accounts\n\n| account | created | pins | boards | followers | status | bait pins | domains |"
      "\n|---|---|---:|---:|---:|---|---:|---|")
    acc = collections.defaultdict(list)
    for r in bait:
        acc[r["pinner"]].append(r)
    for u, rs in sorted(acc.items(), key=lambda kv: kv[1][0]["acct_created"] or ""):
        r0 = rs[0]
        P(f"| {u} | {(r0['acct_created'] or '')[:10]} | {r0['acct_pins']} | {r0['acct_boards']} | "
          f"{r0['acct_followers']} | {r0['acct_status']} | {len(rs)} | "
          f"{', '.join(sorted({r['final_domain'] for r in rs if r['final_domain']}))} |")

    P("\n## Bait pins with links (newest first)\n\n| created | pin | poster | warm-up | repins | destination | key |"
      "\n|---|---|---|---:|---:|---|---|")
    for r in sorted(linked, key=lambda r: r["created_at"] or "", reverse=True)[:300]:
        P(f"| {(r['created_at'] or '')[:16]} | [{r['pin_id']}](https://www.pinterest.com/pin/{r['pin_id']}/) | "
          f"{r['pinner']} | {r['warmup_days'] if r['warmup_days'] is not None else ''} | {r['repin_count']} | "
          f"{r['final_domain'] or ''}{(r['path'] or '')[:25]} | {(r['key'] or '')[:10]} |")

    nolink = [r for r in bait if not r["link"]]
    if nolink:
        P("\n## Bait-looking pins with no link (warming up, stripped, or repins)\n")
        for r in nolink:
            P(f"- {r['pin_id']} — {r['pinner']} — board `{r['board_name']}` — title `{r['title'][:50]}` "
              f"— repin={r['is_repin']}")

    removed = [r for r in rows if r["status"] == "removed"]
    if removed:
        P(f"\n## Removed / not found ({len(removed)})\n")
        P(", ".join(r["pin_id"] for r in removed))
    errs = [r for r in rows if r["status"] == "error"]
    if errs:
        P(f"\n## Fetch errors ({len(errs)}) — usually rate limiting; re-run `fetch --retry-errors`\n")
        P(", ".join(r["pin_id"] for r in errs))

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    log(f"[report] wrote {out_md} and {out_csv}")


# ----------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="pinhunt.db")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between Pinterest requests")
    sp = ap.add_subparsers(dest="cmd", required=True)

    a = sp.add_parser("add"); a.add_argument("items", nargs="+", help="pin/board URLs, IDs, or files")
    f = sp.add_parser("fetch"); f.add_argument("--retry-errors", action="store_true")
    r = sp.add_parser("resolve"); r.add_argument("--force", action="store_true")
    pr = sp.add_parser("profiles"); pr.add_argument("--force", action="store_true")
    s = sp.add_parser("search"); s.add_argument("--query", action="append", default=[])
    s.add_argument("--only", action="store_true", help="use only --query, not the defaults")
    s.add_argument("--max-pages", type=int, default=6)
    e = sp.add_parser("expand"); e.add_argument("--max-related", type=int, default=40)
    e.add_argument("--max-pages", type=int, default=6)
    rc = sp.add_parser("recheck"); rc.add_argument("--days", type=float, default=3)
    ar = sp.add_parser("archive", help="save evidence folders for bait pins")
    ar.add_argument("--dir", default="archive")
    ar.add_argument("--force", action="store_true", help="re-archive pins already archived")
    ar.add_argument("--no-html", action="store_true")
    ar.add_argument("--no-media", action="store_true")
    ar.add_argument("--accounts", action="store_true", help="also download each poster's whole account")
    ar.add_argument("--wayback", action="store_true", help="request Wayback Machine snapshots")
    ar.add_argument("--pin", action="append", default=[], help="only these pin IDs")
    sp.add_parser("report")
    sp.add_parser("status")
    sp.add_parser("clean", help="delete non-numeric junk IDs (e1_...) from the database")
    ru = sp.add_parser("run", help="default full procedure")
    ru.add_argument("--no-search", action="store_true")
    ru.add_argument("--no-expand", action="store_true")
    ru.add_argument("--rounds", type=int, default=2, help="expand rounds")
    ru.add_argument("--max-pages", type=int, default=6)
    ru.add_argument("--max-related", type=int, default=40)
    ru.add_argument("--query", action="append", default=[])
    ru.add_argument("--no-discover", action="store_true",
                    help="don't look for new pins (same as --no-search --no-expand)")
    ru.add_argument("--no-archive", action="store_true")
    ru.add_argument("--archive-accounts", action="store_true")
    ru.add_argument("--wayback", action="store_true")
    args = ap.parse_args()

    db, api = DB(args.db), Api(args.delay)

    if args.cmd == "add":
        pins, boards = parse_inputs(args.items)
        newp = sum(db.add_pin(p, "manual") for p in pins)
        for owner, slug in boards:
            db.add_board(f"/{owner}/{slug}/", url=f"/{owner}/{slug}/", owner=owner)
        db.commit()
        log(f"added {newp} new pins ({len(pins)} given), {len(boards)} boards queued for crawling")
    elif args.cmd == "fetch":
        if args.retry_errors:
            db.x("UPDATE pins SET status='new' WHERE status='error'"); db.commit()
        step_fetch(db, api)
    elif args.cmd == "resolve":
        step_resolve(db, args.force)
    elif args.cmd == "profiles":
        step_profiles(db, api, args.force)
    elif args.cmd == "search":
        step_search(db, api, (args.query if args.only else DEFAULT_QUERIES + args.query), args.max_pages)
    elif args.cmd == "expand":
        step_expand(db, api, args.max_related, args.max_pages)
    elif args.cmd == "recheck":
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()
        ids = [r["pin_id"] for r in db.q("SELECT pin_id FROM pins WHERE status IN ('ok','error') "
                                          "AND (fetched_at IS NULL OR fetched_at < ?)", cutoff)]
        step_fetch(db, api, ids); step_resolve(db); step_profiles(db, api, force=True); report(db)
        step_archive(db)  # archives anything new; index shows which archived pins are now removed
    elif args.cmd == "archive":
        step_archive(db, args.dir, args.force, not args.no_html, not args.no_media,
                     args.accounts, args.wayback, args.pin or None)
    elif args.cmd == "report":
        report(db)
    elif args.cmd == "status":
        for t in ("pins", "links", "users", "boards", "edges", "searches"):
            log(f"{t:<9} {db.q(f'SELECT COUNT(*) n FROM {t}')[0]['n']}")
        for r in db.q("SELECT status, COUNT(*) n FROM pins GROUP BY status"):
            log(f"  pins {r['status']}: {r['n']}")
        log(f"  bait pins with link: {db.q('SELECT COUNT(*) n FROM pins WHERE bait=1 AND link<>\"\"')[0]['n']}")
    elif args.cmd == "clean":
        bad = [r["pin_id"] for r in db.q("SELECT pin_id FROM pins") if not PIN_ID_RE.fullmatch(r["pin_id"])]
        for i in range(0, len(bad), 500):
            chunk = bad[i:i + 500]; ph = ",".join("?" * len(chunk))
            db.x(f"DELETE FROM pins WHERE pin_id IN ({ph})", *chunk)
            db.x(f"DELETE FROM edges WHERE dst IN ({ph}) OR src IN ({ph})", *chunk, *chunk)
        db.commit()
        log(f"deleted {len(bad)} non-numeric pin entries (and their edges)")
    elif args.cmd == "run":
        if args.no_discover:
            args.no_search = args.no_expand = True
        start = now()
        step_fetch(db, api); step_resolve(db); step_profiles(db, api)
        if not args.no_search:
            step_search(db, api, DEFAULT_QUERIES + args.query, args.max_pages)
            step_fetch(db, api)
        if not args.no_expand:
            for i in range(args.rounds):
                log(f"===== expand round {i+1}/{args.rounds} =====")
                before = db.q("SELECT COUNT(*) n FROM pins")[0]["n"]
                step_expand(db, api, args.max_related, args.max_pages)
                step_fetch(db, api)
                if db.q("SELECT COUNT(*) n FROM pins")[0]["n"] == before:
                    log("  nothing new; stopping expansion"); break
        step_resolve(db); step_profiles(db, api)
        report(db, since=start)
        if not args.no_archive:
            step_archive(db, accounts=args.archive_accounts, wayback=args.wayback)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted - progress so far is saved in the database; re-run to continue")
