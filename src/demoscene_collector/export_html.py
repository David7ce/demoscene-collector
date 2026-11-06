"""HTML export — a self-contained, searchable gallery of the curated collection.

The page is generated from SQLite like every other export and never edited by
hand. It embeds the selected collection plus the review queue as one JSON
payload and renders it with vanilla JavaScript, so it opens from ``file://``
with no server, CDN or network access.

Every row keeps the same honesty rules as ``catalogue.md``: a verified link is
shown as recorded HTTP evidence, not as proof that a production runs, and
entries without a video or a usable download say so explicitly.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

INCLUDE_STATUSES = ("selected", "needs_review")

STATUS_ORDER = {
    "verified_match": 0,
    "probable_match": 1,
    "needs_review": 2,
    "rejected": 3,
    "unavailable": 4,
}

RELEASE_ORDER = {
    "verified": 0,
    "redirected": 1,
    "landing_ok": 2,
    "broken": 3,
    "inaccessible": 4,
    "blocked": 5,
    "untested": 6,
    "untested_protocol": 7,
}


def _era(year: int | None) -> str:
    if year is None:
        return "unknown"
    if year < 1990:
        return "pre-1990"
    if year < 2000:
        return "1990s"
    if year < 2010:
        return "2000s"
    if year < 2020:
        return "2010s"
    return "2020s"


def _source_url(conn, row: Any) -> str:
    record = conn.execute(
        "SELECT source_url FROM provenance WHERE entity_type = 'production' AND entity_id = ? "
        "AND field_name = 'source_record' LIMIT 1",
        (row["canonical_key"],),
    ).fetchone()
    if record:
        return record["source_url"]
    key = row["canonical_key"]
    if key.startswith("pouet:"):
        return f"https://www.pouet.net/prod.php?which={key.split(':', 1)[1]}"
    return ""


def _counts(conn) -> dict[str, int]:
    def one(sql: str, *params: Any) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return {
        "productions": one("SELECT COUNT(*) FROM productions"),
        "selected": one("SELECT COUNT(*) FROM productions WHERE selection_status = 'selected'"),
        "needs_review": one(
            "SELECT COUNT(*) FROM productions WHERE selection_status = 'needs_review'"
        ),
        "creators": one("SELECT COUNT(*) FROM creators"),
        "releases": one("SELECT COUNT(*) FROM releases"),
        "videos": one("SELECT COUNT(*) FROM videos"),
        "provenance": one("SELECT COUNT(*) FROM provenance"),
    }


def build_entries(conn) -> list[dict[str, Any]]:
    """Return the curated collection as plain, JSON-serialisable rows."""
    placeholders = ",".join("?" for _ in INCLUDE_STATUSES)
    rows = list(conn.execute(
        f"""
        SELECT * FROM productions
         WHERE selection_status IN ({placeholders})
         ORDER BY COALESCE(selection_rank, 1000000), canonical_key
        """,
        INCLUDE_STATUSES,
    ))
    entries: list[dict[str, Any]] = []
    for row in rows:
        pid = int(row["id"])
        videos = [
            {
                "url": v["canonical_url"],
                "status": v["verification_status"],
                "confidence": v["match_confidence"],
                "kind": v["video_kind"],
                "channel": v["channel_name"],
            }
            for v in conn.execute(
                "SELECT * FROM videos WHERE production_id = ? ORDER BY id", (pid,)
            )
        ]
        videos.sort(key=lambda v: STATUS_ORDER.get(v["status"], 99))
        releases = [
            {
                "url": r["download_url"],
                "status": r["verification_status"],
                "kind": r["url_kind"],
                "artifact": r["artifact_type"],
                "file": r["file_name"],
                "size": r["file_size_bytes"],
            }
            for r in conn.execute(
                "SELECT * FROM releases WHERE production_id = ? ORDER BY id", (pid,)
            )
        ]
        releases.sort(key=lambda r: RELEASE_ORDER.get(r["status"], 99))
        entries.append({
            "key": row["canonical_key"],
            "rank": row["selection_rank"],
            "title": row["title"],
            "year": row["release_year"],
            "era": _era(row["release_year"]),
            "type": row["production_type"],
            "platforms": [
                p["canonical_name"]
                for p in conn.execute(
                    "SELECT p.canonical_name FROM production_platforms pp "
                    "JOIN platforms p ON p.id = pp.platform_id "
                    "WHERE pp.production_id = ? ORDER BY p.canonical_name",
                    (pid,),
                )
            ],
            "creators": [
                {"name": c["canonical_name"], "role": c["role"]}
                for c in conn.execute(
                    "SELECT c.canonical_name, pc.role FROM production_creators pc "
                    "JOIN creators c ON c.id = pc.creator_id "
                    "WHERE pc.production_id = ? "
                    "ORDER BY CASE pc.role WHEN 'group' THEN 0 ELSE 1 END, c.canonical_name",
                    (pid,),
                )
            ],
            "description": row["description"],
            "status": row["selection_status"],
            "scores": {
                "curation": row["curation_score"],
                "popularity": row["popularity_score"],
                "significance": row["significance_score"],
            },
            "videos": videos,
            "releases": releases,
            "source_url": _source_url(conn, row),
        })
    return entries


def _payload(entries: list[dict[str, Any]], counts: dict[str, int], generated_at: str) -> str:
    data = {
        "generated_at": generated_at,
        "counts": counts,
        "entries": entries,
    }
    # A literal "</script>" inside a title would close the tag early.
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_page(
    entries: list[dict[str, Any]],
    *,
    counts: dict[str, int],
    generated_at: str,
) -> str:
    payload = _payload(entries, counts, generated_at)
    title = "Demoscene collection — gallery"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f7f7f4;
    --surface: #fcfcfb;
    --text: #14140f;
    --muted: #6b6a63;
    --border: rgba(20,20,15,.12);
    --accent: #227840;
    --accent-soft: #e3f3e7;
    --warn: #8a5a00;
    --bad: #9b2c2c;
    --bad-soft: #fbe9e9;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --page: #0d0d0c;
      --surface: #1a1a18;
      --text: #f0efec;
      --muted: #a3a299;
      --border: rgba(255,255,255,.12);
      --accent: #5fc87f;
      --accent-soft: #12301d;
      --warn: #e0b45c;
      --bad: #f08a8a;
      --bad-soft: #3a1a1a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--page); color: var(--text);
         font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 1200px; margin: 0 auto; padding: 28px 20px 64px; }}
  h1 {{ margin: 0 0 4px; font-size: 26px; letter-spacing: -.02em; }}
  .sub {{ color: var(--muted); margin: 0 0 22px; }}
  .sub code {{ background: var(--surface); border: 1px solid var(--border);
               padding: 1px 5px; border-radius: 5px; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 20px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border);
           border-radius: 8px; padding: 6px 10px; font-size: 12px; color: var(--muted); }}
  .stat b {{ color: var(--text); font-size: 14px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
  .controls input, .controls select {{
    background: var(--surface); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 7px 9px; font: inherit;
  }}
  .controls input {{ flex: 1 1 260px; min-width: 180px; }}
  .controls label.toggle {{
    display: flex; align-items: center; gap: 7px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 7px; padding: 7px 10px;
    color: var(--muted); font-size: 13px; white-space: nowrap; cursor: pointer;
    flex: 0 0 auto;
  }}
  .controls label.toggle input {{ flex: none; width: 16px; min-width: 0; margin: 0; }}
  .controls label.range {{
    display: flex; align-items: center; gap: 6px; background: var(--surface);
    border: 1px solid var(--border); border-radius: 7px; padding: 6px 9px;
    color: var(--muted); font-size: 13px; white-space: nowrap;
  }}
  .controls label.range input {{
    flex: none; width: 5.4em; min-width: 0; padding: 3px 5px; text-align: right;
  }}
  .controls button.reset {{
    background: var(--surface); color: var(--muted); border: 1px solid var(--border);
    border-radius: 7px; padding: 7px 11px; font: inherit; font-size: 13px; cursor: pointer;
  }}
  .controls button.reset:hover {{ color: var(--text); }}
  th[data-key] {{ cursor: pointer; user-select: none; }}
  th[data-key]:hover, th[data-key]:focus-visible {{ color: var(--text); }}
  th .arrow {{ display: inline-block; width: 1.1em; font-size: 10px; color: var(--muted); }}
  th[aria-sort="ascending"] .arrow, th[aria-sort="descending"] .arrow {{ color: var(--text); }}
  .small {{ font-size: 12px; }}
  .meta {{ color: var(--muted); font-size: 12px; margin: 0 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface);
           border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 9px 11px; border-bottom: 1px solid var(--border);
            vertical-align: top; }}
  th {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
        color: var(--muted); cursor: pointer; user-select: none; white-space: nowrap; }}
  tbody tr:hover {{ background: color-mix(in srgb, var(--accent) 6%, transparent); }}
  tbody tr {{ cursor: pointer; }}
  .rank {{ color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .name {{ font-weight: 600; }}
  .name a {{ color: inherit; }}
  .dim {{ color: var(--muted); }}
  .num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .badge {{ display: inline-block; padding: 1px 6px; border-radius: 999px; font-size: 11px;
            border: 1px solid var(--border); white-space: nowrap; }}
  .ok {{ color: var(--accent); background: var(--accent-soft); border-color: transparent; }}
  .warn {{ color: var(--warn); }}
  .bad {{ color: var(--bad); background: var(--bad-soft); border-color: transparent; }}
  .details td {{ background: color-mix(in srgb, var(--surface) 70%, var(--page)); }}
  .details ul {{ margin: 4px 0 8px; padding-left: 18px; }}
  .details li {{ margin: 2px 0; }}
  .details .about {{ max-width: 90ch; }}
  footer {{ margin-top: 18px; color: var(--muted); font-size: 12px; }}
  .empty {{ padding: 28px; text-align: center; color: var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>Demoscene collection — gallery</h1>
  <p class="sub">Generated at <code>{html.escape(generated_at)}</code> from
    <code>data/demoscene.sqlite3</code> — do not edit by hand. Links are recorded
    HTTP evidence, not proof that a production runs.</p>
  <div class="stats">
    <span class="stat"><b>{counts['selected']}</b> selected</span>
    <span class="stat"><b>{counts['needs_review']}</b> needs review</span>
    <span class="stat"><b>{counts['productions']:,}</b> candidates</span>
    <span class="stat"><b>{counts['creators']:,}</b> creators</span>
    <span class="stat"><b>{counts['releases']:,}</b> release links</span>
    <span class="stat"><b>{counts['videos']:,}</b> video links</span>
  </div>
  <div class="controls">
    <input id="search" type="search" placeholder="Search title, creator, platform…"
           autocomplete="off" aria-label="Search">
    <select id="era" aria-label="Filter by era"><option value="">All eras</option></select>
    <select id="type" aria-label="Filter by type"><option value="">All types</option></select>
    <select id="platform" aria-label="Filter by platform">
      <option value="">All platforms</option>
    </select>
    <label class="range" title="Inclusive release-year range; leave either side empty">
      Years
      <input id="year-from" type="number" inputmode="numeric" placeholder="from"
             min="1900" max="2100" aria-label="Year from">
      –
      <input id="year-to" type="number" inputmode="numeric" placeholder="to"
             min="1900" max="2100" aria-label="Year to">
    </label>
    <label class="toggle" title="Also show ambiguous or unranked entries">
      <input id="review" type="checkbox"> Include review queue
      (<span id="review-count"></span>)
    </label>
    <button id="reset" class="reset" type="button">Reset</button>
  </div>
  <p class="meta" id="meta"></p>
  <table>
    <thead>
      <tr>
        <th data-key="rank" tabindex="0">#<span class="arrow"></span></th>
        <th data-key="year" tabindex="0">Year<span class="arrow"></span></th>
        <th data-key="title" tabindex="0">Production<span class="arrow"></span></th>
        <th data-key="platform" tabindex="0">Platform<span class="arrow"></span></th>
        <th data-key="creators" tabindex="0">Creators<span class="arrow"></span></th>
        <th data-key="score" tabindex="0">Score<span class="arrow"></span></th>
        <th data-key="video" tabindex="0">Video<span class="arrow"></span></th>
        <th data-key="download" tabindex="0">Download<span class="arrow"></span></th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <footer>Row click opens links and the recorded description. Generated by
    demoscene-collector; the Markdown catalogue and CSV/JSON exports carry the
    same population.</footer>
</main>
<script id="data" type="application/json">{payload}</script>
<script>
(function () {{
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const rows = document.getElementById('rows');
  const meta = document.getElementById('meta');
  const search = document.getElementById('search');
  const era = document.getElementById('era');
  const type = document.getElementById('type');
  const platform = document.getElementById('platform');
  const yearFrom = document.getElementById('year-from');
  const yearTo = document.getElementById('year-to');
  const review = document.getElementById('review');
  const reviewCount = document.getElementById('review-count');
  const reset = document.getElementById('reset');
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[c]));

  const selected = DATA.entries.filter((e) => e.status === 'selected');
  const reviewTotal = DATA.entries.length - selected.length;
  reviewCount.textContent = reviewTotal.toLocaleString();

  const eras = [...new Set(DATA.entries.map((e) => e.era))].sort();
  const types = [...new Set(DATA.entries.map((e) => e.type))].sort();
  const platforms = [...new Set(DATA.entries.flatMap((e) => e.platforms))].sort();
  for (const value of eras) era.add(new Option(value, value));
  for (const value of types) type.add(new Option(value, value));
  for (const value of platforms) platform.add(new Option(value, value));

  const scoreOf = (e) => (e.scores && e.scores.curation != null) ? e.scores.curation : -1;
  const videoOf = (e) => e.videos[0] || null;
  const releaseOf = (e) => e.releases.find((r) => ['verified','redirected','landing_ok'].includes(r.status)) || null;
  const STATUS_RANK = {{
    verified_match: 0, verified: 0, redirected: 1, probable_match: 2, landing_ok: 3,
    needs_review: 4, untested: 5, untested_protocol: 6, broken: 7, inaccessible: 8,
    blocked: 9, unavailable: 10
  }};
  const statusRank = (s) => (s in STATUS_RANK ? STATUS_RANK[s] : 99);
  const videoRank = (e) => e.videos.length ? statusRank(e.videos[0].status) : 99;
  const downloadRank = (e) => e.releases.length ? statusRank(e.releases[0].status) : 99;
  const platformText = (e) => e.platforms.join(', ');
  const creatorText = (e) => e.creators.map((c) => c.name).join(', ');

  const sortState = {{ key: 'rank', dir: 1 }};
  const COMPARATORS = {{
    rank: (a, b) => (a.rank || 1e9) - (b.rank || 1e9),
    year: (a, b) => (a.year || 0) - (b.year || 0),
    title: (a, b) => a.title.localeCompare(b.title),
    platform: (a, b) => platformText(a).localeCompare(platformText(b)),
    creators: (a, b) => creatorText(a).localeCompare(creatorText(b)),
    score: (a, b) => scoreOf(a) - scoreOf(b),
    video: (a, b) => videoRank(a) - videoRank(b),
    download: (a, b) => downloadRank(a) - downloadRank(b)
  }};

  function sortList(list) {{
    const cmp = COMPARATORS[sortState.key] || COMPARATORS.rank;
    list.sort((a, b) => {{
      const result = cmp(a, b) * sortState.dir;
      return result || (a.rank || 1e9) - (b.rank || 1e9)
        || a.title.localeCompare(b.title);
    }});
  }}

  function updateHeaders() {{
    for (const th of document.querySelectorAll('th[data-key]')) {{
      const active = th.dataset.key === sortState.key;
      th.setAttribute('aria-sort', active
        ? (sortState.dir === 1 ? 'ascending' : 'descending') : 'none');
      th.querySelector('.arrow').textContent =
        active ? (sortState.dir === 1 ? '▲' : '▼') : '';
    }}
  }}

  for (const th of document.querySelectorAll('th[data-key]')) {{
    const activate = () => {{
      const key = th.dataset.key;
      if (sortState.key === key) sortState.dir *= -1;
      else {{ sortState.key = key; sortState.dir = 1; }}
      updateHeaders();
      render();
    }};
    th.addEventListener('click', activate);
    th.addEventListener('keydown', (ev) => {{
      if (ev.key === 'Enter' || ev.key === ' ') {{
        ev.preventDefault();
        activate();
      }}
    }});
  }}

  function badge(status) {{
    const cls = ['verified_match','verified','redirected'].includes(status) ? 'ok'
      : ['needs_review','probable_match','landing_ok','untested','untested_protocol'].includes(status) ? 'warn'
      : 'bad';
    return '<span class="badge ' + cls + '">' + esc(status) + '</span>';
  }}

  function matches(e, q) {{
    if (!q) return true;
    const hay = [e.title, e.key, e.type, e.platforms.join(' '),
                 e.creators.map((c) => c.name).join(' ')].join(' ').toLowerCase();
    return hay.includes(q);
  }}

  function detailFor(e) {{
    const detail = document.createElement('tr');
    detail.className = 'details';
    detail.hidden = true;
    const parts = ['<td colspan="8">'];
    if (e.description) parts.push('<div class="about">' + esc(e.description) + '</div>');
    if (e.source_url) parts.push('<div><a href="' + esc(e.source_url) + '" target="_blank" rel="noopener">source: ' + esc(e.key) + '</a></div>');
    if (e.videos.length) {{
      parts.push('<div><b>Video</b><ul>' + e.videos.map((v) =>
        '<li><a href="' + esc(v.url) + '" target="_blank" rel="noopener">' + esc(v.url) + '</a> — ' +
        badge(v.status) + ' ' + (v.channel ? esc(v.channel) : '') + '</li>').join('') + '</ul></div>');
    }} else parts.push('<div class="dim">No video linked.</div>');
    if (e.releases.length) {{
      parts.push('<div><b>Releases</b><ul>' + e.releases.map((r) =>
        '<li><a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.url) + '</a> — ' +
        badge(r.status) + ' ' + esc(r.artifact || '') + (r.file ? ' (' + esc(r.file) + ')' : '') + '</li>').join('') + '</ul></div>');
    }} else parts.push('<div class="dim">No release link recorded.</div>');
    parts.push('</td>');
    detail.innerHTML = parts.join('');
    return detail;
  }}

  function render() {{
    const q = search.value.trim().toLowerCase();
    const showReview = review.checked;
    const from = yearFrom.value === '' ? null : Number(yearFrom.value);
    const to = yearTo.value === '' ? null : Number(yearTo.value);
    let list = DATA.entries.filter((e) =>
      (showReview || e.status === 'selected') &&
      (!era.value || e.era === era.value) &&
      (!type.value || e.type === type.value) &&
      (!platform.value || e.platforms.includes(platform.value)) &&
      (from === null || (e.year || 0) >= from) &&
      (to === null || (e.year || 0) <= to) &&
      matches(e, q));
    sortList(list);

    rows.textContent = '';
    const fragment = document.createDocumentFragment();
    for (const e of list) {{
      const tr = document.createElement('tr');
      const names = e.creators.map((c) => c.name);
      const creators = names.slice(0, 2).join(', ')
        + (names.length > 2 ? ' +' + (names.length - 2) : '');
      const video = videoOf(e);
      const release = releaseOf(e);
      const score = scoreOf(e) < 0 ? '—' : scoreOf(e).toFixed(3);
      const reviewTag = e.status === 'needs_review' ? ' · needs review' : '';
      tr.innerHTML =
        '<td class="rank">' + (e.rank || '—') + '</td>' +
        '<td class="num">' + (e.year || '?') + '</td>' +
        '<td><div class="name">' + esc(e.title) + '</div>' +
        '<div class="dim small">' + esc(e.type) + reviewTag + '</div></td>' +
        '<td>' + esc(e.platforms.join(', ') || 'unknown') + '</td>' +
        '<td class="dim" title="' + esc(names.join(', ')) + '">' + esc(creators || 'unknown') + '</td>' +
        '<td class="num">' + score + '</td>' +
        '<td>' + (video ? badge(video.status) : '<span class="dim">none</span>') + '</td>' +
        '<td>' + (release ? badge(release.status) : '<span class="dim">none</span>') + '</td>';
      fragment.appendChild(tr);

      let detail = null;
      tr.addEventListener('click', () => {{
        if (!detail) {{
          detail = detailFor(e);
          tr.after(detail);
        }}
        detail.hidden = !detail.hidden;
      }});
    }}
    rows.appendChild(fragment);

    const bits = [list.length.toLocaleString() + ' shown'];
    if (!showReview) bits.push(reviewTotal.toLocaleString() + ' review entries hidden');
    if (from !== null || to !== null) {{
      bits.push('years ' + (from === null ? '…' : from) + '–' + (to === null ? '…' : to));
    }}
    meta.textContent = bits.join(' · ');
  }}

  search.addEventListener('input', render);
  era.addEventListener('change', render);
  type.addEventListener('change', render);
  platform.addEventListener('change', render);
  yearFrom.addEventListener('input', render);
  yearTo.addEventListener('input', render);
  review.addEventListener('change', render);
  reset.addEventListener('click', () => {{
    search.value = '';
    era.value = '';
    type.value = '';
    platform.value = '';
    yearFrom.value = '';
    yearTo.value = '';
    review.checked = false;
    sortState.key = 'rank';
    sortState.dir = 1;
    updateHeaders();
    render();
  }});
  updateHeaders();
  render();
  render();
}})();
</script>
</body>
</html>
"""


def write_gallery(conn, path: Path, *, generated_at: str) -> dict[str, Any]:
    entries = build_entries(conn)
    counts = _counts(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(entries, counts=counts, generated_at=generated_at),
        encoding="utf-8",
    )
    return {"path": str(path), "entries": len(entries), "counts": counts}


def export_html(conn, exports_dir: Path, *, generated_at: str) -> dict[str, Any]:
    return write_gallery(conn, Path(exports_dir) / "index.html", generated_at=generated_at)
