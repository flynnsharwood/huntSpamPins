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
import random, sqlite3, subprocess, sys, time, unicodedata
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


URL_PARAMS = ("url", "q", "u", "link", "target", "dest", "destination", "redirect", "redirect_url",
              "redir", "goto", "to", "r", "out", "next", "continue")


def decode_embedded(url):
    """If `url` carries another http(s) URL in a query parameter (google.com/url?q=,
    podnova /go/?url=, ...), return that URL. Pure string work - nothing is requested.
    Unencoded embedded URLs (`?url=https://x/?a=1&b=2`) keep the rest of the query string."""
    p = urllib.parse.urlparse(url)
    if not p.query:
        return None
    for name in URL_PARAMS:
        m = re.search(r"(?:^|&)" + re.escape(name) + r"=([^&]*)", p.query, re.I)
        if not m:
            continue
        raw = m.group(1)
        google = p.netloc.lower() in ("google.com", "www.google.com")
        if re.match(r"https?://", raw, re.I) and not google:   # not encoded: take everything after it
            val = p.query[m.start(1):]
        else:
            val = urllib.parse.unquote_plus(raw)
        if re.match(r"https?://[^/\s]+", val, re.I) and val != url:
            return val
    return None


def unwrap(link):
    """Follow shorteners / decode google.com/url and other embedded-URL redirects offline.
    Never requests the final destination: only hosts in REDIRECTORS are ever contacted."""
    chain, url, err = [link], link, None
    for _ in range(10):
        p = urllib.parse.urlparse(url)
        host = p.netloc.lower()
        nxt = decode_embedded(url) if host not in REDIRECTORS else None
        if nxt:
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
    elif host.endswith("auctionr.org") and re.match(r"/\d+/[0-9a-f]{16,}", fp.path):
        kind = "auctionr /<n>/<id>"
    elif via_google and lhost not in ("google.com", "www.google.com") and "tinyurl" not in lhost:
        kind = f"redirect via {lhost} > {host}"
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
        # original creators of repins are users too (older DBs only stored the repinner)
        self.c.execute("""INSERT OR IGNORE INTO users(username, first_seen)
                          SELECT DISTINCT native_creator, datetime('now') FROM pins
                          WHERE native_creator IS NOT NULL AND native_creator<>''""")
        for table, col, typ in (("pins", "archived_at", "TEXT"), ("pins", "archive_dir", "TEXT"),
                                ("users", "archived_at", "TEXT"),
                                # expand results: when, related items returned (NULL = request failed),
                                # new bait pins added (0 = expanded but found nothing new)
                                ("pins", "expanded_at", "TEXT"), ("pins", "expand_seen", "INTEGER"),
                                ("pins", "expand_new", "INTEGER")):
            cols = {r[1] for r in self.c.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
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
        db.add_user(uname(d.get("native_creator")))   # original creator of a repin = likely operator
        db.add_link(link)
        if board.get("id"):
            db.add_board(board.get("url") or board["id"], board_id=board["id"], url=board.get("url"),
                         owner=pinner, name=bname, created_at=iso(board.get("created_at")))
        log(f"  {n}/{len(rows)} {pid}: {pinner} | {title[:45]!r} | {link[:50]}")
        db.commit()


def step_reclassify(db):
    """Offline: decode embedded URLs at the end of already-resolved chains and recompute kind/key.
    No network requests (a decoded shortener is left for the next normal `resolve`)."""
    rows = db.q("SELECT link, chain FROM links WHERE resolved_at IS NOT NULL")
    changed = 0
    for r in rows:
        chain = json.loads(r["chain"] or "[]") or [r["link"]]
        final = chain[-1].replace("  [decoded offline]", "")
        while True:
            nxt = decode_embedded(final)
            if not nxt or urllib.parse.urlparse(final).netloc.lower() in REDIRECTORS:
                break
            final = nxt; chain.append(nxt + "  [decoded offline]")
        host, path, key, kind = classify(r["link"], chain, final)
        cur = db.q("SELECT final_url, kind FROM links WHERE link=?", r["link"])[0]
        if (cur["final_url"], cur["kind"]) != (final, kind):
            changed += 1
            log(f"  {r['link'][:50]} -> {host}{path[:30]} [{kind}]")
        resolved = urllib.parse.urlparse(final).netloc.lower() not in REDIRECTORS
        db.x("""UPDATE links SET final_url=?, final_domain=?, path=?, key=?, chain=?, kind=?,
                resolved_at=CASE WHEN ? THEN resolved_at ELSE NULL END WHERE link=?""",
             final, host, path, key, json.dumps(chain), kind, int(resolved), r["link"])
    db.commit()
    log(f"[resolve] reclassified {len(rows)} links offline, {changed} changed")


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


def pick(items, n):
    """Random order; if n > 0, only a random n of the items (the rest wait for a later run)."""
    items = list(items)
    random.shuffle(items)
    return items[:n] if n and n > 0 else items


def _picked(n_total, chosen):
    return f"{len(chosen)} random of {n_total}" if len(chosen) < n_total else f"{n_total}"


def step_search(db, api, queries, max_pages, sample=0):
    all_q = list(queries)
    queries = pick(all_q, sample)   # random subset/order so early queries don't always get the fresh rate-limit budget
    log(f"[search] {_picked(len(all_q), queries)} queries x up to {max_pages} pages")
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


def step_expand(db, api, max_related, max_pages, sample=0, max_user_pins=5000):
    # 1. related pins of bait pins that have a link
    all_rows = db.q("""SELECT pin_id FROM pins WHERE status='ok' AND bait=1 AND link<>'' AND expanded=0
                       ORDER BY first_seen""")
    caps = [n for n in (sample, max_related) if n and n > 0]
    rows = pick(all_rows, min(caps) if caps else 0)
    log(f"[expand] related pins for {_picked(len(all_rows), rows)} bait pins")
    for r in rows:
        pid, new, seen = r["pin_id"], 0, None
        for res, opts in [
            ("RelatedModulesResource", {"pin_id": pid, "context_pin_ids": [], "search_query": "",
                                        "source": "deep_linking", "top_level_source": "deep_linking",
                                        "page_size": 25}),
            ("RelatedPinFeedResource", {"pin": pid, "page_size": 25, "add_vase": True}),
        ]:
            try:
                items = list(api.paged(res, opts, f"/pin/{pid}/", max_pages=2))
                seen = len(items)
                if items:
                    new = ingest_items(db, items, f"related:{pid}", pid, "related"); break
            except Exception:
                continue
        db.x("UPDATE pins SET expanded=1, expanded_at=?, expand_seen=?, expand_new=? WHERE pin_id=?",
             now(), seen, new, pid); db.commit()
        log(f"  {pid}: {new} new" + (" (related pins request failed)" if seen is None else
                                     f" of {seen} related" if seen else " (no related pins)"))

    # 2. boards + created pins of every poster of a bait pin
    # accounts that created bait (native_creator, else pinner); savers' huge accounts are skipped
    all_users = db.q("""SELECT DISTINCT u.username FROM users u
                        JOIN pins p ON u.username = COALESCE(NULLIF(p.native_creator, ''), p.pinner)
                        WHERE p.bait=1 AND u.crawled=0 AND u.status<>'removed'
                        AND (? <= 0 OR u.pin_count IS NULL OR u.pin_count <= ?)""", max_user_pins, max_user_pins)
    big = db.q("""SELECT COUNT(DISTINCT u.username) n FROM users u JOIN pins p ON p.pinner=u.username
                  WHERE p.bait=1 AND u.crawled=0 AND ? > 0 AND u.pin_count > ?""", max_user_pins, max_user_pins)[0]["n"]
    if big:
        log(f"[expand] skipping {big} uncrawled accounts with more than {max_user_pins} pins (likely savers)")
    users = pick(all_users, sample)
    log(f"[expand] crawling {_picked(len(all_users), users)} poster accounts")
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
    all_boards = db.q("SELECT * FROM boards WHERE crawled=0 AND owner IS NOT NULL AND url IS NOT NULL")
    boards = pick(all_boards, sample)
    log(f"[expand] crawling {_picked(len(all_boards), boards)} queued boards")
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


# ----------------------------------------------------------------- search queries file
QUERIES_HEADER = ("# pinhunt search queries - one per line. Lines starting with # are ignored.\n"
                  "# Edit freely: delete noisy queries, add your own. `queries --update` appends new\n"
                  "# phrases learned from bait pins in the database.\n")


def _qnorm(q):
    return re.sub(r"\s+", " ", q).strip().casefold()


def load_queries(path):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(QUERIES_HEADER + "\n" + "\n".join(DEFAULT_QUERIES) + "\n")
        log(f"[queries] created {path} with {len(DEFAULT_QUERIES)} default queries")
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    seen, uniq = set(), []
    for q in out:
        if _qnorm(q) not in seen:
            seen.add(_qnorm(q)); uniq.append(q)
    return uniq


def _plain_core(t):
    """Text without emoji / symbols / fancy unicode: only plain words, digits, + ' -."""
    t = unicodedata.normalize("NFKC", t)   # 𝐇𝐎𝐓 -> HOT
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9\s+'’-]", " ", t)).strip(" -")


def derive_queries(db):
    """Candidate search phrases from bait pins: Counter(query -> number of supporting pins)."""
    rows = db.q("SELECT title, board_name, description FROM pins WHERE status='ok' AND bait=1")
    title_counts = collections.Counter((r["title"] or "").strip() for r in rows)
    cnt = collections.Counter()
    for r in rows:
        cands = set()
        t = (r["title"] or "").strip()
        # a title is used if it looks like bait, or is a repeated template (>=2 bait pins share it)
        if (t and not t.lower().startswith("pin on ") and 4 <= len(t) <= 100
                and (BAIT_RE.search(t) or title_counts[t] >= 2)):
            cands.add(_plain_core(t))
        b = (r["board_name"] or "").strip()
        if b and BAIT_RE.search(b) and len(b) <= 80:
            cands.add(_plain_core(b))
        for piece in re.split(r"[.!?\n]+", r["description"] or ""):
            piece = piece.strip(" -–—")
            if BAIT_RE.search(piece) and 3 <= len(piece.split()) <= 12:
                cands.add(_plain_core(piece))
        for c in cands:
            if c and len(c.split()) >= 2:   # Pinterest search returns nothing for emoji queries
                cnt[c] += 1
    return cnt


def _pruned_queries(path):
    """Queries removed by `queries --prune` (kept as '# pruned: <q>' so --update won't re-add them)."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {_qnorm(l.split(":", 1)[1]) for l in f if l.startswith("# pruned")}


def prune_queries(db, path, dry_run=False, min_runs=2):
    """Comment out queries that returned 0 results in every one of >= min_runs runs."""
    stats = {_qnorm(r["query"]): r for r in db.q(
        """SELECT query, COUNT(DISTINCT substr(run_at,1,16)) runs, SUM(results) results
           FROM searches GROUP BY query""")}
    load_queries(path)   # creates the file if missing
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, dropped = [], []
    for line in lines:
        q = line.strip()
        st = stats.get(_qnorm(q)) if q and not q.startswith("#") else None
        if st and st["runs"] >= min_runs and not st["results"]:
            dropped.append(q); out.append(f"# pruned {now()[:10]} (0 results in {st['runs']} runs): {q}")
        else:
            out.append(line)
    for q in dropped:
        log(f"  - {q!r}")
    if not dropped:
        log(f"[queries] nothing to prune (no query with 0 results over >= {min_runs} runs)")
    elif dry_run:
        log(f"[queries] dry run: {len(dropped)} queries would be pruned from {path}")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        log(f"[queries] pruned {len(dropped)} queries from {path} (kept as '# pruned' comments)")
    return dropped


def update_queries(db, path, dry_run=False, min_count=1):
    existing = {_qnorm(q) for q in load_queries(path)} | _pruned_queries(path)
    new = [(q, n) for q, n in derive_queries(db).most_common()
           if n >= min_count and _qnorm(q) not in existing]
    if not new:
        log(f"[queries] no new phrases to add to {path}")
        return []
    for q, n in new:
        log(f"  + {q!r}  ({n} bait pin{'s' if n != 1 else ''})")
    if dry_run:
        log(f"[queries] dry run: {len(new)} phrases would be added to {path}")
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n# auto-added {now()} from bait pins in the database\n")
            f.write("\n".join(q for q, _ in new) + "\n")
        log(f"[queries] added {len(new)} phrases to {path}")
    return [q for q, _ in new]


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
    I = ["# Archive index", "", f"Updated {now()} — {len(rows)} pins", ""]
    T = []
    for r in rows:
        rel = os.path.relpath(r["archive_dir"], root) if r["archive_dir"] else ""
        T.append([(r['created_at'] or '')[:10], r['pin_id'], r['pinner'], r['final_domain'] or '',
                  (r['key'] or '')[:10], 'no' if r['status'] == 'removed' else 'yes', f"[{rel}]({rel}/summary.md)"])
    I.append(md_table(["created", "pin", "poster", "destination", "key", "still live?", "folder"], T))
    with open(os.path.join(root, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(I) + "\n")
    log(f"[archive] index: {os.path.join(root, 'index.md')}")


# ----------------------------------------------------------------- picture board
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def step_pics(root="archive", board=True):
    """Copy archived pin images into <root>/_pics, then run `pixboards` there to rebuild the local HTML board.
    Never passes --upload / --gitPush: the images are stolen photos and must stay private."""
    pics = os.path.join(root, "_pics")
    os.makedirs(pics, exist_ok=True)
    copied = 0
    for base, _, files in os.walk(os.path.join(root, "pins")):
        if os.path.basename(base) != "media":
            continue
        pid = os.path.basename(os.path.dirname(base))
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in IMAGE_EXTS:
                continue
            name = fn if pid in fn else f"{pid}_{fn}"   # fallback_0.jpg etc. would collide between pins
            src, dst = os.path.join(base, fn), os.path.join(pics, name)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue
            shutil.copy2(src, dst); copied += 1
    log(f"[pics] copied {copied} new images to {pics}")
    if not board:
        return
    exe = shutil.which("pixboards")
    if not exe:
        log("[pics] pixboards not found on PATH; skipped building the board"); return
    p = subprocess.run([exe], cwd=pics, capture_output=True, text=True)
    log(f"[pics] pixboards exited {p.returncode}")
    if p.returncode:
        log((p.stdout + p.stderr)[-2000:])


# ----------------------------------------------------------------- markdown tables
def _width(s):
    """Approximate terminal/monospace display width (emoji and CJK = 2, combining marks/VS16/ZWJ = 0)."""
    w = 0
    for i, ch in enumerate(s):
        if unicodedata.combining(ch) or ch in "\u200d\ufe0e\ufe0f" or unicodedata.category(ch) == "Cf":
            continue
        emoji = s[i + 1:i + 2] == "\ufe0f"   # e.g. "▶️": a narrow symbol drawn as a wide emoji
        w += 2 if emoji or unicodedata.east_asian_width(ch) in "WF" else 1
    return w


def md_table(headers, rows, align=""):
    """Markdown table with columns padded to equal width. align: one char per column, 'l' or 'r'."""
    cells = [[str(h) for h in headers]] + [
        ["" if c is None else str(c).replace("|", "\\|").replace("\n", " ") for c in row] for row in rows]
    n = len(headers)
    align = (align + "l" * n)[:n]
    widths = [max(3, *(_width(r[i]) for r in cells)) for i in range(n)]

    def line(r):
        out = []
        for i, c in enumerate(r):
            pad = " " * (widths[i] - _width(c))
            out.append(pad + c if align[i] == "r" else c + pad)
        return "| " + " | ".join(out) + " |"
    sep = "| " + " | ".join("-" * (w - 1) + ":" if a == "r" else "-" * w for w, a in zip(widths, align)) + " |"
    return "\n".join([line(cells[0]), sep] + [line(r) for r in cells[1:]])


# ----------------------------------------------------------------- report
AGED_DAYS = 365      # pinner account this much older than the bait pin, with 1 bait pin -> saver
WAVE_GAP_MIN = 15    # bait pins of one account at most this far apart belong to one wave


def attribute(rows, users):
    """Who is behind each pin. Sets r['saver'] = 1 if the pinner only carried someone else's bait
    (a repin, or an aged account with a single bait pin), r['operator'] = the account that created
    the bait (native_creator, else the pinner unless it is a saver; None = unknown), and
    r['warmup_days'] from the operator's account creation (None for savers' copies)."""
    n_bait = collections.Counter(r["pinner"] for r in rows if r["status"] == "ok" and r["bait"])
    for r in rows:
        nc, pinner = r.get("native_creator") or "", r.get("pinner")
        b = parse_iso(r.get("created_at") or "")
        a = parse_iso((users.get(pinner) or {}).get("created_at") or "")
        aged = bool(a and b and (b - a).days >= AGED_DAYS)
        repin = bool(r.get("is_repin")) or bool(nc and nc != pinner)
        r["saver"] = int(repin or (aged and n_bait[pinner] == 1))
        r["operator"] = nc if nc and nc != pinner else (None if r["saver"] else pinner)
        op = users.get(r["operator"]) or {}
        r["op_created"] = op.get("created_at")
        c = parse_iso(op.get("created_at") or "")
        r["warmup_days"] = round((b - c).total_seconds() / 86400, 1) if c and b and not r["saver"] else None


def link_attrs(r):
    """Shared-infrastructure signals of a pin, used to join accounts into clusters."""
    out = []
    if r.get("key"):
        out.append(("key", r["key"]))
    if (r.get("kind") or "").startswith("auctionr"):
        out.append(("auctionr id", r["path"]))
    if "tinyurl" in urllib.parse.urlparse(r.get("link") or "").netloc.lower():
        out.append(("tinyurl", r["link"]))
    if (r.get("final_domain") or "").endswith("netlify.app"):
        out.append(("netlify host", r["final_domain"]))
    if r.get("image_signature"):
        out.append(("image", r["image_signature"]))
    return out


def clusters(pins):
    """Connected components of operator accounts that share a key / auctionr id / tinyurl /
    netlify host / image. Returns [(accounts, {signal type: number of shared values})]."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    users_of = collections.defaultdict(set)
    for r in pins:
        if not r.get("operator"):
            continue
        a = ("acct", r["operator"]); find(a)
        for at in link_attrs(r):
            parent[find(a)] = find(at); users_of[at].add(r["operator"])
    comps = collections.defaultdict(set)
    for x in list(parent):
        if x[0] == "acct":
            comps[find(x)].add(x[1])
    out = []
    for root, accts in comps.items():
        shared = collections.Counter(at[0] for at, us in users_of.items() if len(us) > 1 and find(at) == root)
        out.append((sorted(accts), dict(shared)))
    return sorted(out, key=lambda c: (-len(c[0]), c[0]))


