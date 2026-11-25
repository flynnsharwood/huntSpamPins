# Pinterest smartlink-spam investigation

Personal investigation into a network of Pinterest accounts that post clickbait pins
linking to ad-network "smartlinks". Goal: understand how it works, find more pins,
archive evidence before Pinterest deletes it, and report accounts/links for takedown.

## Safety rules (always follow)

- **Never request the final ad/smartlink destinations** (e.g. walkingdrunkard.com,
  depthschokedirected.com, effectivecpmnetwork.com, or any `/<random>?key=<32 hex>` URL),
  and never open them in a browser. Only follow shorteners (tinyurl etc.) with
  HEAD/GET-without-redirect, and decode `google.com/url?q=...` offline.
- Netlify tracker pages (`*.netlify.app/kampid.../pid.../`) are destinations too: don't fetch.
- Pinterest pages, the Pinterest resource API, and `i.pinimg.com` / `v.pinimg.com` media are fine.
- The archive contains stolen photos of real people. Keep it private; never publish images.
  Reports to platforms should use text summaries and links, not images.
- Be gentle with Pinterest: keep request delays (default 1.5 s) and back-off on 403/429.

## How the scheme works (findings so far)

1. Accounts are created in batches from name templates: `[firstname][word][batch number]`,
   e.g. `sageblue391504` + `anastasiablue391504`, `stephaniedoll92473` + `amanidoll92473`.
   Some use non-template names (`indyjanaam`).
2. Each account has 1 board and is "warmed up" with ~113-120 filler/repinned pins
   for 2-5 weeks (the `doll` series posted exactly 14 days after creation).
3. Then one bait pin is created (`native_creator` = the account, not a repin), often on a
   board made minutes earlier. Stolen, mildly suggestive image; title template
   `WATCH NOW▶️🔞 10M+ Monthly Views` (also `▶` without VS16, `▶️WATCH VIDEO▶️🥵`,
   `FULL BOLD WOMAN CLICK ON SITE FOR ENJOY…`, board-name bait like `I can't wait 🌙 open Now`);
   empty description.
4. Link chain: `tinyurl.com/xxxx` → 301 → `google.com/url?q=...&sa=D&sntz=1&usg=...`
   (Google Sites-style signed redirect; returns 200 with meta-refresh + `location.replace`,
   no warning page) → throwaway smartlink domain `/<8-10 char id>?key=<32 hex>`.
5. Each account gets its own `key` (per-account tracking; one publisher can own many keys,
   so different keys ≠ different operators). Domains rotate over time and differ by batch:
   `blue391504` → depthschokedirected.com; `doll92473`/indyjanaam → effectivecpmnetwork.com
   then walkingdrunkard.com.
