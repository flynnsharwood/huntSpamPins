#!/usr/bin/env python3
"""probe.py - open spam destinations in a sandboxed headless browser and record what they serve.

Safety model (see CLAUDE.md):
  * Nothing is opened unless --yes-i-know is given; without it this is a dry run.
  * Never part of `pinhunt.py run`.
  * The browser runs inside a bubblewrap sandbox (read-only root, empty $HOME and /tmp, only
    the run's output folder writable, no access to the project folder or the database).
  * Traffic goes through --proxy (required unless --direct is given explicitly).
  * Fresh browser context per visit; permissions denied; downloads, service workers, popups,
    notification and push requests blocked; nothing is ever clicked or typed.
  * At most --per-key visits per key/target (default 1), --max per run, --delay between visits.

The parent process (outside the sandbox) selects targets from pinhunt.db and stores results in
the `probes` table; only the child process inside the sandbox imports Playwright.

Usage:
  python3 probe.py                         # dry run: list what would be visited
  python3 probe.py --yes-i-know --max 3        # proxy/locale/timezone from probe_proxy.json
  python3 probe.py --yes-i-know --proxy socks5://127.0.0.1:1080 --max 3
  python3 probe.py --yes-i-know --direct --url http://127.0.0.1:8000/start   # local test
  python3 probe.py --list                  # show stored probe results
"""
import argparse, collections, datetime, json, os, re, shutil, sqlite3, subprocess, sys, time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
UA_DEVICE = "Pixel 7"
REFERER = "https://www.pinterest.com/"
PROBE_KINDS = ("tinyurl", "smartlink", "auctionr", "redirect via", "netlify")

SCHEMA = """
CREATE TABLE IF NOT EXISTS probes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, probed_at TEXT, target TEXT, link TEXT, key TEXT,
  group_id TEXT, proxy TEXT, locale TEXT, timezone TEXT, final_url TEXT, final_domain TEXT,
  title TEXT, category TEXT, nav_chain TEXT, script_hosts TEXT, hosts TEXT, events TEXT,
  params TEXT, dir TEXT, error TEXT);
"""

# keyword -> category, matched on title + page text + final URL (first match wins per category)
CATEGORIES = [
    ("notification prompt", r"\b(click allow|press allow|tap allow|allow to (continue|verify)|"
                            r"confirm (that )?you are not a robot|not a robot)\b"),
    ("fake virus / scareware", r"\b(virus|infected|malware|your (phone|device) is|clean(er)? now|"
                               r"security alert|battery is damaged)\b"),
    ("sweepstakes / prize", r"\b(congratulations|you (have )?won|winner|prize|gift card|reward|"
                            r"spin the wheel|claim (now|your))\b"),
    ("dating / adult", r"\b(dating|hookup|singles|meet (women|girls|local)|horny|sex|cam(s|girl)?|"
                       r"adult|18\+|nude|xxx|chat with)\b"),
    ("app / vpn install", r"\b(install|download|vpn|app store|google play|update (required|now))\b"),
    ("crypto / investing", r"\b(crypto|bitcoin|invest(ment)?|trading|profit)\b"),
    ("video / streaming", r"\b(watch (now|video|full)|stream(ing)?|play video|hd video)\b"),
]


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host(u):
    try:
        return urllib.parse.urlparse(u).hostname or ""
    except ValueError:
        return ""


def redact_proxy(p):
    if not p:
        return "direct"
    u = urllib.parse.urlparse(p)
    return f"{u.scheme}://{u.hostname}:{u.port}" if u.hostname else p


def categorise(title, text, url, error=None):
    if error or (url or "").startswith("chrome-error:"):
        return "error"
    if not (title or "").strip() and not (text or "").strip():
        return "blank page (likely filtered: bot / proxy / geo)"
    blob = " ".join([title or "", text or "", url or ""]).lower()
    cats = [c for c, rx in CATEGORIES if re.search(rx, blob)]
    return ", ".join(cats) or "unknown"


# ---------------------------------------------------------------- target selection (parent)

