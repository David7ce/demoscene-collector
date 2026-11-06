# Source audit — Demoscene Collection Pipeline

**Access date:** 2026-10-10 (UTC)
**Environment:** Linux, Python 3.14.4, outbound HTTPS available (no proxy), no API keys present in
environment variables.
**Method:** every endpoint below was requested with `curl`/`requests` from this machine on the access
date. No conclusions were carried over from prior knowledge; responses were inspected for schema.

All findings are observations, not assumptions. Failures are recorded as failures.

---

## 1. Demozoo — https://demozoo.org/

| Check | Result |
| --- | --- |
| Reachable from this network | **NO** — connection timeout |
| Official API discovered | Not verifiable from this network |
| API key required | Unknown (could not load documentation) |

Evidence:

```
$ curl -v https://demozoo.org/ --max-time 15
*   Trying 188.114.96.5:443 ...
* Connection timed out after 15001 ms
curl: (28) Connection timed out
```

* DNS resolves (`dns.google/resolve` → `188.114.96.2`, `188.114.97.2`, IPv6 `2a06:98c1:3120::5` …),
  but TCP connections to all returned addresses time out (IPv6: "Network is unreachable").
* `api.demozoo.org` → DNS `NXDOMAIN` (no such subdomain on 2026-10-10).
* Other Cloudflare-hosted sites (cdnjs, www.cloudflare.com) **are** reachable, so this is specific to
  the `demozoo.org` address space / route, not a blanket Cloudflare block.
* `https://demozoo.org/api-docs/` → timeout; Wayback Machine availability API returned
  `archived_snapshots: {}` for that URL (no snapshot), so the API documentation could not be read.
* A web search returned current `demozoo.org` page content (the site itself is alive on the public
  internet), confirming the failure is local to this network path, not a site outage.

**Consequence:** Demozoo cannot be queried as a primary source from this environment. The pipeline
ships a `demozoo` adapter that performs an explicit reachability probe and records a failed
`fetch_jobs` row (and source status `unreachable`) instead of silently skipping it. No Demozoo JSON
schema, endpoint or authentication method is assumed anywhere in the code, because none could be
observed.

**Mitigation:** Pouët records carry a `demozoo` cross-reference ID (observed in the dump, e.g.
`"demozoo": "63820"`), so the Demozoo identity of a production is preserved as provenance and can be
used later if Demozoo becomes reachable.

---

## 2. Pouët — https://www.pouet.net/

| Check | Result |
| --- | --- |
| Reachable | **YES** (HTTP 200) |
| Official API | **YES** — https://api.pouet.net/ (source: github.com/pouetnet/pouet-api) |
| Auth required | **No** — no key, no login for read endpoints |
| Rate limit | **15 requests / 60 s per IP** (from `config.inc.php.dist`: `POUETAPI_RATELIMIT_TIMEFRAME=60`, `POUETAPI_RATELIMIT_REQUESTCOUNT=15`), enforced in `_rewrite.php`, returns `HTTP 429` |
| `robots.txt` | `https://www.pouet.net/robots.txt` → **404** |

### Tested API endpoints (2026-10-10)

| Endpoint | Result | Notes |
| --- | --- | --- |
| `GET https://api.pouet.net/v1/stats` | 200 JSON | `prods_all: 102992`, `youtube: 33902`, … |
| `GET https://api.pouet.net/v1/prod?id=1000` | 200 JSON | full production record (schema below) |
| `GET https://api.pouet.net/v1/search/prod?query=…` | 200 JSON | rewrite rules; parameter is `q`/`query` — an empty query returns `{"error": true}` plus PHP warnings |
| `GET https://api.pouet.net/v1/lists?id=…` | 200 JSON | requires `id`, otherwise `{"error": true}` |
| `GET https://www.pouet.net/api.php` | 404 | legacy endpoint no longer exists |

URL mapping comes from `_rewrite.php` in the API repository: `/v1/prod`, `/v1/prod/comments`,
`/v1/search/prod`, `/v1/group`, `/v1/party`, `/v1/search/party`, `/v1/user`, `/v1/board`,
`/v1/lists`, `/v1/stats`, `/v1/front-page/{latest-added,latest-released,alltime-top,top-of-the-month}`.
All are read-only and keyless.

Observed `/v1/prod` schema (2026-10-10): `id, name, type, addedUser{…}, addedDate, releaseDate,
voteup, votepig, votedown, voteavg, download, party_compo, party_place, party_year, sceneorg,
demozoo, csdb, zxdemo, invitation, invitationyear, boardID, rank, groups[], types[], placings[],
platforms{id:{name,icon,slug}}, awards[], party{…}, cdc, popularity, screenshot, party_compo_name,
downloadLinks[{type,link}], credits[{user{…},role}]`.

**Privacy note:** the API returns user account records (email is not present, but registration date,
permissions and internal IDs are). The pipeline stores only `nickname`, internal user id and credit
role — no other user fields are persisted.

### Data dumps — https://data.pouet.net/

