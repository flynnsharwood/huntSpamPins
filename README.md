# huntSpamPins

A small tool for investigating a network of Pinterest spam accounts that post clickbait
pins linking to ad-network "smartlinks". It finds more of these pins, records how the
accounts and links behave, keeps evidence before Pinterest removes it, and produces a
report that can be used to flag accounts and links for takedown.

Everything is stored in a local SQLite database. The tool uses only the Python standard
library.

## Safety

- **`pinhunt.py` never opens ad destinations.** It follows URL shorteners only with
  `HEAD` / `GET` without redirects, and decodes `google.com/url?q=…` wrappers offline. The
  final smartlink pages (`/<id>?key=<32 hex>` on throwaway domains) and `*.netlify.app`
  tracker pages are only opened by the separate `probe.py`, and only with `--yes-i-know`,
  in a sandboxed headless browser behind a proxy (see below). Don't open them in your own
  browser.
- **Pinterest is queried gently**, with a delay between requests (`--delay`, default 1.5 s)
  and back-off on 403/429.
- **The evidence archive contains stolen photos of real people.** It is git-ignored and
  must stay private. When reporting to platforms, use text summaries and links, not images.

## How the spam works

1. **Batch-made accounts.** Names come from a template, `[first name][word][batch number]`,
   for example `<name>blue391504`. Each account has one board.
2. **Warm-up.** The account fills that board with roughly 115 repinned filler pins over
   2–5 weeks.
3. **One bait pin.** The account then posts a single pin of its own: a stolen, mildly
   suggestive photo with a templated title such as `WATCH NOW▶️🔞 10M+ Monthly Views` and
   an empty description.
4. **Link chain.** The pin links to `tinyurl.com/…`, which goes to a signed
   `google.com/url?q=…` redirect (no warning page), which goes to a rotating smartlink
   domain with a per-account `key`.
5. **Domains rotate** over time and differ between batches. Different keys don't
   necessarily mean different operators.