def waves(pins, gap_min=WAVE_GAP_MIN):
    """Bursts of bait pins per operator: consecutive pins at most gap_min minutes apart.
    Savers' copies are left out (their time is when they saved it, not when it was posted)."""
    by = collections.defaultdict(list)
    for r in pins:
        d = parse_iso(r.get("created_at") or "")
        if r.get("operator") and not r.get("saver") and d:
            by[r["operator"]].append((d, r))
    out = []
    for acct, lst in by.items():
        lst.sort(key=lambda x: x[0])
        cur = [lst[0]]
        for prev, nxt in zip(lst, lst[1:]):
            if (nxt[0] - prev[0]).total_seconds() > gap_min * 60:
                out.append((acct, cur)); cur = []
            cur.append(nxt)
        out.append((acct, cur))
    return sorted(out, key=lambda w: (w[0], w[1][0][0]))


def name_families(accounts, created):
    """Groups of accounts that look batch-made: same numeric suffix, shared name stem
    (e.g. *blue, *janaam, neriah*), or created on the same day. Returns [(signal, label, members)]."""
    fams = {}

    def add(signal, label, members):
        if len(members) > 1:
            fams.setdefault((signal, frozenset(members)), label)
    by_num, by_day = collections.defaultdict(set), collections.defaultdict(set)
    for u in accounts:
        m = re.match(r"^([A-Za-z_.]+?)[_.-]?(\d{4,})$", u)
        if m:
            by_num[m.group(2)].add(u)
        if (created.get(u) or "")[:10]:
            by_day[created[u][:10]].add(u)
    for k, v in by_num.items():
        add("numeric suffix", k, v)
    letters = {u: re.match(r"[a-z]*", u.lower()).group(0) for u in accounts}
    for u, w in letters.items():
        # shortest suffix (>= 4 letters, after a first-name head) / prefix (>= 5) shared with another account
        for L in range(4, len(w)):
            g = {v for v, x in letters.items() if len(x) > L and x.endswith(w[-L:])}
            if len(g) > 1:
                add("name stem", "*" + os.path.commonprefix([letters[v][::-1] for v in g])[::-1], g); break
        for L in range(5, len(w) + 1):
            g = {v for v, x in letters.items() if x.startswith(w[:L])}
            if len(g) > 1:
                add("name stem", os.path.commonprefix([letters[v] for v in g]) + "*", g); break
    for k, v in by_day.items():
        add("created same day", k, v)
    order = {"numeric suffix": 0, "name stem": 1, "created same day": 2}
    return sorted(((sig, lab, sorted(m)) for (sig, m), lab in fams.items()),
                  key=lambda f: (order[f[0]], -len(f[2]), f[1]))


