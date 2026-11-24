import json, sys, urllib.request, urllib.parse, collections

USER = "indyjanaam"
BAIT_PIN = "1108659633317786298"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

def get(resource, options, source="/"):
    q = urllib.parse.urlencode({"source_url": source,
                                "data": json.dumps({"options": options, "context": {}})})
    req = urllib.request.Request(
        f"https://www.pinterest.com/resource/{resource}/get/?{q}",
        headers={"User-Agent": UA, "Accept": "application/json",
                 "X-Requested-With": "XMLHttpRequest",
                 "X-Pinterest-PWS-Handler": "www/[username].js"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("resource_response", {})

def paged(resource, options, source="/", limit=20):
    bookmark, out = None, []
    for _ in range(limit):
        o = dict(options)
        if bookmark: o["bookmarks"] = [bookmark]
        resp = get(resource, o, source)
        data = resp.get("data") or []
        if isinstance(data, dict): data = data.get("results", []) or [data]
        out += data
        bookmark = resp.get("bookmark")
        if not bookmark or bookmark == "-end-" or not data: break
    return out

def u(x): return (x or {}).get("username") if isinstance(x, dict) else None

def row(p):
    return {
        "id": p.get("id"),
        "created_at": p.get("created_at"),
        "pinner": u(p.get("pinner")),
        "native_creator": u(p.get("native_creator")),
        "origin_pinner": u(p.get("origin_pinner")),
        "is_repin": p.get("is_repin"),
        "title": (p.get("title") or p.get("grid_title") or "")[:80],
        "link": p.get("link"),
        "domain": p.get("domain"),
        "img": p.get("image_signature"),
        "board": (p.get("board") or {}).get("name"),
    }

def section(t): print(f"\n===== {t} =====")

# 1. The bait pin in detail
section("BAIT PIN DETAIL")
try:
    d = get("PinResource", {"id": BAIT_PIN, "field_set_key": "detailed"}, f"/pin/{BAIT_PIN}/").get("data") or {}
    r = row(d)
    for k in ["updated_at", "is_repin", "repin_count", "comment_count", "parent_pin", "via_pinner",
              "origin_pinner", "native_creator", "is_native", "is_stale_product", "tracked_link"]:
        v = d.get(k)
        if isinstance(v, dict): v = v.get("username") or v.get("id")
        r[k] = v
    for k, v in r.items(): print(f"{k}: {v}")
except Exception as e:
    print("error:", e)

# 2. Boards
section("BOARDS")
boards = []
try:
    boards = paged("BoardsResource", {"username": USER, "page_size": 50, "privacy_filter": "all",
                                      "sort": "last_pinned_to", "field_set_key": "profile_grid_item"},
                   f"/{USER}/")
    for b in boards:
        print(b.get("id"), "|", b.get("name"), "|", b.get("url"), "| pins:", b.get("pin_count"),
              "| created:", b.get("created_at"))
except Exception as e:
    print("error:", e)

# 3. Every pin on every board
pins = []
for b in boards:
    try:
        pins += paged("BoardFeedResource", {"board_id": b.get("id"), "board_url": b.get("url"),
                                            "page_size": 25, "field_set_key": "react_grid_pin"},
                      b.get("url") or "/")
    except Exception as e:
        print("board feed error:", b.get("name"), e)

# 4. Pins the user created themselves (as opposed to saved)
created = []
try:
    created = paged("UserActivityPinsResource", {"username": USER, "page_size": 25,
                                                 "field_set_key": "grid_item"}, f"/{USER}/_created/")
except Exception as e:
    print("created-pins error:", e)

section(f"ALL BOARD PINS ({len(pins)})")
rows = [row(p) for p in pins if isinstance(p, dict) and p.get("id")]
for r in sorted(rows, key=lambda r: r["created_at"] or ""):
    print(f'{r["created_at"]} | {r["id"]} | pinner={r["pinner"]} creator={r["native_creator"]} '
          f'origin={r["origin_pinner"]} repin={r["is_repin"]} | {r["title"]!r} | {r["link"]}')

section(f"PINS CREATED BY {USER} ({len(created)})")
for p in created:
    if isinstance(p, dict) and p.get("id"):
        r = row(p); print(f'{r["created_at"]} | {r["id"]} | {r["title"]!r} | {r["link"]}')

section("SUMMARY")
print("pins per day:", dict(sorted(collections.Counter((r["created_at"] or "")[:16] for r in rows).items())))
print("native creators:", collections.Counter(r["native_creator"] for r in rows).most_common(15))
print("link domains:", collections.Counter(r["domain"] for r in rows if r["link"]).most_common(15))
print("pins WITH a link:", sum(1 for r in rows if r["link"]), "/", len(rows))
dup = [k for k, v in collections.Counter(r["img"] for r in rows if r["img"]).items() if v > 1]
print("duplicate images:", len(dup))
