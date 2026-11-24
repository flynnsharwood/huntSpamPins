import json, urllib.request, urllib.parse, urllib.error, collections

PINS = [
    "1108659633317786298",  # original (indyjanaam)
    "1149754979902693740", "1092967403352554653", "1095571046888211441",
    "1108941108282058921", "1126533294325842653", "269019777737634451",
    "1110841064371400020", "1096767315545003096",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
REDIRECTORS = {"tinyurl.com","www.tinyurl.com","bit.ly","t.co","cutt.ly","is.gd","rb.gy","shorturl.at",
               "tiny.cc","ow.ly","goo.gl","t.ly","rebrand.ly","s.id","shorturl.gg","bitly.com"}

def s(x, n=90):
    if isinstance(x, dict): x = x.get("format") or x.get("text") or x.get("name") or json.dumps(x)
    return str(x or "")[:n]
def uname(x): return x.get("username") if isinstance(x, dict) else None

def api(resource, options, source="/"):
    q = urllib.parse.urlencode({"source_url": source, "data": json.dumps({"options": options, "context": {}})})
    req = urllib.request.Request(f"https://www.pinterest.com/resource/{resource}/get/?{q}",
        headers={"User-Agent": UA, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                 "X-Pinterest-PWS-Handler": "www/[username].js"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return (json.load(r).get("resource_response") or {}).get("data") or {}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
opener = urllib.request.build_opener(NoRedirect)

def unwrap(url):
    """Follow shorteners/Google redirector only; never request the final destination."""
    chain = [url]
    for _ in range(8):
        p = urllib.parse.urlparse(url); host = p.netloc.lower()
        if host in ("www.google.com", "google.com") and p.path == "/url":
            q = urllib.parse.parse_qs(p.query).get("q") or urllib.parse.parse_qs(p.query).get("url")
            if not q: break
            url = q[0]; chain.append(url + "   [decoded from google.com/url, not fetched]"); continue
        if host not in REDIRECTORS: break
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
            opener.open(req, timeout=20); break
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urllib.parse.urljoin(url, loc); chain.append(url); continue
            chain.append(f"[HTTP {e.code}]"); break
        except Exception as e:
            chain.append(f"[error {e}]"); break
    return url, chain

results, users = [], {}
for pid in PINS:
    print(f"\n===== PIN {pid} =====")
    try:
        d = api("PinResource", {"id": pid, "field_set_key": "detailed"}, f"/pin/{pid}/")
    except Exception as e:
        print("fetch error:", e); continue
    pinner = uname(d.get("pinner")); creator = uname(d.get("native_creator"))
    link = d.get("link") or d.get("tracked_link") or ""
    info = {
        "created_at": d.get("created_at"), "pinner": pinner, "native_creator": creator,
        "is_repin": d.get("is_repin"), "board": s((d.get("board") or {}).get("name")),
        "title": s(d.get("title") or d.get("grid_title")), "description": s(d.get("description"), 150),
        "repin_count": d.get("repin_count"), "img": d.get("image_signature"), "link": link,
    }
    for k, v in info.items(): print(f"{k}: {v}")
    final, chain = (unwrap(link) if link else ("", []))
    if chain:
        print("redirect chain:"); [print("   ->", c) for c in chain]
    fp = urllib.parse.urlparse(final)
    key = (urllib.parse.parse_qs(fp.query).get("key") or [""])[0]
    print(f"FINAL DOMAIN: {fp.netloc}   PATH: {fp.path}   KEY: {key}")
    results.append({**info, "pid": pid, "final_domain": fp.netloc, "path": fp.path, "key": key})
    if pinner: users[pinner] = None

print("\n===== POSTER PROFILES =====")
for u in users:
    try:
        d = api("UserResource", {"username": u, "field_set_key": "profile"}, f"/{u}/")
        print(f'{u}: name={s(d.get("full_name"),40)!r} created={d.get("created_at")} pins={d.get("pin_count")} '
              f'boards={d.get("board_count")} followers={d.get("follower_count")} following={d.get("following_count")} '
              f'website={d.get("website_url")} country={d.get("country") or d.get("locale")}')
    except Exception as e:
        print(f"{u}: error {e}")

print("\n===== SUMMARY =====")
print(f'{"pin":<20} {"created":<32} {"poster":<20} {"repins":>6}  final_domain / path / key')
for r in sorted(results, key=lambda r: r["created_at"] or ""):
    print(f'{r["pid"]:<20} {str(r["created_at"]):<32} {str(r["pinner"]):<20} {str(r["repin_count"]):>6}  '
          f'{r["final_domain"]} {r["path"]} {r["key"]}')
print("\nfinal domains:", collections.Counter(r["final_domain"] for r in results).most_common())
print("keys:", collections.Counter(r["key"] for r in results if r["key"]).most_common())
print("same image reused:", [k for k, v in collections.Counter(r["img"] for r in results).items() if k and v > 1])