def db_open(path):
    con = sqlite3.connect(path, timeout=120)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def targets(con, per_key=1, domain=None, only_key=None, kinds=None, proxy_label=None):
    """One destination per key (smartlinks) / per URL path (auctionr, netlify, others), skipping
    groups already probed per_key times successfully through the same proxy (so switching proxy /
    country probes them again). Newest pins first."""
    rows = con.execute("""
        SELECT l.link, l.final_url, l.final_domain, l.key, l.kind, l.path, MAX(p.created_at) AS last
        FROM links l JOIN pins p ON p.link = l.link
        WHERE l.final_url IS NOT NULL AND l.final_url != '' AND p.bait = 1
        GROUP BY l.link ORDER BY last DESC""").fetchall()
    done = collections.Counter(r[0] for r in con.execute(
        "SELECT group_id FROM probes WHERE (error IS NULL OR error = '') AND (? IS NULL OR proxy = ?)",
        (proxy_label, proxy_label)))
    seen, out = set(), []
    for r in rows:
        kind = r["kind"] or ""
        if not any(kind.startswith(k) for k in (kinds or PROBE_KINDS)):
            continue
        if domain and domain not in (r["final_domain"] or ""):
            continue
        if only_key and r["key"] != only_key:
            continue
        h = host(r["final_url"])
        # one target per smartlink key, per netlify tracker host, per other URL path
        gid = r["key"] or (h if h.endswith("netlify.app") else f"{h}{urllib.parse.urlparse(r['final_url']).path}")
        if gid in seen or done[gid] >= per_key:
            continue
        seen.add(gid)
        out.append({"target": r["final_url"], "link": r["link"], "key": r["key"] or "",
                    "group_id": gid, "kind": kind, "domain": h, "last_pin": r["last"]})
    # interleave by final domain, then by link scheme, so a small --max covers several of both
    return round_robin(round_robin(out, lambda t: t["domain"]), lambda t: t["kind"].split()[0])


def round_robin(items, keyf):
    by = collections.OrderedDict()
    for t in items:
        by.setdefault(keyf(t), []).append(t)
    mixed = []
    while any(by.values()):
        for lst in by.values():
            if lst:
                mixed.append(lst.pop(0))
    return mixed


# ---------------------------------------------------------------- sandbox launcher (parent)

def browsers_path():
    p = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    return os.path.realpath(p)


def run_child(task_file, out_dir, use_bwrap=True, chromium=None):
    cmd = [sys.executable, "-I", os.path.join(HERE, "probe.py"), "_child", task_file]
    if use_bwrap:
        if not shutil.which("bwrap"):
            sys.exit("bubblewrap (bwrap) not found - install it, or pass --no-sandbox (not recommended)")
        home, bp, script = os.path.expanduser("~"), browsers_path(), os.path.join(HERE, "probe.py")
        cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               "--tmpfs", "/tmp", "--tmpfs", home, "--tmpfs", "/run",
               "--ro-bind", bp, bp, "--ro-bind", script, script, "--bind", out_dir, out_dir,
               *(["--ro-bind", os.path.dirname(os.path.realpath(chromium))] * 1 +
                 [os.path.dirname(os.path.realpath(chromium))] if chromium else []),
               "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
               "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
               "--setenv", "HOME", home, "--setenv", "PLAYWRIGHT_BROWSERS_PATH", bp,
               "--chdir", out_dir] + cmd
    return subprocess.run(cmd).returncode


