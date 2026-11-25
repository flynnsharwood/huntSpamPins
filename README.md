# huntSpamPins

**An open-source-intelligence (OSINT) toolkit that maps a Pinterest spam network end to end:
from the fake accounts that post the bait, through the redirect chains that hide where the links
go, to the ad networks and offers that pay for it.**

I started this after noticing clickbait pins with stolen photos of real people in my feed. It
grew into a small investigation pipeline: a crawler for Pinterest's unofficial web API, an
offline link decoder, attribution and clustering analysis, a tamper-evident evidence archive,
and a sandboxed browser that safely records what the final pages serve.

| | |
| --- | --- |
| Pins collected and analysed | **821** (808 bait pins) |
| Operator accounts identified | **33**, in 9 clusters by shared tracking IDs |
| Bystander accounts correctly excluded | **21** (they only saved a bait pin) |
| Smartlink tracking keys / final domains | **220** / **17** |
| Sandboxed destination probes | **27**, revealing 4 distinct monetised offers |
| Code | ~2,100 lines of Python; the crawler uses only the standard library |

## What is a smartlink?

A **smartlink** is a single ad-network URL (here `https://<random-domain>/<id>?key=<32 hex>`)
that a publisher can post anywhere. Each time someone clicks it, the ad network decides on the
spot which advertiser's page to send them to, based on the visitor's country, device and
browser, picking whatever pays best at that moment. The `key` identifies the publisher, so the
network knows whom to pay for each click, install or sign-up. Because the link itself contains no
destination, the same URL can lead to a dating page for one visitor and a fake prize for the
next, and the networks rotate the domains as they get blocklisted.

## What the investigation found

- **Industrialised account farms.** Accounts are created in batches from name templates
  (`[first name][word][batch number]`), "warmed up" with ~115 repinned filler pins for a median
  of **19 days**, and then post **4-16 bait pins within 1-2 minutes**, in repeated waves ~5 days
  apart. Several accounts post in the same minute, which points to one scheduling tool.