def report(db, out_md="report.md", out_csv="pins.csv", since=None):
    rows = db.q("""SELECT p.*, l.final_url, l.final_domain, l.path, l.key, l.kind, l.error AS link_error,
                   u.full_name, u.created_at AS acct_created, u.pin_count AS acct_pins,
                   u.board_count AS acct_boards, u.follower_count AS acct_followers,
                   u.status AS acct_status
                   FROM pins p LEFT JOIN links l ON l.link=p.link
                   LEFT JOIN users u ON u.username=p.pinner""")
    rows = [dict(r) for r in rows]
    users = {u["username"]: dict(u) for u in db.q("SELECT * FROM users")}
    attribute(rows, users)

    cols = ["pin_id", "status", "source", "first_seen", "created_at", "pinner", "full_name",
            "native_creator", "is_repin", "operator", "saver", "op_created", "board_name", "title", "description", "repin_count", "link",
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
    ops = {r["operator"] for r in bait if r["operator"]}
    savers = {r["pinner"] for r in bait if r["saver"]}
    P(f"- Distinct posters of bait pins: **{len({r['pinner'] for r in bait if r['pinner']})}** — "
      f"operator accounts (created the bait): **{len(ops)}**, savers (repinned / carried it): **{len(savers)}**")
    P(f"- Distinct outbound links: **{len({r['link'] for r in linked})}**, "
      f"final domains: **{len({r['final_domain'] for r in linked if r['final_domain']})}**, "
      f"smartlink keys: **{len({r['key'] for r in linked if r['key']})}**")
    if since:
        P(f"- New pins found this run: **{sum(1 for r in rows if (r['first_seen'] or '') >= since)}**")

    P("\n## Link chain types\n")
    P(md_table(["count", "chain"], [[n, k] for k, n in collections.Counter(
        r["kind"] or "(unresolved)" for r in linked).most_common()], "rl"))

    P("\n## Final domains\n")
    T = []
    by_dom = collections.defaultdict(list)
    for r in linked:
        by_dom[r["final_domain"] or "(unresolved)"].append(r)
    for dom, rs in sorted(by_dom.items(), key=lambda kv: -len(kv[1])):
        dates = sorted(r["created_at"] for r in rs if r["created_at"])
        T.append([dom, len(rs), len({r['key'] for r in rs if r['key']}), len({r['operator'] for r in rs if r['operator']}),
                  sum(r['saver'] for r in rs), dates[0][:10] if dates else '', dates[-1][:10] if dates else ''])
    P(md_table(["domain", "pins", "keys", "operators", "via savers", "first pin", "last pin"], T, "lrrrrll"))

    P("\n## Domain timeline (bait pins per ISO week)\n")
    weeks = collections.defaultdict(collections.Counter)
    for r in linked:
        d = parse_iso(r["created_at"] or "")
        if d:
            y, w, _ = d.isocalendar(); weeks[f"{y}-W{w:02d}"][r["final_domain"] or "?"] += 1
    for wk in sorted(weeks):
        P(f"- {wk}: " + ", ".join(f"{d} ×{n}" for d, n in weeks[wk].most_common()))

    P("\n## Title variants\n")
    P(md_table(["count", "title"], [[n, f"`{k}`"] for k, n in
                                     collections.Counter(r["title"] for r in bait).most_common(25)], "rl"))

    def doms_of(accts):
        return ', '.join(sorted({r['final_domain'] for r in linked if r['operator'] in accts and r['final_domain']}))

    P("\n## Account families (operators only; heuristic)\n")
    created = {u: (users.get(u) or {}).get("created_at") for u in ops}
    fam = name_families(sorted(ops), created)
    if fam:
        P(md_table(["signal", "family", "n", "accounts", "domains"],
                   [[sig, f"`{lab}`", len(m), ', '.join(m), doms_of(m)] for sig, lab, m in fam], "llrll"))
    else:
        P("- none found yet")
    unprofiled = sorted(u for u in ops if not created.get(u))
    if unprofiled:
        P(f"\n{len(unprofiled)} operator accounts have no profile yet (run `profiles`): {', '.join(unprofiled)}")

    P("\n## Clusters (operators sharing a key, auctionr id, tinyurl, netlify host or image)\n")
    cl = clusters(bait)
    multi = [c for c in cl if len(c[0]) > 1]
    if multi:
        P(md_table(["#", "accounts", "n", "bait pins", "shared signals", "domains", "first pin", "last pin"],
                   [[i, ', '.join(a), len(a), sum(1 for r in bait if r['operator'] in a),
                     ', '.join(f"{k} ×{v}" for k, v in sorted(sh.items())),
                     doms_of(set(a)),
                     min((r['created_at'] or '' for r in bait if r['operator'] in a), default='')[:10],
                     max((r['created_at'] or '' for r in bait if r['operator'] in a), default='')[:10]]
                    for i, (a, sh) in enumerate(multi, 1)], "rlrrllll"))
    P(f"\n{len(cl) - len(multi)} operator accounts share no signal with any other account: "
      + ', '.join(a[0] for a, _ in cl if len(a) == 1))

    P(f"\n## Waves (an operator's bait pins at most {WAVE_GAP_MIN} min apart; savers' copies excluded)\n")
    wv = waves(bait)
    big = [w for w in wv if len(w[1]) > 1]
    T, last = [], {}
    for acct, ws in wv:
        start = ws[0][0]
        gap = round((start - last[acct]).total_seconds() / 86400, 1) if acct in last else ''
        last[acct] = ws[-1][0]
        if len(ws) > 1:
            T.append([acct, start.strftime("%Y-%m-%d %H:%M"), len(ws),
                      round((ws[-1][0] - start).total_seconds() / 60, 1), gap,
                      ', '.join(sorted({r['final_domain'] for _, r in ws if r['final_domain']}))])
    if T:
        P(md_table(["account", "start (UTC)", "pins", "minutes", "days since prev.", "domains"], T, "llrrrl"))
    P(f"\n{len(big)} waves of 2+ pins, {len(wv) - len(big)} single pins.")
    sync = []
    for i, (a1, w1) in enumerate(big):
        for a2, w2 in big[i + 1:]:
            if a1 != a2 and abs((w1[0][0] - w2[0][0]).total_seconds()) <= WAVE_GAP_MIN * 60:
                sync.append(f"{a1} + {a2} at {min(w1[0][0], w2[0][0]).strftime('%Y-%m-%d %H:%M')}")
    if sync:
        P("\nWaves of different accounts starting within the same window (one scheduler?):\n")
        P("\n".join(f"- {x}" for x in sorted(sync, key=lambda x: x.split(' at ')[1])))

    P("\n## Warm-up (days from operator account creation to bait pin; savers excluded)\n")
    gaps = sorted(r["warmup_days"] for r in linked if r["warmup_days"] is not None)
    if gaps:
        hist = collections.Counter(int(g // 7) for g in gaps)
        P(f"- n={len(gaps)}, median **{gaps[len(gaps)//2]}** days, min {gaps[0]}, max {gaps[-1]}")
        for wk in sorted(hist):
            P(f"  - {wk*7:>3}–{wk*7+6:<3} days: {'█' * hist[wk]} {hist[wk]}")
    else:
        P("- no data yet")

    P("\n## Operator accounts (created bait pins)\n")
    T = []
    acc = collections.defaultdict(list)
    for r in bait:
        if r["operator"]:
            acc[r["operator"]].append(r)
    for u, rs in sorted(acc.items(), key=lambda kv: (users.get(kv[0]) or {}).get("created_at") or "~"):
        x = users.get(u) or {}
        T.append([u, (x.get('created_at') or '')[:10], x.get('pin_count'), x.get('board_count'),
                  x.get('follower_count'), x.get('status'), sum(not r['saver'] for r in rs),
                  sum(r['saver'] for r in rs), ', '.join(sorted({r['final_domain'] for r in rs if r['final_domain']}))])
    P(md_table(["account", "created", "pins", "boards", "followers", "status", "own bait pins",
                "via savers", "domains"], T, "llrrrlrrl"))

    only = sorted(u for u, rs in acc.items() if not any(not r["saver"] for r in rs))
    P("\n## Operators known only through saved copies\n")
    if only:
        P("Their own bait pins are not in the database (removed, or not crawled yet) - only savers' "
          "repins point to them.\n")
        T = []
        for u in only:
            x = users.get(u) or {}
            st = x.get("status") or "?"
            T.append([u, (x.get("created_at") or "")[:10], x.get("pin_count"),
                      st + (" (not profiled yet)" if st == "new" else ""),
                      "yes" if x.get("crawled") else "no", len(acc[u]),
                      ', '.join(sorted({r['pinner'] for r in acc[u]})),
                      ', '.join(sorted({r['final_domain'] for r in acc[u] if r['final_domain']}))])
        P(md_table(["account", "created", "pins", "status", "crawled", "saved copies", "saved by", "domains"],
                   T, "llrlrrll"))
    else:
        P("- none")

    P(f"\n## Savers (repinned or carried someone else's bait; aged = account ≥ {AGED_DAYS} days older "
      "than the pin with 1 bait pin)\n")
    T = []
    sv = collections.defaultdict(list)
    for r in bait:
        if r["saver"]:
            sv[r["pinner"]].append(r)
    for u, rs in sorted(sv.items(), key=lambda kv: (users.get(kv[0]) or {}).get("created_at") or "~"):
        x = users.get(u) or {}
        T.append([u, (x.get('created_at') or '')[:10], x.get('pin_count'), len(rs),
                  ', '.join(sorted({r['operator'] for r in rs if r['operator']})) or '(unknown)',
                  ', '.join(sorted({r['final_domain'] for r in rs if r['final_domain']}))])
    P(md_table(["account", "created", "pins", "bait pins carried", "original creator", "domains"], T, "llrrll"))

    P("\n## Bait pins with links (newest first)\n")
    P(md_table(["created", "pin", "operator", "saved by", "warm-up", "repins", "destination", "key"],
               [[(r['created_at'] or '')[:16], f"[{r['pin_id']}](https://www.pinterest.com/pin/{r['pin_id']}/)",
                 r['operator'] or '?', r['pinner'] if r['saver'] else '', r['warmup_days'], r['repin_count'],
                 f"{r['final_domain'] or ''}{(r['path'] or '')[:25]}", (r['key'] or '')[:10]]
                for r in sorted(linked, key=lambda r: r["created_at"] or "", reverse=True)[:300]], "llllrrll"))

    nolink = [r for r in bait if not r["link"]]
    if nolink:
        P("\n## Bait-looking pins with no link (warming up, stripped, or repins)\n")
        for r in nolink:
            P(f"- {r['pin_id']} — {r['pinner']} — board `{r['board_name']}` — title `{r['title'][:50]}` "
              f"— repin={r['is_repin']}")

    qs = db.q("""SELECT query, COUNT(DISTINCT substr(run_at,1,16)) runs, SUM(results) results,
                 SUM(bait_hits) new_pins, MAX(run_at) last FROM searches GROUP BY query
                 ORDER BY new_pins DESC""")
    if qs:
        P("\n## Search query performance\n\nQueries with 0 new pins over several runs are candidates "
          "for deleting from the queries file.\n")
        P(md_table(["query", "runs", "results seen", "new pins added", "last run"],
                   [[f"`{q['query']}`", q['runs'], q['results'], q['new_pins'], (q['last'] or '')[:16]] for q in qs],
                   "lrrrl"))

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
    r.add_argument("--reclassify", action="store_true",
                   help="offline only: re-decode embedded URLs and recompute link kinds of resolved links")
    pr = sp.add_parser("profiles"); pr.add_argument("--force", action="store_true")
    s = sp.add_parser("search"); s.add_argument("--query", action="append", default=[])
    s.add_argument("--queries", default="queries.txt", help="queries file (default: queries.txt)")
    s.add_argument("--only", action="store_true", help="use only --query, not the queries file")
    qp = sp.add_parser("queries", help="list the queries file, or --update it from bait pins in the db")
    qp.add_argument("--queries", default="queries.txt")
    qp.add_argument("--update", action="store_true", help="append phrases learned from bait pins")
    qp.add_argument("--prune", action="store_true", help="comment out queries with 0 results over >= --min-runs runs")
    qp.add_argument("--min-runs", type=int, default=2)
    qp.add_argument("--dry-run", action="store_true", help="with --update/--prune: only show what would change")
    qp.add_argument("--min-count", type=int, default=1, help="only phrases seen on at least N bait pins")
    s.add_argument("--max-pages", type=int, default=6)
    s.add_argument("--sample", type=int, default=5, help="random items per list each step: search queries, and each expand list (0 = all; default %(default)s)")
    e = sp.add_parser("expand"); e.add_argument("--max-related", type=int, default=40)
    e.add_argument("--max-pages", type=int, default=6)
    e.add_argument("--sample", type=int, default=5, help="random items per list each step: search queries, and each expand list (0 = all; default %(default)s)")
    e.add_argument("--max-user-pins", type=int, default=5000,
                    help="expand: skip accounts with more pins than this (0 = no limit; default %(default)s)")
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
    pc = sp.add_parser("pics", help="copy archived images to archive/_pics and run pixboards there")
    pc.add_argument("--dir", default="archive")
    pc.add_argument("--no-board", action="store_true", help="only copy, don't run pixboards")
    sp.add_parser("status")
    sp.add_parser("clean", help="delete non-numeric junk IDs (e1_...) from the database")
    ru = sp.add_parser("run", help="default full procedure")
    ru.add_argument("--no-search", action="store_true")
    ru.add_argument("--no-expand", action="store_true")
    ru.add_argument("--rounds", type=int, default=2, help="expand rounds")
    ru.add_argument("--max-pages", type=int, default=6)
    ru.add_argument("--max-related", type=int, default=40)
    ru.add_argument("--sample", type=int, default=5, help="random items per list each step: search queries, and each expand list (0 = all; default %(default)s)")
    ru.add_argument("--max-user-pins", type=int, default=5000,
                    help="expand: skip accounts with more pins than this (0 = no limit; default %(default)s)")
    ru.add_argument("--query", action="append", default=[])
    ru.add_argument("--queries", default="queries.txt", help="queries file (default: queries.txt)")
    ru.add_argument("--no-update-queries", action="store_true",
                    help="don't append learned phrases to the queries file")
    ru.add_argument("--no-discover", action="store_true",
                    help="don't look for new pins (same as --no-search --no-expand)")
    ru.add_argument("--no-archive", action="store_true")
    ru.add_argument("--no-pics", action="store_true", help="don't copy images to archive/_pics / run pixboards")
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
        step_reclassify(db) if args.reclassify else step_resolve(db, args.force)
    elif args.cmd == "profiles":
        step_profiles(db, api, args.force)
    elif args.cmd == "search":
        qs = args.query if args.only else load_queries(args.queries) + args.query
        log(f"[search] using {'--query only' if args.only else args.queries}")
        step_search(db, api, list(dict.fromkeys(qs)), args.max_pages, args.sample)
    elif args.cmd == "queries":
        if args.prune:
            prune_queries(db, args.queries, args.dry_run, args.min_runs)
        if args.update:
            update_queries(db, args.queries, args.dry_run, args.min_count)
        elif not args.prune:
            stats = {r["query"]: r for r in db.q(
                "SELECT query, COUNT(*) pages, SUM(bait_hits) hits, MAX(run_at) last FROM searches GROUP BY query")}
            qs = load_queries(args.queries)
            log(f"{len(qs)} queries in {args.queries}:")
            for q in qs:
                s_ = stats.get(q)
                log(f"  {q!r}" + (f"   [new pins: {s_['hits']}, last run {s_['last'][:16]}]" if s_ else "   [never run]"))
            known = {_qnorm(x) for x in qs} | _pruned_queries(args.queries)
            pending = [q for q in derive_queries(db) if _qnorm(q) not in known]
            if pending:
                log(f"\n{len(pending)} new phrases could be learned from the db - run `queries --update`")
    elif args.cmd == "expand":
        step_expand(db, api, args.max_related, args.max_pages, args.sample, args.max_user_pins)
    elif args.cmd == "recheck":
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat()
        ids = [r["pin_id"] for r in db.q("SELECT pin_id FROM pins WHERE status IN ('ok','error') "
                                          "AND (fetched_at IS NULL OR fetched_at < ?)", cutoff)]
        step_fetch(db, api, ids); step_resolve(db); step_profiles(db, api, force=True); report(db)
        step_archive(db)  # archives anything new; index shows which archived pins are now removed
    elif args.cmd == "pics":
        step_pics(args.dir, not args.no_board)
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
        e = db.q("""SELECT COUNT(*) n, SUM(expand_new>0) found, SUM(expand_new=0 AND expand_seen>0) none_new,
                    SUM(expand_seen=0) no_related, SUM(expanded_at IS NOT NULL AND expand_seen IS NULL) failed
                    FROM pins WHERE expanded=1""")[0]
        log(f"  expanded pins: {e['n']} (found new: {e['found'] or 0}, nothing new: {e['none_new'] or 0}, "
            f"no related pins: {e['no_related'] or 0}, request failed: {e['failed'] or 0}, "
            f"before tracking: {e['n'] - (e['found'] or 0) - (e['none_new'] or 0) - (e['no_related'] or 0) - (e['failed'] or 0)})")
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
            if not args.no_update_queries:
                update_queries(db, args.queries)
            qs = list(dict.fromkeys(load_queries(args.queries) + args.query))
            log(f"[search] {len(qs)} queries from {args.queries}")
            step_search(db, api, qs, args.max_pages, args.sample)
            step_fetch(db, api)
        if not args.no_expand:
            for i in range(args.rounds):
                log(f"===== expand round {i+1}/{args.rounds} =====")
                before = db.q("SELECT COUNT(*) n FROM pins")[0]["n"]
                step_expand(db, api, args.max_related, args.max_pages, args.sample, args.max_user_pins)
                step_fetch(db, api)
                if db.q("SELECT COUNT(*) n FROM pins")[0]["n"] == before:
                    log("  nothing new; stopping expansion"); break
        step_resolve(db); step_profiles(db, api)
        if not args.no_search and not args.no_update_queries:
            update_queries(db, args.queries)   # learn from pins found this run, ready for next time
        report(db, since=start)
        if not args.no_archive:
            step_archive(db, accounts=args.archive_accounts, wayback=args.wayback)
            if not args.no_pics:
                step_pics()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted - progress so far is saved in the database; re-run to continue")
