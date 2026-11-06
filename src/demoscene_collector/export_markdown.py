"""Markdown exports: overview, catalogue, and the three exception reports.

All files are rebuilt from SQLite — never edited by hand. Output is
deterministic: stable ordering, no timestamps inside list bodies (the
generation time appears once in ``README.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

VIDEO_VERIFIED = {"verified_match"}
VIDEO_USABLE = {"verified_match", "probable_match"}
DOWNLOAD_OK = {"verified", "redirected"}
DOWNLOAD_LANDING = {"landing_ok"}


def md_escape(text: Any) -> str:
    """Escape Markdown control characters in free text."""
    if text is None:
        return ""
    out = str(text)
    for ch in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        out = out.replace(ch, "\\" + ch)
    return out.replace("|", "\\|").replace("\n", " ")


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


def _family(conn, production_id: int) -> str:
    row = conn.execute(
        "SELECT p.platform_family FROM production_platforms pp "
        "JOIN platforms p ON p.id = pp.platform_id WHERE pp.production_id = ? "
        "ORDER BY p.platform_family LIMIT 1",
        (production_id,),
    ).fetchone()
    return row["platform_family"] if row else "Other"


def _creators(conn, production_id: int, roles: tuple[str, ...] | None = None) -> list[str]:
    query = ("SELECT c.canonical_name, pc.role FROM production_creators pc "
             "JOIN creators c ON c.id = pc.creator_id WHERE pc.production_id = ?")
    params: list[Any] = [production_id]
    if roles:
        query += " AND pc.role IN (%s)" % ",".join("?" for _ in roles)
        params.extend(roles)
    query += " ORDER BY CASE pc.role WHEN 'group' THEN 0 ELSE 1 END, c.canonical_name"
    return [r["canonical_name"] for r in conn.execute(query, params)]


def _platforms(conn, production_id: int) -> list[str]:
    return [r["canonical_name"] for r in conn.execute(
        "SELECT p.canonical_name FROM production_platforms pp "
        "JOIN platforms p ON p.id = pp.platform_id WHERE pp.production_id = ? "
        "ORDER BY p.canonical_name",
        (production_id,),
    )]


def _selected_rows(conn) -> list[Any]:
    return list(conn.execute(
        "SELECT * FROM productions WHERE selection_status IN ('selected','needs_review') "
        "ORDER BY COALESCE(selection_rank, 1000000), canonical_key"
    ))


def _entry(conn, row: Any, *, include_scores: bool = True) -> list[str]:
    pid = int(row["id"])
    lines: list[str] = []
    title = md_escape(row["title"])
    year = row["release_year"] or "?"
    rank = row["selection_rank"]
    prefix = f"{rank}. " if rank else "- "
    lines.append(f"{prefix}**{title}** ({year})")
    platforms = ", ".join(md_escape(p) for p in _platforms(conn, pid)) or "unknown"
    creators = ", ".join(md_escape(c) for c in _creators(conn, pid)) or "unknown"
    lines.append(f"  - **Type:** {md_escape(row['production_type'])} · "
                 f"**Platform:** {platforms} · **Creators:** {creators}")
    if row["description"]:
        lines.append(f"  - **About:** {md_escape(row['description'])}")

    videos = list(conn.execute("SELECT * FROM videos WHERE production_id = ? ORDER BY id", (pid,)))
    if videos:
        for vid in videos:
            status = vid["verification_status"]
            confidence = f"{vid['match_confidence']:.2f}" if vid["match_confidence"] is not None else "n/a"
            label = f"{status} (confidence {confidence})"
            lines.append(
                f"  - **Video** [{label}]({vid['canonical_url']}) — "
                f"{md_escape(vid['video_kind'])}"
                + (f", channel {md_escape(vid['channel_name'])}" if vid["channel_name"] else "")
            )
    else:
        lines.append("  - **Video:** none linked")

    releases = list(conn.execute("SELECT * FROM releases WHERE production_id = ? ORDER BY id", (pid,)))
    if releases:
        for rel in releases:
            status = rel["verification_status"]
            kind = rel["url_kind"]
            url = rel["download_url"]
            size = f", {rel['file_size_bytes']} bytes" if rel["file_size_bytes"] else ""
            lines.append(
                f"  - **Release** [{kind} · {status}]({url}) — "
                f"{md_escape(rel['artifact_type'])}"
                + (f" ({md_escape(rel['file_name'])}{size})" if rel["file_name"] else "")
            )
    else:
        lines.append("  - **Release:** no link recorded")

    source_url = _source_url(conn, row)
    if source_url:
        lines.append(f"  - **Source:** [{md_escape(row['canonical_key'])}]({source_url})")
    else:
        lines.append(f"  - **Source:** `{md_escape(row['canonical_key'])}`")
    if include_scores:
        score = row["curation_score"]
        pop = row["popularity_score"]
        sig = row["significance_score"]
        if score is not None:
            lines.append(f"  - **Score:** {score:.4f} "
                         f"(popularity {pop if pop is not None else 'n/a'}, "
                         f"significance {sig if sig is not None else 'n/a'})")
    return lines


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


def write_catalogue(conn, path: Path, *, generated_at: str) -> dict[str, Any]:
    rows = _selected_rows(conn)
    buckets: dict[str, dict[str, list[Any]]] = {}
    for row in rows:
        buckets.setdefault(_era(row["release_year"]), {}).setdefault(
            _family(conn, int(row["id"])), []
        ).append(row)

    era_order = ["pre-1990", "1990s", "2000s", "2010s", "2020s", "unknown"]
    lines: list[str] = [
        "# Demoscene collection — catalogue",
        "",
        f"Generated at {generated_at} from `data/demoscene.sqlite3` — do not edit by hand.",
        "",
        "Hierarchy: **era → platform family → selection rank**. Video confidence and link "
        "verification status are printed with every entry; a checked link only proves the "
        "recorded HTTP evidence, never that a program runs.",
        "",
    ]
    total = 0
    for era in era_order + [e for e in buckets if e not in era_order]:
        if era not in buckets:
            continue
        lines.append(f"## {era}")
        lines.append("")
        families = sorted(buckets[era])
        for family in families:
            lines.append(f"### {family}")
            lines.append("")
            for row in buckets[era][family]:
                lines.extend(_entry(conn, row))
                total += 1
                lines.append("")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"entries": total, "path": str(path)}


def write_missing_videos(conn, path: Path, *, generated_at: str) -> dict[str, Any]:
    rows = _selected_rows(conn)
    none_linked: list[Any] = []
    unverified: list[Any] = []
    needs_review: list[Any] = []
    unavailable: list[Any] = []
    for row in rows:
        videos = list(conn.execute(
            "SELECT * FROM videos WHERE production_id = ?", (int(row["id"]),)))
        if not videos:
            none_linked.append(row)
            continue
        statuses = {v["verification_status"] for v in videos}
        if statuses & VIDEO_USABLE:
            continue
        if "needs_review" in statuses:
            needs_review.append(row)
        elif "unavailable" in statuses:
            unavailable.append(row)
        else:
            unverified.append(row)

    lines = [
        "# Productions without a verified video match",
        "",
        f"Generated at {generated_at}. Counts refer to the selected collection "
        f"({len(rows)} productions).",
        "",
        "| group | count | meaning |",
        "| --- | --- | --- |",
        f"| no video linked | {len(none_linked)} | no video URL found in any source record |",
        f"| linked but unverified | {len(unverified)} | links exist but were not checked yet "
        f"(budget/offline) |",
        f"| ambiguous | {len(needs_review)} | a link exists but titles do not match confidently |",
        f"| unavailable | {len(unavailable)} | the linked video was reported unavailable |",
        "",
    ]
    for title, group in (("No video linked", none_linked),
                         ("Linked but not verified", unverified),
                         ("Ambiguous match (needs review)", needs_review),
                         ("Linked video unavailable", unavailable)):
        if not group:
            continue
        lines.append(f"## {title}")
        lines.append("")
        for row in group:
            lines.append(f"- **{md_escape(row['title'])}** ({row['release_year'] or '?'}) — "
                         f"`{row['canonical_key']}`")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"path": str(path), "none_linked": len(none_linked), "unverified": len(unverified),
            "needs_review": len(needs_review), "unavailable": len(unavailable)}


def write_unavailable_downloads(conn, path: Path, *, generated_at: str) -> dict[str, Any]:
    rows = _selected_rows(conn)
    groups: dict[str, list[Any]] = {}
    for row in rows:
        releases = list(conn.execute(
            "SELECT * FROM releases WHERE production_id = ? ORDER BY id", (int(row["id"]),)))
        if not releases:
            groups.setdefault("no_link_recorded", []).append((row, None))
            continue
        usable = [r for r in releases if r["verification_status"] in (DOWNLOAD_OK | DOWNLOAD_LANDING)]
        if usable:
            continue
        for rel in releases:
            groups.setdefault(rel["verification_status"], []).append((row, rel))

    legend = {
        "broken": "server answered 4xx — the artifact is gone or moved",
        "inaccessible": "network/server failure after retries — not proven dead",
        "blocked": "401/403 — access denied; we do not bypass it",
        "untested": "not checked yet (budget, offline run or cache only)",
        "untested_protocol": "non-HTTP scheme (e.g. ftp://) that we cannot check over HTTP",
        "landing_ok": "a landing page resolves; the artifact itself is not proven",
        "no_link_recorded": "no release link at all in the source record",
    }
    lines = [
        "# Releases without a usable verified download",
        "",
        f"Generated at {generated_at}. 'Usable' = direct/redirected artifact "
        "or a resolvable landing page, for the selected collection.",
        "",
        "| status | count | meaning |",
        "| --- | --- | --- |",
    ]
    for status in ("no_link_recorded", "untested", "untested_protocol", "broken",
                   "inaccessible", "blocked"):
        if status in groups or status == "no_link_recorded":
            lines.append(f"| {status} | {len(groups.get(status, []))} | {legend[status]} |")
    lines.append("")
    for status in sorted(groups):
        entries = groups[status]
        lines.append(f"## {status} ({len(entries)})")
        lines.append("")
        lines.append(f"_{legend.get(status, status)}_")
        lines.append("")
        for row, rel in entries:
            url = rel["download_url"] if rel else "(none)"
            lines.append(f"- **{md_escape(row['title'])}** ({row['release_year'] or '?'}) — "
                         f"`{row['canonical_key']}` — {md_escape(url)}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    counts = {k: len(v) for k, v in groups.items()}
    return {"path": str(path), "counts": counts}


def write_review_required(conn, path: Path, *, generated_at: str) -> dict[str, Any]:
    review_rows = list(conn.execute(
        "SELECT * FROM productions WHERE selection_status = 'needs_review' ORDER BY canonical_key"))
    ambiguous_videos = list(conn.execute(
        """
        SELECT v.*, p.title AS prod_title, p.canonical_key FROM videos v
          JOIN productions p ON p.id = v.production_id
         WHERE v.verification_status = 'needs_review'
         ORDER BY p.canonical_key, v.id
        """
    ))
    merges = list(conn.execute("SELECT * FROM merge_log ORDER BY merged_at DESC, id"))
    merges.reverse()

    lines = [
        "# Records requiring human review",
        "",
        f"Generated at {generated_at}. Nothing in this file was merged or resolved automatically.",
        "",
        f"- productions flagged `needs_review`: **{len(review_rows)}**",
        f"- ambiguous video matches: **{len(ambiguous_videos)}**",
        f"- automatic merges performed (audit trail): **{len(merges)}**",
        "",
        "## Ambiguous productions (possible duplicates)",
        "",
    ]
    if not review_rows:
        lines.append("_None._")
        lines.append("")
    for row in review_rows:
        rationale = row["curation_rationale"] or "{}"
        try:
            reason = json.loads(rationale)
        except ValueError:
            reason = {"raw": rationale}
        lines.append(f"- **{md_escape(row['title'])}** ({row['release_year'] or '?'}) — "
                     f"`{row['canonical_key']}` — {md_escape(json.dumps(reason, ensure_ascii=False))}")
    lines.append("")
    lines.append("## Ambiguous video matches")
    lines.append("")
    if not ambiguous_videos:
        lines.append("_None._")
        lines.append("")
    for vid in ambiguous_videos:
        lines.append(
            f"- **{md_escape(vid['prod_title'])}** — `{vid['canonical_url']}` — "
            f"confidence {vid['match_confidence'] if vid['match_confidence'] is not None else 'n/a'} — "
            f"{md_escape(vid['match_evidence'] or '')}"
        )
    lines.append("")
    lines.append("## Merge audit trail")
    lines.append("")
    if not merges:
        lines.append("_No merges were performed._")
        lines.append("")
    for entry in merges:
        lines.append(f"- `{entry['merged_key']}` → `{entry['survivor_key']}` "
                     f"({entry['method']}, confidence {entry['confidence']:.3f})")
    lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"path": str(path), "needs_review": len(review_rows),
            "ambiguous_videos": len(ambiguous_videos), "merges": len(merges)}


def write_readme(conn, path: Path, *, generated_at: str, verification: dict[str, Any] | None = None) -> dict[str, Any]:
    counts = {
        "productions": int(conn.execute("SELECT COUNT(*) AS n FROM productions").fetchone()["n"]),
        "selected": int(conn.execute(
            "SELECT COUNT(*) AS n FROM productions WHERE selection_status='selected'").fetchone()["n"]),
        "creators": int(conn.execute("SELECT COUNT(*) AS n FROM creators").fetchone()["n"]),
        "releases": int(conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"]),
        "videos": int(conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]),
        "provenance": int(conn.execute("SELECT COUNT(*) AS n FROM provenance").fetchone()["n"]),
    }
    lines = [
        "# Demoscene collection — export overview",
        "",
        f"Generated at {generated_at} from `data/demoscene.sqlite3` (schema v1).",
        "",
        "## What is in this folder",
        "",
        "| file | contents |",
        "| --- | --- |",
        "| `productions.csv` … `provenance.csv` | one entity/relationship per row, full population |",
        "| `verification_report.csv` | link/video verification states for the collection |",
        "| `demoscene.json` | versioned, nested export of the curated collection |",
        "| `index.html` | self-contained, searchable gallery of the same collection |",
        "| `catalogue.md` | readable catalogue grouped by era → platform family |",
        "| `missing_videos.md` | selected productions without a verified video |",
        "| `unavailable_downloads.md` | releases without a usable verified link |",
        "| `review_required.md` | ambiguous matches and merge audit trail |",
        "",
        "## Collection at a glance",
        "",
        f"- candidate productions in database: **{counts['productions']}**",
        f"- selected productions: **{counts['selected']}**",
        f"- unique creators: **{counts['creators']}**",
        f"- release links: **{counts['releases']}**",
        f"- video links: **{counts['videos']}**",
        f"- provenance records: **{counts['provenance']}**",
        "",
        "## How to read the verification labels",
        "",
        "| label | meaning |",
        "| --- | --- |",
        "| `verified` / `redirected` | HTTP check succeeded and the response looked like an artifact "
        "(content type / known extension). It does **not** prove the program runs. |",
        "| `landing_ok` | an HTML landing page resolves; the artifact itself is unproven. |",
        "| `broken` | the server answered 4xx. |",
        "| `inaccessible` | network/server failure after retries; not proven dead. |",
        "| `blocked` | 401/403 — access denied; never bypassed. |",
        "| `untested` | no check has been recorded yet. |",
        "| `untested_protocol` | non-HTTP scheme (e.g. `ftp://`) that cannot be checked over HTTP. |",
        "",
        "Video matches: `verified_match` > `probable_match` > `needs_review` > `rejected` > "
        "`unavailable`. Confidence comes from title similarity plus creator/year evidence; an "
        "exact title alone is never treated as proof.",
        "",
        "## Methodology in one paragraph",
        "",
        "Candidates are read from the weekly Pouët JSON dump (an official published dataset), "
        "normalised, deduplicated with exact source ids first and fuzzy evidence second, scored "
        "with configurable weights that keep *popularity* and *historical/technical significance* "
        "separate, then selected with a deterministic diversity algorithm (era × platform family "
        "caps). Video links and release links are verified with polite, rate-limited single "
        "requests. See `docs/methodology.md` for the full rules and `docs/source_audit.md` for "
        "the evidence behind each source.",
        "",
        "## Caveats",
        "",
        "- Demozoo was unreachable from the build network on 2026-10-10 and contributes nothing "
        "beyond cross-reference ids carried in Pouët records.",
        "- No YouTube API key was available: video *search* was not executed; explicit links were "
        "verified with the keyless oEmbed endpoint.",
        "- Link checks are point-in-time HTTP observations, not guarantees.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(path), "counts": counts}


def export_markdown(conn, exports_dir: Path, *, generated_at: str) -> dict[str, Any]:
    out = Path(exports_dir)
    out.mkdir(parents=True, exist_ok=True)
    return {
        "readme": write_readme(conn, out / "README.md", generated_at=generated_at),
        "catalogue": write_catalogue(conn, out / "catalogue.md", generated_at=generated_at),
        "missing_videos": write_missing_videos(conn, out / "missing_videos.md", generated_at=generated_at),
        "unavailable_downloads": write_unavailable_downloads(
            conn, out / "unavailable_downloads.md", generated_at=generated_at),
        "review_required": write_review_required(
            conn, out / "review_required.md", generated_at=generated_at),
    }