| Check | Result |
| --- | --- |
| Reachable | **YES** (HTTP 200) |
| Format | JSON, gzip/xz compressed, one file per dataset |
| Refresh | "New dump every wednesday morning" (observed: latest `20261007`) |
| Index API | `GET https://data.pouet.net/json.php` → machine-readable list of dump URLs + sizes |
| Auth | **None** |

Datasets: `prods`, `parties`, `groups`, `boards`.

Downloaded on 2026-10-10 into `data/input/`:

| File | Size | Records |
| --- | --- | --- |
| `pouetdatadump-prods-20261007.json.gz` | 15,151,687 B | 102,973 productions |
| `pouetdatadump-groups-20261007.json.gz` | 404,675 B | groups |
| `pouetdatadump-parties-20261007.json.gz` | 36,272 B | parties |

Observed dump schema: `{"dump_date": "2026-10-07 04:30:01", "prods": [ … ]}`. Each production has
`id, name, type, types[], releaseDate (YYYY-MM-DD or null), voteup/votedown/voteavg, rank,
popularity, download (primary release URL), downloadLinks[{type,link}], groups[], platforms{},
placings[{party,compo,ranking,year,compo_name}], awards[], party{…}, credits[{user,role}],
screenshot, sceneorg, demozoo, csdb, zxdemo`.

Observed statistics used for planning:

* `types`: demo 32,936 · cracktro 11,323 · intro 10,071 · 4k 6,147 · 64k 3,656 · 8k 1,589 · wild 3,132 …
* `platforms`: Commodore 66 22,489 · Amiga OCS/ECS 18,893 · MS-Dos 17,449 · Windows 12,181 ·
  Atari ST 9,992 · Amiga AGA 4,438 …
* 97,729 of 102,973 productions have a `releaseDate`; 9,244 have none.
* 30,662 productions carry at least one YouTube link (`youtube`/`Youtube`/`YouTube`/`youtube(live)`).
* 1,822 carry a `video`-type link (e.g. `.avi`/`.mp4` captures).
* 1,018 carry `awards`; 37,973 have a party record; `placings` cover competition results.
* Primary `download` hosts: `files.scene.org` 42,938 · `csdb.dk` 18,813 · `ftp.untergrund.net`
  11,681 · `ftp.amigascne.org` 11,044 · `no-fragments.atari.org` 1,843 …

**Selected strategy:** the Pouët data dump is the **primary discovery source** (published dataset >
API > scraping), with the Pouët API used for optional detail lookups under a 15 req/min budget.

---

## 3. Pouët API repository — https://github.com/pouetnet/pouet-api

* Reachable (raw.githubusercontent.com, api.github.com → 200).
* Files inspected: `README.md`, `_rewrite.php`, `v1_prod.php`, `config.inc.php.dist`.
* Confirms: read-only endpoints, no authentication, per-IP rate limiting (15/60 s), no documented
  pagination on `/v1/prod` (single record by `id`, or `?random=1`).
* No licence file granting reuse of the API *code* was found (there is a `LICENSE` file; code reuse
  was not required — we only consume the JSON API).

---

## 4. Scene.org file archive — https://files.scene.org/

| Check | Result |
| --- | --- |
| Reachable | **YES** (HTTP 200) |
| API | **None** — PHP HTML front-end with a file browser, search form, RSS |
| `robots.txt` | 404 |
| Terms | Site footer: "Copyright © The International Scene Organization ry. All rights reserved." |

Observed URL model (2026-10-10, verified by fetching a real page):

| Kind | URL | Behaviour |
| --- | --- | --- |
| Browse | `https://files.scene.org/browse/<path>/` | HTML directory listing (name/extension/size/date sort) |
| **Landing page** | `https://files.scene.org/view/<path>` | HTML "File download" page with file size, file date, download counts, screenshot |
| **Direct file** | `https://files.scene.org/get/<path>` | `302` → mirror (e.g. `mirror.netcologne.de/scene.org/…`), `Content-Type` of the artifact after redirect |
| Mirrored direct | `https://files.scene.org/get:<cc>-<proto>/<path>` | same, pinned mirror |
| Search | `https://files.scene.org/search/?query=…` | HTML |
| RSS | `/rss/latest-parties/` | RSS |

Test evidence: `HEAD https://files.scene.org/get/demos/groups/tbl/pc/astral.zip` → `302 location:
https://mirror.netcologne.de/scene.org/demos/groups/tbl/pc/astral.zip`; following it yields
`200 application/zip`. `HEAD https://ftp.scene.org/mirrors/hornet/demos/1998/a/astral32.zip` →
`200 application/zip`.

**Consequence:** a Pouët `download` URL of the form `https://files.scene.org/view/…` is a **landing
page**, not a direct artifact link, and must be labelled as such. The `/get/…` form (or a host that
returns a non-HTML `Content-Type`) is treated as a **direct download**. This distinction is encoded
in `normalization.classify_url()`.

**Selected strategy:** used only to (a) classify links Pouët already points at and (b) verify link
liveness with `HEAD`. No bulk spidering of the archive.