A second, apparently separate scheme uses fashion/anime bait ("Tap the image link to get
the exact look…") that links to `*.netlify.app` tracker pages.

### How they earn (inferred)

The final pages are never opened, so this is inferred from the link structure:

- **Selling clicks.** The free Pinterest traffic is sold to ad networks. A smartlink's `key` is the
  spammer's publisher ID. The network sends each visitor to whichever offer pays best for their
  country and device (dating or cam sign-ups, sweepstakes, fake virus warnings, app installs,
  browser-notification subscriptions) and pays per view or per conversion. Bots and unwanted
  countries are filtered out, which is why the final page sometimes just closes.
- **Rotation.** Domains rotate because they get blocklisted; many keys let one publisher track
  earnings per account or batch, so a single ban costs little.
- **Other link types.** `auctionr.org/4/…` looks like a second ad network's direct link, the
  netlify pages look like an affiliate tracker that records which Pinterest search led to the
  click, and a blogspot page with `utm_campaign` tags probably earns from its own ads.
- **Economics.** Each click is worth very little, but accounts, posting and images cost nothing,
  so volume makes it pay. Reporting the keys and domains to the ad networks can cut the payouts,
  alongside reporting the accounts to Pinterest.

## Requirements

- Python 3.9+
- Optional: [`gallery-dl`](https://github.com/mikf/gallery-dl), to download pin media
  into the archive. Without it, a basic image download is used instead.
- Optional: `pixboards`, to build a local HTML picture board from the archived images.
- Optional, for `probe.py`: [Playwright](https://playwright.dev/python/) with Chromium
  (`pip install playwright && python3 -m playwright install chromium`),
  [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`), and a proxy or VPN.

## Quick start

```sh
python3 pinhunt.py add seeds.txt   # pin/board URLs or IDs, from a file or as arguments
python3 pinhunt.py run             # full procedure (see below)
python3 pinhunt.py status          # quick counts
```

Run commands from the project folder, because outputs are written relative to the current
directory.

## Commands

| command    | what it does                                                                               |
| ---------- | ------------------------------------------------------------------------------------------ |
| `add`      | add pin/board URLs or IDs (arguments or files)                                             |
| `fetch`    | fetch details of pins not fetched yet (`--retry-errors` to retry failures)                 |
| `resolve`  | unwrap outbound links (shorteners, `google.com/url`) without visiting the destination      |
| `profiles` | fetch the poster accounts' profiles                                                        |
| `search`   | search Pinterest for bait phrases from `queries.txt` (`--query` to add more, `--only`)     |
| `queries`  | list the queries file; `--update` adds phrases learned from bait pins (`--dry-run`)        |
| `expand`   | crawl related pins, the posters' boards and created pins, and boards added by hand         |
| `recheck`  | re-fetch known pins to detect removals (`--days N`)                                        |
| `archive`  | save an evidence folder per bait pin (`--accounts`, `--wayback`, `--force`, `--pin ID`)    |
| `pics`     | copy archived images to `archive/_pics` and run `pixboards` there (`--no-board`)           |
| `report`   | rebuild `report.md` and `pins.csv`                                                         |
| `status`   | quick counts, including expand results                                                     |
| `clean`    | delete junk non-numeric IDs (promoted feed items) from the database                        |
| `run`      | the full procedure                                                                         |

Global options: `--db FILE` (default `pinhunt.db`) and `--delay SECONDS`.

### `run`

`run` does: fetch → resolve → profiles → update queries → search → fetch → (expand → fetch)
× rounds → resolve → profiles → update queries → report → archive → pics.

Useful flags:

- `--sample N`: per step, use only N random search queries and N random items from each
  expand list (default 5; `0` = all). Everything not picked waits for a later run.
- `--rounds N`: number of expand rounds (default 2; stops early when nothing new is found).
- `--no-search`, `--no-expand`, `--no-discover`: skip finding new pins.
- `--no-archive`, `--no-pics`, `--archive-accounts`, `--wayback`: control the archive step.
- `--no-update-queries`: don't add learned phrases to `queries.txt`.

## Probing destinations (`probe.py`)

`probe.py` opens the final destinations in a headless browser to record what they actually
serve. Without `--yes-i-know` it only lists what it would visit.

```sh
python3 probe.py                                    # dry run: list targets
python3 probe.py --yes-i-know --max 3               # uses probe_proxy.json
python3 probe.py --yes-i-know --proxy socks5://127.0.0.1:1080 --max 3
python3 probe.py --list                             # stored results
```

- **Proxy settings** live in `probe_proxy.json` (git-ignored, may hold credentials; copy
  `probe_proxy.example.json`): `proxy`, `country`, `locale`, `timezone`. Command-line flags
  override it. `"proxy": "warp"` uses Cloudflare WARP running as a system-wide tunnel (the
  probe checks Cloudflare's trace page and refuses to run if WARP is off). Free public proxies
  are blocked by the ad networks ("Anonymous Proxy detected"); WARP is not. The per-key limit
  counts per proxy, so switching country probes targets again.

- **Targets** come from `pinhunt.db`: one per smartlink key, per netlify tracker host, and per
  other destination URL, interleaved by domain. A target is skipped once it has been probed
  `--per-key` times (default 1); errors, blank pages and "proxy detected" pages don't count. `--domain`, `--key`, `--kind` and `--url` narrow the choice.
- **Sandbox**: the browser runs under bubblewrap with a read-only root, an empty home folder
  and `/tmp`, and only the run's output folder writable, so it can't see the project or the
  database. It needs a proxy (from the settings file or `--proxy`), or an explicit `--direct`.
- **Each visit** starts at the pin's original link (tinyurl → google.com/url → destination,
  like a real click; `--start final` goes straight to the destination) and uses a fresh
  browser context posing as Android Chrome (user agent and client hints, no "HeadlessChrome")
  with a `pinterest.com` referrer. Permissions are denied; downloads, service workers, popups and
  notification or push requests are blocked and logged; nothing is clicked or typed.
  `--locale` / `--timezone` should match the proxy's country.
- **Recorded** in `probes/<run>/<n>/`: `screenshot.png`, `page.html`, `traffic.har.zip`
  and `result.json` (redirect chain, final URL, title, script hosts, URL parameters such as
  `sub_id` / `clickid`, page events). Results also go into the `probes` table and a
  "Destination probes" section of `report.md`. `probes/` is git-ignored.
- `--chromium PATH` uses a different Chromium build if Playwright's own isn't installed.

## Files

| path            | contents                                                           | in git? |
| --------------- | ------------------------------------------------------------------ | ------- |
| `pinhunt.py`    | the tool                                                           | yes     |
| `pinhunt.db`    | SQLite master database; everything else is generated from it       | yes     |
| `seeds.txt`     | starting pin/board URLs                                            | yes     |
| `queries.txt`   | search phrases, one per line, `#` for comments                     | no      |
| `report.md`     | generated report: domains, keys, accounts, batches, timelines      | no      |
| `pins.csv`      | generated table of all pins                                        | no      |
| `probe.py`      | sandboxed browser probe of destinations (opt-in)                   | yes     |
| `archive/`      | evidence folders per pin, `index.md`, and the `_pics` board        | no      |
| `probes/`       | probe screenshots, HAR files and results                           | no      |
| `probe_proxy.json` | probe proxy / locale / timezone (example: `probe_proxy.example.json`) | no   |

Each `archive/pins/<pin_id>/` holds `summary.md`, `pin.json`, `redirect_chain.json`,
`poster_profile.json`, `poster_boards.json`, `page.html`, `media/` and a SHA-256
`manifest.json`.

## Notes

- This uses Pinterest's unofficial web resource API, which changes without notice. The
  related-pins endpoints are the least stable.
- A 403 is usually rate limiting, not a removed pin; a 404 or empty data means removed.
- `database is locked` means another pinhunt process or a database viewer has the file
  open.
- Test changes offline: copy the database, run with `--db copy.db`, and check syntax with
  `python3 -m py_compile pinhunt.py`.