6. A separate scheme (likely different operator): fashion/anime bait ("Tap the image link to
   get the exact look…") linking to `*.netlify.app` tracker URLs.
7. When clicked directly, walkingdrunkard.com "opens and closes" - likely geo/bot/referrer
   filtering or the user's ad blocker; the redirect chain up to it works.

## Findings from report of 2026-11-25 (332 pins, 41 posters, 17 domains, 104 keys)

Corrections to the list above: accounts do NOT post one bait pin, and keys are NOT one per account.

- **Waves**: farm accounts post 4-16 bait pins within 1-2 minutes, then another wave ~5 days later
  (stephaniedoll 09-21 ×11 / 09-27 ×16; amanidoll 10-20 ×11 / 10-25 ×12; amirahnora 10-25 ×11 /
  10-30 ×10). jadeclara7425 + lydaclara7425 posted waves minutes apart on 11-01 ~04:00 and 11-05
  ~18:30 UTC → one scheduler. First bait wave 5-28 days after account creation (mostly 14-20).
  Busiest UTC hours: 15, 18, 21.
- **Clusters by shared smartlink key / auctionr id** (strongest attribution signal):
  blue (sageblue, anastasiablue, evieblue `391504`) · janaam (indyjanaam, neriahjanaam) ·
  doll (stephaniedoll, amanidoll `92473`) · clara (jadeclara, lydaclara `7425`) · nora (amirahnora).
  No key overlap *between* these clusters → separate campaigns.
- Name families beyond numeric suffixes: `*janaam`, `*nora`/`theanora4143`, `*clara`, `*blue`,
  `*doll`, `neriah*`; several accounts created the same day (2026-10-24: neriah8742, ariaangel7612,
  neriahjanaam, indyjanaam).
- **21 aged accounts (2012-2022, thousands of pins, 1 bait pin each, 0 repins)** share keys with farm
  pins (tessiefish↔blue, carlosochoabram/passionatedesir↔janaam, arjusiris↔amirahnora).
  **VERIFIED 2026-11-25 (DB query): all 21 bait pins from pre-2026 accounts have is_repin=1;
  13 have native_creator = a different (farm) account, 8 have no native_creator at all. All 299
  bait pins from 2026 accounts are originals (is_repin=0, native_creator = pinner).**
  They are savers (bystanders / save-everything users), NOT operators and NOT hijacked. Do not
  report them. The report now attributes by native_creator: **33 operator accounts** (20 with their
  own bait pins in the DB + 13 known only through savers' copies) and **21 savers**.
- **Operators found through saved copies** (2026-11-25): the 13 native creators are classic farm
  accounts - created 2026-07-12…10-16, 110-234 pins, 1-2 boards. They add new families
  `*bulbul` (lillianabulbul, matidabulbul, michellebulbul19233), `*juliat58396n24` (arielle-,
  isabela-, lyric-), `*luna` (wynterluna2526, lakelynnluna), and join known clusters:
  aryablue → blue (shared keys), makenziejanaam → janaam, lakelynnluna → amirahnora (shared
  auctionr id). lillianabulbul, matidabulbul, michellebulbul19233 and evienora4143 were all
  created 2026-09-29 (across two name families). *(Since crawled - see the 821-pin section below;
  12 of 13 now have their own bait pins in the DB.)* **enzolaith5189 profile is `removed`** -
  the first removal seen.
- The 8 savers with no native_creator (tessiefish, carlosochoabram, prasadmantri921,
  sandralaurent78, dhimangshu21, prosenjitmondal818, joshtreebrond, alycomm1) carry bait whose
  original creator is unknown (original probably deleted).
- Netlify operator lisandra_mitchell6085 posts on a schedule: after the 42-pin burst on 11-11,
  waves of 2-5 pins every ~2-10 h, starting at :00-:05 or :30-:33 past the hour → automated poster.
- Synchronised waves (report "Waves" section): jadeclara7425 + lydaclara7425 (11-01 03:57), and
  berkley66600 + neriahjanaam both at 11-13 14:31 (different link schemes - may be coincidence).
- podnova decoded offline: `twitter.podnova.com/go/?url=` → `yuiuuyttyikkoi.blogspot.com/2026/11/
  <post>.html?utm_campaign=fair6|masti46|masti47&utm_source=google|tumblr` (not fetched).
- Warm-up recomputed from the operator's account (savers excluded): n=263, median 16.9 days,
  157 of 263 pins at 14-20 days, max 124.8 (kaylanicora).
- Link ecosystems: (a) `?key=` smartlink domains with 3-word names (depthschokedirected,
  walkingdrunkard, surelydecoratedfundraiser, effectivecpmnetwork, cursecrookadvantage, revolthem,
  thorpejoy, turnstilesocially, industriousslowly, formssternlystately, motortape, kangaroolame);
  (b) `auctionr.org/4/<22 hex>` - second network, mixed into the same waves;
  (c) netlify trackers on 3 subdomains (lisandra_mitchell6085: 96 pins, 42 in 7 min;
  abilenehahn6391o; birgitoertel4823) with shuffled path segments `kampid`, `strtjid`, `i#####`,
  `q<pinterest search term>` → SEO-targeted at Pinterest searches;
  (d) `twitter.podnova.com/go/` (berkley66600) - probable third-party open redirect; the
  destination is an unencoded URL in `?url=` (decoded, see above).
- Search saturation (corrected 2026-11-25 from the `searches` log): emoji / fancy-unicode queries
  DO return results (18 of 39, ~6800 results) but added only 7 new pins; plain-text queries
  (18 of 26 with results, ~7100 results) added 139. Only "Tap the image link to get the exact
  look" was really productive (129, netlify scheme). Emoji only add cost → plain-text queries only.
- As of 2026-11-25 no pin had been removed by Pinterest; one operator profile (enzolaith5189) is
  gone.

## Findings after expand of the 18 uncrawled operators (2026-11-25, 821 pins, 53 posters, 17 domains, 220 keys)

- `expand --sample 18` crawled all 18 uncrawled operators: +488 pins (abilenehahn6391o 124,
  birgitoertel4823 100, others 5-37 each). Related pins and boards added **0** new pins → the
  operators' own created-pins feeds are where new bait comes from, not related-pins.
- Totals now 808 bait pins (772 with link), 595 links, still **17 final domains** (no new domains),
  220 keys, 33 operators + 21 savers (unchanged - no new accounts, only more pins per account).
  113 waves of ≥2 pins. Warm-up n=751, median 18.9 days, 397 of 751 at 14-20 days.
- Of the 13 operators known only through saved copies, 12 now have their own bait pins in the DB;
  only **enzolaith5189 (removed)** remains known only via a saver (finanzalex, formssternlystately.com).
- **Clusters (shared key / auctionr id / tinyurl / netlify host / image), 8 multi-account** (9 since task 10: images no longer merge, so janaam and clara are separate):
  janaam+clara (indyjanaam, neriahjanaam, makenziejanaam, jadeclara7425, lydaclara7425; 68 pins) ·
  blue (sageblue, anastasiablue, evieblue `391504`, aryablue; 50) ·
  nora (evienora4143, theanora4143, kaylanicora; 27, surelydecoratedfundraiser.com) ·
  bulbul (lillianabulbul, matidabulbul, michellebulbul19233; 72) · doll (50) ·
  amirahnora + lakelynnluna (35, shared auctionr ids) ·
  juliat (isabelajuliat58396n24, lyricjuliat58396n24; 70) · izabellajuna + wynterluna2526 (21).
  10 operators share no signal with anyone (incl. ariellejuliat58396n24, which only shares the name
  family with the juliat pair, and all 3 netlify operators).
- **The janaam + clara merge is only through one shared image signature**
  (`3e2e4646ec80feae7e753ba034a6f2d6`, jadeclara7425 + makenziejanaam), not a key, auctionr id or
  tinyurl. It may be the same operator or just the same stolen photo reused. **Key-based finding
  still holds: no key / auctionr overlap between clusters.** Treat image-only links as weak.
- Same domains across clusters: bulbul, juliat and blue all use depthschokedirected.com +
  auctionr.org (separate keys); nora, clara, izabellajuna/wynterluna use surelydecoratedfundraiser.com.
  Shared domain ≠ shared operator (the ad network owns the domain), but it fits one publisher
  running several farms.
- The juliat accounts are the oldest farm operators seen (created 2026-07-12…20; first bait
  2026-08-20), kaylanicora created 2026-07-11 (warm-up max 124.8 days).
- **Synchronised waves (one scheduler)** - new: aryablue + sageblue391504 at 09-30 07:26 and again
  10-06 18:08 (confirms aryablue in the blue farm); bulbul trio at 10-15 05:40 (matida + michelle)
  and all three at 10-21 17:19-17:20. Still: jadeclara + lydaclara 11-01 03:57; berkley66600 +
  neriahjanaam 11-13 14:31 (different link schemes - probably coincidence).
- **Netlify operators all run the same automated schedule**: abilenehahn6391o (125 pins, one
  netlify host) and birgitoertel4823 (101 pins, another host) each start with a big burst
  (50 in 7 min on 11-14; 49 in 34 min on 11-16), then waves of 2-5 pins every ~4-8 h starting at
  :00-:03 or :30-:34 - same pattern as lisandra_mitchell6085 → probably one netlify operator with
  one host per account, despite no shared signal.

## Open tasks / ideas

1. [x] Attribute pins by `native_creator` (fallback pinner); separate "savers" (is_repin=1) from
   operators in report tables and exclude them from warm-up stats and from `expand` crawling.
   Add every native_creator of a saved bait pin to `users` so it gets fetched/crawled; add a report
   section "operators known only through saved copies" (native_creator removed or not crawlable).
   *Done 2026-11-25: `attribute()`; saver = repin, or account ≥365 days older than its only bait
   pin. Native creators are added to `users` by `fetch` and by a migration in `DB.__init__`.*
2. [x] Report section: clusters = connected components of accounts sharing key / auctionr id / tinyurl /
   image_signature / netlify host; list waves per account (pins ≤15 min apart).
   *Done: `clusters()`, `waves()` + synchronised-wave list.*
3. [x] Name-family batch detection (shared stem or same creation date), not only numeric suffix.
   *Done: `name_families()` (numeric suffix, shared suffix ≥4 / prefix ≥5 letters, same creation day).*
4. [x] `unwrap()`: decode any URL embedded in query params offline (e.g. podnova `/go/?url=`).
   *Done: `decode_embedded()`; existing links updated offline with `resolve --reclassify`.*
5. [x] `queries --prune` (drop queries with 0 results after ≥2 runs); `derive_queries` should add only
   plain-text cores, no emoji variants. *Done; pruned queries stay as `# pruned` comments so
   `--update` won't re-add them. Not run on the real queries.txt yet (dry run: 20 would go).*
6. [x] Don't crawl huge saver accounts in `expand` (skip users with pin_count > ~5000).
   *Done: `--max-user-pins` (default 5000); expand now crawls operators (native_creator), not pinners.*
7. Discovery beyond search: crawl the farm boards' followers / repinners of high-repin bait pins.
8. Convert the remaining emoji queries in queries.txt to their plain-text cores (most return
   results, so `--prune` keeps them, but they add almost no new pins).
9. [x] Next network run: `profiles` + `expand` to crawl the 13 operators found via saved copies.
   *Done 2026-11-25: expand of all 18 uncrawled operators, then fetch + resolve (821 pins).*
10. [x] `clusters()`: image signatures alone merged janaam + clara. Make image a weak signal.
   *Done: `WEAK_SIGNALS = {"image"}` - images no longer merge clusters; they are counted inside a
   cluster, and image-only links between clusters are listed under "Weak links between clusters".
   Result: 9 clusters, janaam (3) and clara (2) separate again, joined by 1 weak image link.*

## Project files

- `pinhunt.py` - the tool (Python stdlib only; optional `gallery-dl` for media).
- `pinhunt.db` - SQLite master database (WAL mode). **Back this up; everything else is derived.**
- `seeds.txt` - original pin/board URLs found manually.
- `queries.txt` - search phrases, one per line, `#` comments; auto-appended by `queries --update`.
- `report.md`, `pins.csv` - regenerated by `report` / `run` / `recheck`.
- `archive/` - evidence: `index.md`, `pins/<pin_id>/{summary.md, pin.json, redirect_chain.json,
  poster_profile.json, poster_boards.json, page.html, media/, gallery-dl.log, manifest.json}`,
  optional `accounts/<username>/`.

## pinhunt.py structure

Commands: `add`, `fetch [--retry-errors]`, `resolve [--force | --reclassify]`, `profiles`,
`search [--queries F --sample N]`, `queries [--update --prune --min-runs N --dry-run --min-count N]`,
`expand [--sample N --max-user-pins N]`, `recheck --days N`,
`archive [--accounts --wayback --force --pin ID]`, `report`, `pics [--no-board]`, `status`, `clean`,
`run [--no-discover --no-search --no-expand --no-archive --no-pics --no-update-queries --rounds N
--sample N --max-user-pins N]`.

`run` = fetch → resolve → profiles → (update queries → search → fetch) → (expand → fetch) ×rounds
→ resolve → profiles → update queries → report → archive → pics.

`resolve --reclassify` is offline: it re-decodes embedded URLs at the end of stored chains and
recomputes `kind`/`key` (no requests). Report attribution: `operator` = native_creator (else the
pinner unless it is a saver), `saver` flag; both are also columns in pins.csv.

DB tables: `pins` (status new/ok/removed/error/invalid, bait flag, raw JSON, archived_at,
archive_dir), `links` (chain, final_domain, key, kind), `users` (profile, crawled, archived_at),
`boards`, `edges` (related-pin graph), `searches` (query log → "query performance" in report).
Schema migrations are done in `DB.__init__` with `ALTER TABLE ADD COLUMN` if missing.

Pinterest access uses the unofficial web resource API:
`https://www.pinterest.com/resource/<Resource>/get/?source_url=...&data={"options":...,"context":{}}`
with headers `X-Requested-With: XMLHttpRequest`, `X-Pinterest-PWS-Handler: www/[username].js`.
Resources used: PinResource, UserResource, BoardsResource, BoardResource, BoardFeedResource,
UserActivityPinsResource, BaseSearchResource, RelatedModulesResource / RelatedPinFeedResource
(fallback). Pagination via `bookmark` / `bookmarks`; `-end-` means done.

## Known gotchas

- These endpoints change without notice; related-pins is the least stable.
- 403 often means rate limiting, not removal; 404 / empty data = removed.
- `title` can be a dict, not a string - always go through `txt()`.
- Feeds contain promoted/encoded items with IDs like `e1_Afq...`; only numeric IDs are real pins
  (`PIN_ID_RE`). `clean` deletes junk entries.
- Pin ID top 16 bits (`id >> 46`) = Pinterest shard of the owner, not a timestamp.
- Pinterest `created_at` is RFC 2822; stored as ISO 8601 UTC via `iso()`.
- `database is locked` → another pinhunt process or a DB viewer; DB now uses WAL + 120 s timeout.
- Outputs are written relative to the current working directory - run from the project folder.

## Testing changes

Test offline where possible: copy `pinhunt.db` to a scratch file and use `--db scratch.db`,
or insert mock rows; check with `python3 -m py_compile pinhunt.py`. Avoid running full network
crawls just to test code paths.