---

## 5. CSDb — https://csdb.dk/

| Check | Result |
| --- | --- |
| Reachable | **YES** (HTTP 200) |
| API | **None** observed — HTML site with a search form |
| Auth | Reading works without login ("You are not logged in" banner, results visible) |
| `robots.txt` | Present; disallows `meta-webindexer`, `ExaSearchBot`, `AwarioBot`, `Applebot`, `OAI-SearchBot`, `Claude-SearchBot`, … |

* Search form: `GET https://csdb.dk/search/?query=…` (HTML, ≥2 characters).
* No machine-readable format observed; no sitemap/API discovered in a short inspection.
* Pouët dump records carry a `csdb` numeric ID (e.g. `18813` productions link to csdb.dk), which is
  enough to record cross-source identity **without** scraping.

**Selected strategy:** CSDb is used as a *cross-reference and release-URL host only* (URLs taken from
the Pouët dump). Direct HTML scraping of CSDb pages is **not** performed by the pipeline; if a CSDb
release URL is checked, it is a single polite `HEAD`/`GET` request with our stated User-Agent, never
a crawl. robots.txt entries for named bots do not list generic clients, but we keep access minimal
regardless.

---

## 6. YouTube Data API v3 — https://developers.google.com/youtube/v3

| Check | Result |
| --- | --- |
| Docs reachable | **YES** (HTTP 200) |
| Key required | **YES** — verified: `GET https://www.googleapis.com/youtube/v3/search?…` without a key → `403 PERMISSION_DENIED`, message "Method doesn't allow unregistered callers… Please use API Key" |
| Key available in this environment | **NO** (`YOUTUBE_API_KEY` not set) |
| Quota (documented 2026-10-10) | "Projects that enable the YouTube Data API have a default quota allocation of **100 `search.list` calls**, 100 `videos.insert` calls, and **10,000 units per day** combined for all other endpoints." |
| `search.list` cost | Documented as 100 units/call (i.e. the 100 searches/day default) |
| Terms | Standard Google API Services terms; API data may not be cached beyond 30 days for some fields, attribution/quota rules apply |

**Keyless alternative verified:** the oEmbed endpoint works without any key:

```
GET https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=eZyLSHyUGBY&format=json
→ 200 {"title":"The Black Lotus - Astral Blur (1997) [60fps]","author_name":"DemosceneVids", …}
```

It returns title, channel and thumbnail only — **no duration, no publication date, no statistics**.
This is sufficient to verify that an *explicitly linked* video actually exists and to compare its
title with the production title, but not to enrich metadata further.

**Selected strategy:**

1. Take explicit video links from Pouët production records (30,662 productions already have them —
   no search needed for those).
2. Verify each linked video through `oEmbed` (existence + title comparison) and classify the match.
3. If `YOUTUBE_API_KEY` is set, use `search.list`/`videos.list` for productions still missing a
   video, honouring quota accounting (100 searches/day budget is configurable).
4. Without a key: report "video discovery via search unavailable (no API key); explicit-link
   extraction only". **No HTML scraping of youtube.com and no unofficial invidious endpoints.**

---

## 7. Summary and selected strategy

| Source | Reachable | Machine-readable | Auth | Role in pipeline |
| --- | --- | --- | --- | --- |
| Pouët data dumps | ✅ | JSON dump (102,973 prods) | none | **Primary discovery + metadata + credits + placings + awards + video/release links** |
| Pouët API | ✅ | JSON, 15 req/min | none | Detail lookups / spot checks |
| Scene.org | ✅ | HTML only, clean URL model | none | Landing-vs-direct classification, `HEAD` link verification |
| CSDb | ✅ | HTML only, robots.txt present | none (read) | Cross-reference IDs + release URLs (from Pouët), minimal direct requests |
| Demozoo | ❌ timeout | not observable | not observable | Reachability probe + failure record only; cross-IDs preserved |
| YouTube Data API | ✅ | JSON | **key required** (absent) | Optional search enrichment; disabled without key |
| YouTube oEmbed | ✅ | JSON | none | Video existence + title verification |

**Rationale:** the Pouët dump is a *published dataset* that already aggregates what this project
needs (titles, dates, groups, credits, platforms, competition results, awards, release URLs and
video URLs) for 102,973 productions. Building discovery on it means hundreds of thousands of HTML
page fetches are unnecessary, which is both faster and far more polite to the scene sites.

### Known limitations recorded at audit time

1. Demozoo unreachable from this network → no Demozoo-side metadata, screenshots or download links.
2. No YouTube API key → no *search*-based video discovery; explicit links only (≈30k productions
   already have explicit links; productions without them are reported as `missing_videos`).
3. Pouët API rate limit (15 req/min) → API used sparingly, dumps preferred.
4. CSDb/scene.org have no APIs → only single-request verification, never crawling.
5. FTP URLs (`ftp://…`, 11,681 `ftp.untergrund.net` links) cannot be `HEAD`-checked over HTTP →
   recorded as `untested` (protocol), not as broken.