def ingest(con, result):
    con.execute("""INSERT INTO probes(probed_at, target, link, key, group_id, proxy, locale, timezone,
        final_url, final_domain, title, category, nav_chain, script_hosts, hosts, events, params,
        dir, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (result["probed_at"], result["target"], result.get("link"), result.get("key"),
         result.get("group_id"), result["proxy"], result["locale"], result["timezone"],
         result.get("final_url"), host(result.get("final_url") or ""), result.get("title"),
         categorise(result.get("title"), result.get("text"), result.get("final_url"), result.get("error")),
         json.dumps(result.get("nav_chain", [])), json.dumps(result.get("script_hosts", [])),
         json.dumps(result.get("hosts", {})), json.dumps(result.get("events", [])),
         json.dumps(result.get("params", {})),
         result.get("dir"), result.get("error")))
    con.commit()


# ---------------------------------------------------------------- browser (child, in sandbox)

INIT_JS = r"""
(() => {
  const log = (k, v) => { try { window.__probeLog(k, String(v === undefined ? '' : v).slice(0, 500)); } catch (e) {} };
  try {
    if (window.Notification) {
      Notification.requestPermission = function () { log('notification.requestPermission', location.href); return Promise.resolve('denied'); };
    }
  } catch (e) {}
  try {
    if (window.PushManager) PushManager.prototype.subscribe = function () {
      log('push.subscribe', location.href); return Promise.reject(new DOMException('denied', 'NotAllowedError')); };
  } catch (e) {}
  try {
    if (navigator.serviceWorker) navigator.serviceWorker.register = function (u) {
      log('serviceWorker.register', u); return Promise.reject(new Error('blocked by probe')); };
  } catch (e) {}
  try { window.open = function (u) { log('window.open', u); return null; }; } catch (e) {}
  try { navigator.clipboard.writeText = function (t) { log('clipboard.writeText', t); return Promise.resolve(); }; } catch (e) {}
  for (const ev of ['beforeunload']) window.addEventListener(ev, () => log(ev, location.href));
})();
"""


def child(task_file):
    from playwright.sync_api import sync_playwright, Error as PWError
    with open(task_file) as f:
        job = json.load(f)
    os.unlink(task_file)                                 # holds the proxy credentials
    out_dir = os.path.dirname(task_file)
    with sync_playwright() as pw:
        device = dict(pw.devices[UA_DEVICE])
        launch = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
        if job.get("chromium"):
            launch["executable_path"] = job["chromium"]
        if job["proxy"]:
            u = urllib.parse.urlparse(job["proxy"])
            launch["proxy"] = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
            if u.username:
                launch["proxy"].update(username=urllib.parse.unquote(u.username),
                                       password=urllib.parse.unquote(u.password or ""))
        browser = pw.chromium.launch(**launch)
        for i, t in enumerate(job["tasks"]):
            if i:
                time.sleep(job["delay"])
            d = os.path.join(out_dir, f"{i + 1:02d}")
            os.makedirs(d, exist_ok=True)
            res = visit(browser, device, t, d, job, PWError)
            res.update(probed_at=t["probed_at"], target=t["target"], link=t.get("link"),
                       key=t.get("key"), group_id=t.get("group_id"), dir=d,
                       proxy=job["proxy_label"], locale=job["locale"], timezone=job["timezone"])
            with open(os.path.join(d, "result.json"), "w") as f:
                json.dump(res, f, indent=1, ensure_ascii=False)
            print(f"  [{i + 1}/{len(job['tasks'])}] {t['target'][:70]} -> "
                  f"{res.get('final_url', '')[:70]} {res.get('error') or ''}", flush=True)
        browser.close()


def visit(browser, device, task, d, job, PWError):
    task["probed_at"] = now()
    nav, events, scripts, main, failed_reqs = [], [], set(), [], []
    hosts = collections.Counter()
    ctx = browser.new_context(**device, locale=job["locale"], timezone_id=job["timezone"],
                              permissions=[], accept_downloads=False, service_workers="block",
                              record_har_path=os.path.join(d, "traffic.har.zip"),
                              record_har_content="attach")
    ctx.expose_binding("__probeLog", lambda src, k, v: events.append(
        {"t": now(), "event": k, "value": v, "frame": src["frame"].url}))
    ctx.add_init_script(INIT_JS)

    def on_request(req):
        hosts[host(req.url)] += 1
        if req.is_navigation_request() and req.frame == req.frame.page.main_frame:
            nav.append({"url": req.url, "method": req.method,
                        "redirected_from": req.redirected_from.url if req.redirected_from else None})

    def on_response(resp):
        if resp.request.resource_type == "script":
            scripts.add(host(resp.url))
        for n in nav:
            if n["url"] == resp.url and "status" not in n:
                n["status"] = resp.status
                n["location"] = resp.headers.get("location")
                break

    def on_page(p):                                     # popups / new tabs: record, close
        if not main:                                    # the first page is our own
            main.append(p); return
        events.append({"t": now(), "event": "popup", "value": p.url, "frame": ""})
        try:
            p.close()
        except PWError:
            pass

    ctx.on("request", on_request)
    ctx.on("requestfailed", lambda req: failed_reqs.append(
        {"url": req.url, "type": req.resource_type, "error": req.failure}))
    ctx.on("response", on_response)
    ctx.on("page", on_page)
    ctx.on("dialog", lambda dl: (events.append({"t": now(), "event": f"dialog.{dl.type}",
                                                "value": dl.message, "frame": ""}), dl.dismiss()))
    page = ctx.new_page()
    page.on("download", lambda dl: (events.append({"t": now(), "event": "download",
                                                   "value": dl.suggested_filename, "frame": ""}),
                                    dl.cancel()))
    res = {}
    try:
        page.goto(task["target"], referer=job["referer"], wait_until="domcontentloaded",
                  timeout=job["timeout"] * 1000)
    except PWError as e:
        res["goto_error"] = str(e).splitlines()[0][:300]
    # let JS / meta-refresh redirects happen; stop early when the URL is stable
    end, last, stable_since = time.time() + job["settle"], None, time.time()
    while time.time() < end:
        try:
            page.wait_for_timeout(500)
        except PWError:
            break
        if page.url != last:
            last, stable_since = page.url, time.time()
        elif time.time() - stable_since > job["stable"]:
            break
    try:
        res["final_url"] = page.url
        res["title"] = page.title()
        res["text"] = page.inner_text("body", timeout=3000)[:5000]
        with open(os.path.join(d, "page.html"), "w") as f:
            f.write(page.content())
        page.screenshot(path=os.path.join(d, "screenshot.png"), timeout=10000)
    except PWError as e:
        res.setdefault("final_url", page.url)
        res["capture_error"] = str(e).splitlines()[0][:300]
    try:
        ctx.close()                                     # writes the HAR
    except PWError:
        pass
    params = {}
    for n in nav + [{"url": res.get("final_url") or ""}]:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(n["url"]).query)
        if q:
            params[n["url"][:200]] = {k: v[0][:200] for k, v in q.items()}
    if res.get("goto_error") and (not nav or (res.get("final_url") or "").startswith("chrome-error:")):
        res["error"] = res["goto_error"]
    failed = [r for r in failed_reqs if r["url"] != task["target"]]
    if failed:
        res["failed_requests"] = failed[:30]
    res.update(nav_chain=nav, events=events, script_hosts=sorted(scripts),
               hosts=dict(hosts.most_common(50)), params=params)
    return res


# ---------------------------------------------------------------- CLI

def show(con, limit):
    rows = con.execute("""SELECT id, probed_at, proxy, key, final_domain, category, title, events, error
                          FROM probes ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    for r in rows:
        ev = collections.Counter(e["event"] for e in json.loads(r["events"] or "[]"))
        print(f"#{r['id']} {r['probed_at']} via {r['proxy']} key={r['key'][:8] or '-'} "
              f"-> {r['final_domain']} [{r['category']}] {(r['title'] or '')[:60]!r} "
              f"{dict(ev) or ''} {r['error'] or ''}")
    if not rows:
        print("no probes yet")


PROXY_FILE = "probe_proxy.json"
PROXY_EXAMPLE = {
    "_comment": "Settings for probe.py. Edit freely; command-line flags override these. "
                "Keep locale/timezone consistent with the proxy's country.",
    "proxy": "http://host:port",
    "country": "IN",
    "locale": "en-IN",
    "timezone": "Asia/Kolkata",
}


def load_proxy_file(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_") and v not in (None, "")}


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "_child":
        return child(sys.argv[2])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="pinhunt.db")
    ap.add_argument("--out", default="probes", help="output folder (default %(default)s)")
    ap.add_argument("--yes-i-know", action="store_true",
                    help="really open the destinations (otherwise dry run)")
    ap.add_argument("--proxy-file", default=PROXY_FILE,
                    help="JSON file with proxy / locale / timezone (default %(default)s)")
    ap.add_argument("--proxy", help="proxy URL, e.g. socks5://host:port or http://user:pass@host:port "
                                    "(overrides the proxy file)")
    ap.add_argument("--direct", action="store_true", help="allow visiting without a proxy (your own IP!)")
    ap.add_argument("--no-sandbox", action="store_true", help="don't wrap the browser in bubblewrap")
    ap.add_argument("--max", type=int, default=3, help="max visits this run (default %(default)s)")
    ap.add_argument("--per-key", type=int, default=1,
                    help="max successful visits per key / target group overall (default %(default)s)")
    ap.add_argument("--delay", type=float, default=20, help="seconds between visits (default %(default)s)")
    ap.add_argument("--timeout", type=float, default=30, help="page load timeout, s (default %(default)s)")
    ap.add_argument("--settle", type=float, default=20, help="max wait for JS redirects, s (default %(default)s)")
    ap.add_argument("--stable", type=float, default=5, help="stop when the URL is unchanged this long, s")
    ap.add_argument("--chromium", help="Chromium executable to use instead of Playwright's own build")
    ap.add_argument("--locale", help="browser locale (default: proxy file, else en-US)")
    ap.add_argument("--timezone", help="should match the proxy's country "
                                       "(default: proxy file, else America/New_York)")
    ap.add_argument("--referer", default=REFERER)
    ap.add_argument("--domain", help="only targets whose final domain contains this")
    ap.add_argument("--key", help="only this smartlink key")
    ap.add_argument("--kind", action="append", help="only link kinds starting with this (repeatable)")
    ap.add_argument("--url", action="append", help="probe this URL instead of DB targets (repeatable)")
    ap.add_argument("--list", action="store_true", help="show stored probe results and exit")
    args = ap.parse_args()

    cfg = load_proxy_file(args.proxy_file)
    args.proxy = args.proxy or cfg.get("proxy")
    args.locale = args.locale or cfg.get("locale") or "en-US"
    args.timezone = args.timezone or cfg.get("timezone") or "America/New_York"
    country = cfg.get("country", "?") if args.proxy == cfg.get("proxy") else "?"
    label = redact_proxy(args.proxy) + (f" [{country}]" if args.proxy and country != "?" else "")
    con = db_open(args.db)
    if args.list:
        return show(con, 50)
    if args.url:
        tasks = [{"target": u, "link": None, "key": "", "group_id": u, "kind": "manual"} for u in args.url]
    else:
        tasks = targets(con, args.per_key, args.domain, args.key, args.kind, label)
    tasks = tasks[:args.max] if args.max > 0 else tasks
    print(f"{len(tasks)} target(s):")
    for t in tasks:
        print(f"  {t['kind'][:30]:30} {t['group_id'][:40]:40} {t['target'][:90]}")
    if not args.yes_i_know:
        print("\ndry run - nothing opened. Add --yes-i-know to visit (see CLAUDE.md safety rules).")
        return
    if not tasks:
        return
    if not args.proxy and not args.direct:
        sys.exit(f"refusing to visit without a proxy: set \"proxy\" in {args.proxy_file}, pass --proxy, "
                 "or pass --direct to use your own IP")

    run_dir = os.path.abspath(os.path.join(args.out, datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(run_dir)
    for t in tasks:
        t["probed_at"] = None
    job = {"tasks": tasks, "proxy": args.proxy, "proxy_label": label,
           "locale": args.locale, "timezone": args.timezone, "referer": args.referer,
           "chromium": args.chromium, "delay": args.delay, "timeout": args.timeout, "settle": args.settle, "stable": args.stable}
    task_file = os.path.join(run_dir, "job.json")
    fd = os.open(task_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(job, f)
    print(f"\nvisiting via {job['proxy_label']}{'' if args.no_sandbox else ' (bubblewrap sandbox)'}"
          f" -> {run_dir}")
    rc = run_child(task_file, run_dir, use_bwrap=not args.no_sandbox, chromium=args.chromium)
    if os.path.exists(task_file):
        os.unlink(task_file)
    n = 0
    for sub in sorted(os.listdir(run_dir)):
        rf = os.path.join(run_dir, sub, "result.json")
        if os.path.exists(rf):
            with open(rf) as f:
                ingest(con, json.load(f)); n += 1
    print(f"stored {n} probe result(s) in {args.db}:probes (child exit code {rc})")
    show(con, n)


if __name__ == "__main__":
    main()