- **Layered redirects to dodge filters.** Pins link to `tinyurl.com`, which forwards to a signed
  `google.com/url` redirect (no warning page), which forwards to the smartlink. Another scheme
  uses Netlify-hosted decoy pages that pass through a traffic-distribution script and a
  [Keitaro](https://keitaro.io/)-style click tracker before reaching the offer.
- **Correct attribution.** Accounts with thousands of pins and years of history looked like
  operators at first. A database query showed that every one of their bait pins was a *repin*
  of a farm account's original, so they are bystanders who saved a pin. The report now attributes
  each pin to its original creator, which also uncovered 13 operators that were visible only
  through other people's saved copies.
- **Who pays.** Probing the destinations through a sandboxed browser showed that, for visitors
  from India, **five tracking keys on five different domains all lead to the same advertiser
  campaign**, a "prize" scam. The Netlify and second-network links end at the same tracker and
  porn landing page pushing an Android app install. The money comes from app installs (CPI) and
  sweepstakes sign-ups.
- **Evasion.** The ad networks return blank pages to headless browsers and "Anonymous Proxy
  detected" to free proxies. Getting real results needed a browser that presents as normal
  mobile Chrome and a reputable egress IP.

## How it works

```
 seeds / search ──► fetch pins ──► resolve links ──► profiles ──► expand ──┐
   (Pinterest           (PinResource)   (HEAD to shorteners,    (operator     │
    resource API)                        offline decoding)       accounts,    │
                                                                 boards)      │
        ▲                                                                     │
        └──────────────── new pins and learned search phrases ◄───────────────┘
                                         │
                        SQLite database (pins, links, users, boards, searches, probes)
                                         │
          ┌──────────────────────────────┼──────────────────────────────┐
          ▼                              ▼                              ▼
   report.md / pins.csv          evidence archive              probe.py (opt-in)
   attribution, clusters,        per pin: JSON, HTML,          sandboxed headless
   waves, name families,         media, redirect chain,        browser records the
   timelines, query stats        SHA-256 manifest              real offers
```

### Engineering highlights

- **Reverse-engineered API client.** Talks to Pinterest's undocumented web resource endpoints
  (pins, users, boards, feeds, search, related pins) with cursor pagination, polite rate
  limiting (1.5 s default) and exponential back-off on 403/429.
- **Safe link resolution.** Only known shorteners are contacted, with `HEAD` or `GET` without
  following redirects. Wrapped URLs (`google.com/url?q=`, `/go/?url=` open redirects) are
  decoded offline, so the crawler never touches an ad server.
- **Graph analysis.** Union-find clustering of accounts over shared signals (tracking key,
  ad-network ID, short link, tracker host), with shared images treated as a weak signal that is
  reported but never merges clusters. Burst ("wave") detection finds accounts posting in the
  same minute, and name-family detection finds batch-made usernames.
- **Self-improving search.** Phrases are learned from new bait titles, normalised to plain-text
  cores, and pruned automatically when they keep returning nothing. A random sample of queries
  and crawl targets is used per run to spread load.
- **Evidence handling.** Each bait pin gets an archive folder with raw API JSON, the page HTML,
  original media, the redirect chain and a SHA-256 manifest, ready for platform abuse reports.
- **Sandboxed dynamic analysis (`probe.py`).** Playwright + Chromium inside a bubblewrap
  sandbox (read-only system, empty home, no access to the project). Each visit:
  - starts in a fresh mobile browser context and follows the pin's real link chain
  - denies all permissions and blocks downloads, popups, push and service workers, never
    clicking or typing
  - records the redirect chain, a full HAR of the traffic, a screenshot and tracking
    parameters

  It refuses to run without an explicit flag and a proxy or VPN, and visits each target only
  once per egress country.
- **Maintainable.** Schema migrations run on start-up, every analysis step can be re-run
  offline against a copy of the database, and Markdown reports pad tables correctly for emoji
  and wide characters.

## Safety and ethics

- **The crawler never opens ad destinations.** Only `probe.py` does, opt-in (`--yes-i-know`),
  sandboxed, behind a proxy, and rate-limited, so the spammers earn next to nothing from it.
  Don't open these links in your own browser.
- **Pinterest is queried gently**, with delays and back-off.
- **The evidence archive contains stolen photos of real people.** It is git-ignored and never
  published, and reports to platforms use text summaries and links, not images.
- **Bystanders are protected.** Accounts that only saved a bait pin are labelled as savers and
  excluded from crawling and from takedown lists.

## Usage

Requirements:
- Python 3.9+.
- Optional: [`gallery-dl`](https://github.com/mikf/gallery-dl) for media downloads, and
  `pixboards` for a local picture board.
- Optional, for `probe.py`: [Playwright](https://playwright.dev/python/) with Chromium,
  [bubblewrap](https://github.com/containers/bubblewrap), and a proxy or VPN.

```sh
python3 pinhunt.py add seeds.txt   # pin/board URLs or IDs, from a file or as arguments
python3 pinhunt.py run             # the full procedure (below)
python3 pinhunt.py status          # quick counts
```

Run commands from the project folder, because outputs are written relative to the current
directory. Global options: `--db FILE` (default `pinhunt.db`) and `--delay SECONDS`.

| command    | what it does                                                                               |
| ---------- | ------------------------------------------------------------------------------------------ |
| `add`      | add pin/board URLs or IDs (arguments or files)                                             |
| `fetch`    | fetch details of pins not fetched yet (`--retry-errors` to retry failures)                 |
| `resolve`  | unwrap outbound links without visiting the destination (`--reclassify`: offline re-decode) |
| `profiles` | fetch the poster accounts' profiles                                                        |
| `search`   | search Pinterest for bait phrases from `queries.txt` (`--query`, `--only`, `--sample`)     |
| `queries`  | list queries; `--update` learns new phrases, `--prune` drops dead ones (`--dry-run`)       |
| `expand`   | crawl operators' created pins and boards, related pins (`--sample`, `--max-user-pins`)     |
| `recheck`  | re-fetch known pins to detect removals (`--days N`)                                        |
| `archive`  | save an evidence folder per bait pin (`--accounts`, `--wayback`, `--force`, `--pin ID`)    |
| `pics`     | copy archived images to `archive/_pics` and run `pixboards` there (`--no-board`)           |
| `report`   | rebuild `report.md` and `pins.csv`                                                         |
| `status`   | quick counts, including expand results                                                     |
| `clean`    | delete junk non-numeric IDs (promoted feed items) from the database                        |
| `run`      | the full procedure                                                                         |

`run` does: fetch → resolve → profiles → update queries → search → fetch → (expand → fetch)
× rounds → resolve → profiles → update queries → report → archive → pics. Useful flags:
- `--sample N`: random queries / crawl items per step (default 5; `0` = all).
- `--rounds N`: number of expand rounds (default 2).
- `--no-search`, `--no-expand`, `--no-discover`: skip finding new pins.
- `--no-archive`, `--no-pics`, `--archive-accounts`, `--wayback`: control the archive step.
- `--no-update-queries`: don't add learned phrases to `queries.txt`.

### Probing destinations (`probe.py`)

```sh
python3 probe.py                                    # dry run: list targets, open nothing
python3 probe.py --yes-i-know --max 3               # uses probe_proxy.json
python3 probe.py --yes-i-know --proxy socks5://127.0.0.1:1080 --max 3
python3 probe.py --yes-i-know --recheck --max 5     # revisit targets that already have a result
python3 probe.py --list                             # stored results
```

- **Proxy settings** go in `probe_proxy.json` (git-ignored; copy `probe_proxy.example.json`):
  `proxy`, `country`, `locale`, `timezone`, which flags override.
  - `"proxy": "warp"` uses Cloudflare WARP running as a system-wide tunnel. The probe checks
    Cloudflare's trace endpoint first and refuses to run if WARP is off.
  - Free public proxies are blocklisted by the ad networks; WARP is not.
- **Targets** come from the database: one per smartlink key, Netlify tracker host or other
  destination, interleaved by domain and link type.
  - A target counts as done after `--per-key` successful visits (default 1) through the same
    proxy. Errors, blank pages and "proxy detected" pages don't count.
  - `--domain`, `--key`, `--kind` and `--url` narrow the choice.
- **Results** go to `probes/<run>/<n>/` (screenshot, page HTML, HAR, `result.json`), to the
  `probes` table, and to a "Destination probes" section of the report. `probes/` is git-ignored.
- `--chromium PATH` uses a different Chromium build if Playwright's own isn't installed.

## Files

| path                 | contents                                                       | in git? |
| -------------------- | -------------------------------------------------------------- | ------- |
| `pinhunt.py`         | the crawler, analysis and report generator                     | yes     |
| `probe.py`           | sandboxed browser probe of destinations (opt-in)               | yes     |
| `pinhunt.db`         | SQLite master database; everything else is generated from it   | yes     |
| `seeds.txt`          | starting pin/board URLs                                        | yes     |
| `queries.txt`        | search phrases, one per line, `#` for comments                 | no      |
| `report.md`          | generated report: attribution, clusters, waves, domains, keys  | no      |
| `pins.csv`           | generated table of all pins                                    | no      |
| `archive/`           | evidence folders per pin, `index.md`, and the `_pics` board    | no      |
| `probes/`            | probe screenshots, HAR files and results                       | no      |
| `probe_proxy.json`   | probe proxy settings (template: `probe_proxy.example.json`)    | no      |

Each `archive/pins/<pin_id>/` holds `summary.md`, `pin.json`, `redirect_chain.json`,
`poster_profile.json`, `poster_boards.json`, `page.html`, `media/` and a SHA-256
`manifest.json`.

## Notes

- Pinterest's unofficial web API changes without notice; the related-pins endpoints are the
  least stable.
- A 403 is usually rate limiting, not a removed pin; a 404 or empty data means removed.
- `database is locked` means another pinhunt process or a database viewer has the file open.
- Test changes offline: copy the database, run with `--db copy.db`, and check syntax with
  `python3 -m py_compile pinhunt.py probe.py`.
