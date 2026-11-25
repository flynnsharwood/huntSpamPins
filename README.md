# huntSpamPins

A small tool for investigating a network of Pinterest spam accounts that post clickbait
pins linking to ad-network "smartlinks". It finds more of these pins, records how the
accounts and links behave, keeps evidence before Pinterest removes it, and produces a
report that can be used to flag accounts and links for takedown.

Everything is stored in a local SQLite database. The tool uses only the Python standard
library.

## Safety

- **Ad destinations are never opened.** The tool follows URL shorteners only with
  `HEAD` / `GET` without redirects, and decodes `google.com/url?q=…` wrappers offline. The
  final smartlink pages (`/<id>?key=<32 hex>` on throwaway domains) and `*.netlify.app`
  tracker pages are never requested. Don't open them in a browser either.
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

## Requirements

- Python 3.9+
- Optional: [`gallery-dl`](https://github.com/mikf/gallery-dl), to download pin media
  into the archive. Without it, a basic image download is used instead.
- Optional: `pixboards`, to build a local HTML picture board from the archived images.

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

## Files

| path            | contents                                                           | in git? |
| --------------- | ------------------------------------------------------------------ | ------- |
| `pinhunt.py`    | the tool                                                           | yes     |
| `pinhunt.db`    | SQLite master database; everything else is generated from it       | yes     |
| `seeds.txt`     | starting pin/board URLs                                            | yes     |
| `queries.txt`   | search phrases, one per line, `#` for comments                     | no      |
| `report.md`     | generated report: domains, keys, accounts, batches, timelines      | no      |
| `pins.csv`      | generated table of all pins                                        | no      |
| `archive/`      | evidence folders per pin, `index.md`, and the `_pics` board        | no      |

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
