"""Pipeline orchestration: stages A–G as independently runnable operations.

Stage letters follow the implementation prompt:

* A `discover`  — read official data dumps/APIs, store source ids + hashes
* B `normalize` — applied inside discovery (validation before persistence)
* C `dedupe`    — exact ids first, fuzzy evidence second (run by `curate`)
* D `curate`    — score + select with a deterministic, diversity-aware algorithm
* E `enrich`    — video verification/search for the selected productions
* F `verify-links` — HEAD/ranged-GET checks of release links (never downloads)
* G `verify`    — referential integrity and coverage checks
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from . import database as db
from .config import Settings
from .deduplication import DedupReport, deduplicate, identity_from_candidate, review_keys
from .http_client import HttpClient, HttpClientError, build_client
from .models import CreatorRef, ProductionCandidate, Provenance, VideoCandidate
from .normalization import normalize_name, normalize_whitespace
from .selection import SelectionResult, select
from .source_adapters import (
    DemozooAdapter,
    NoApiKey,
    PouetAdapter,
    SourceUnreachable,
    YouTubeAdapter,
    YouTubeQuotaExceeded,
    build_search_query,
    match_video,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 2000

# Link-check outcomes that are not failures of the source or of this pipeline.
_LINK_JOB_STATUSES = {
    "verified": "success",
    "redirected": "success",
    "landing_ok": "success",
    # ftp://, git:// … cannot be checked over HTTP: expected, not a failure.
    "untested_protocol": "skipped",
}


def _link_job_status(link_status: str) -> str:
    """Map a link-check result to a fetch_jobs status.

    ``untested_protocol`` is recorded as ``skipped`` rather than ``failed``:
    the pipeline never claims an FTP link is dead just because HTTP cannot
    reach it, so it must not inflate the source-failure report either.
    """
    return _LINK_JOB_STATUSES.get(link_status, "failed")


class Pipeline:
    def __init__(self, settings: Settings, *, offline: bool = False):
        self.settings = settings
        self.offline = offline
        self._conn: Any = None
        self._client: HttpClient | None = None

    # -- resources ----------------------------------------------------------
    @property
    def conn(self):
        if self._conn is None:
            self._conn = db.connect(self.settings.db_path)
            db.init_db(self._conn)
        return self._conn

    @property
    def client(self) -> HttpClient:
        if self._client is None:
            cfg = self.settings.sources
            self._client = build_client(cfg, cache_dir=self.settings.cache_dir, offline=self.offline)
        return self._client

    def adapters(self) -> dict[str, Any]:
        from .source_adapters import build_adapters

        return build_adapters(self.settings.sources, youtube_api_key=self.settings.youtube_api_key)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Stage 0: source audit (network)
    # ------------------------------------------------------------------
    def audit_sources(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        adapters = self.adapters()
        results: list[dict[str, Any]] = []
        for name, adapter in adapters.items():
            cfg = self.settings.source(name)
            db.upsert_source(
                self.conn, name=name, base_url=cfg.get("base_url", adapter.base_url),
                source_type=cfg.get("source_type", adapter.source_type),
                access_method=cfg.get("access_method", adapter.access_method),
                terms_url=cfg.get("terms_url"), notes=cfg.get("notes"),
                api_documentation_url=cfg.get("api_documentation_url"),
            )
            if dry_run:
                results.append({"source": name, "status": "skipped", "detail": "--dry-run: no network requests"})
                continue
            if self.offline:
                results.append({"source": name, "status": "skipped", "detail": "--offline: no network requests"})
                continue
            probe = adapter.probe(self.client)
            db.mark_source(self.conn, name, status=probe.status, ok=probe.reachable,
                           notes=probe.detail)
            db.record_job(
                self.conn, job_type="audit", source_id=name, target_key="probe",
                status="success" if probe.reachable else "unreachable",
                error=None if probe.reachable else probe.detail,
                detail=probe.detail, attempt_increment=True,
            )
            results.append({
                "source": name,
                "status": probe.status,
                "reachable": probe.reachable,
                "detail": probe.detail,
                "auth_required": probe.auth_required,
                "api_documented": probe.api_documented,
            })
            log.info("probe %-16s %s — %s", name, probe.status, probe.detail)
        self.conn.commit()
        return results

    # ------------------------------------------------------------------
    # Stage A/B: discovery + normalisation
    # ------------------------------------------------------------------
    def discover(self, *, limit: int | None = None, resume: bool = False,
                 source: str | None = None) -> dict[str, Any]:
        source_name = source or "pouet"
        if source_name != "pouet":
            adapter = self.adapters().get(source_name)
            if adapter is None:
                raise ValueError(f"unknown or disabled source: {source_name}")
            try:
                candidates = list(adapter.discover(self.client, offline=self.offline, limit=limit))
            except SourceUnreachable as exc:
                db.record_job(self.conn, job_type="discover", source_id=source_name,
                              target_key=source_name, status="unreachable", error=str(exc),
                              attempt_increment=True)
                self.conn.commit()
                return {"source": source_name, "productions": 0, "error": str(exc)}
            return self._persist(candidates, source_name=source_name, response_hash=None,
                                 dump_name=source_name, limit=limit)

        adapter = PouetAdapter(self.settings.source("pouet"))
        db.upsert_source(
            self.conn, name="pouet", base_url=adapter.base_url, source_type=adapter.source_type,
            access_method=adapter.access_method, terms_url=adapter.terms_url,
            api_documentation_url=adapter.api_documentation_url, notes=adapter.notes,
        )
        try:
            candidates = adapter.discover(
                self.client, dump_dir=self.settings.input_dir,
                offline=self.offline, limit=limit,
            )
            return self._persist_stream(candidates, adapter=adapter, resume=resume, limit=limit)
        except HttpClientError as exc:
            db.record_job(self.conn, job_type="discover", source_id="pouet", target_key="dump",
                          status="failed", error=str(exc), attempt_increment=True)
            self.conn.commit()
            return {"source": "pouet", "productions": 0, "error": str(exc)}

    def _persist_stream(self, candidates: Iterable[ProductionCandidate], *, adapter: PouetAdapter,
                        resume: bool, limit: int | None) -> dict[str, Any]:
        cursor_name = "pouet:discover:last_id"
        last_id = int(db.get_cursor(self.conn, cursor_name) or 0) if resume else 0
        dump_path = _latest_dump(self.settings.input_dir)
        dump_hash = _sha256_file(dump_path) if dump_path else None
        dump_date = adapter.dump_date(dump_path) if dump_path else "unknown"
        started = db.utcnow()
        stats = {"productions": 0, "creators": 0, "platforms": 0, "releases": 0,
                 "videos": 0, "provenance": 0, "skipped_existing": 0}
        batch = 0
        max_id = last_id
        for cand in candidates:
            try:
                record_id = int(cand.source_record_id)
            except ValueError:
                record_id = 0
            if resume and record_id <= last_id:
                stats["skipped_existing"] += 1
                continue
            with db.transaction(self.conn):
                written = self._persist_one(cand, stats)
            max_id = max(max_id, record_id)
            batch += 1
            if batch >= BATCH_SIZE:
                db.set_cursor(self.conn, cursor_name, str(max_id))
                self.conn.commit()
                log.info("discovery checkpoint at pouet id %s (%s productions)",
                         max_id, stats["productions"])
                batch = 0
            if limit and stats["productions"] >= int(limit):
                break
        db.set_cursor(self.conn, cursor_name, str(max_id))
        db.record_job(
            self.conn, job_type="discover", source_id="pouet",
            target_key=dump_path.name if dump_path else "dump",
            status="success", response_hash=dump_hash, attempt_increment=True,
            detail=f"dump_date={dump_date}; started={started}; stats={json.dumps(stats, sort_keys=True)}",
        )
        db.mark_source(self.conn, "pouet", status="ok", ok=True,
                       notes=f"dump_date={dump_date}")
        self.conn.commit()
        log.info("discovery finished: %s", stats)
        stats["dump_date"] = dump_date
        stats["dump_sha256"] = dump_hash
        return stats

    def _persist(self, candidates: Iterable[ProductionCandidate], *, source_name: str,
                 response_hash: str | None, dump_name: str, limit: int | None) -> dict[str, Any]:
        stats = {"productions": 0, "creators": 0, "platforms": 0, "releases": 0,
                 "videos": 0, "provenance": 0, "failed": 0, "skipped_creators": 0}
        pending = 0
        for cand in candidates:
            try:
                self._persist_one(cand, stats)
            except Exception as exc:  # one bad record must not abort discovery
                log.warning("skipping unpersistable record %s:%s: %s",
                            cand.source_id, cand.source_record_id, exc)
                stats["failed"] += 1
            pending += 1
            if pending >= 500:  # batch commits: ~100k records would be slow otherwise
                self.conn.commit()
                pending = 0
            if limit and stats["productions"] >= int(limit):
                break
        db.record_job(self.conn, job_type="discover", source_id=source_name, target_key=dump_name,
                      status="success", response_hash=response_hash, attempt_increment=True,
                      detail=json.dumps(stats, sort_keys=True))
        self.conn.commit()
        return stats

    def _persist_one(self, cand: ProductionCandidate, stats: dict[str, int]) -> None:
        production_id = db.upsert_production(self.conn, cand)
        stats["productions"] += 1

        # creators / groups
        seen_roles: set[tuple[int, str]] = set()
        for ref, role in [(ref, "group") for ref in cand.groups] + [
            (ref, normalize_whitespace(ref.role) or "unknown") for ref in cand.credits
        ]:
            if not normalize_name(ref.name):
                # e.g. Pouët credits whose nickname is only invisible markup
                stats["skipped_creators"] = stats.get("skipped_creators", 0) + 1
                continue
            creator_id = db.ensure_creator(self.conn, ref)
            key = (creator_id, role)
            if key not in seen_roles:
                db.link_creator(self.conn, production_id, creator_id, role=role,
                                source_id=cand.source_id)
                seen_roles.add(key)
                stats["creators"] += 1

        # platforms
        for canonical, family in cand.normalized_platforms:
            platform_id = db.ensure_platform(self.conn, canonical, family)
            db.link_platform(self.conn, production_id, platform_id, source_id=cand.source_id,
                             evidence_url=cand.source_url)
            stats["platforms"] += 1

        # releases (metadata only — never downloaded)
        for rel in cand.releases:
            platform_id = None
            if cand.normalized_platforms:
                platform_id = db.ensure_platform(self.conn, *cand.normalized_platforms[0])
            db.upsert_release(self.conn, production_id, rel, platform_id=platform_id)
            stats["releases"] += 1

        # videos
        for vid in cand.videos:
            db.upsert_video(self.conn, production_id, vid)
            stats["videos"] += 1

        # scoring inputs
        db.upsert_metrics(
            self.conn, production_id,
            popularity=cand.popularity, significance=cand.significance,
            cross_ids=cand.cross_ids,
            extra={k: v for k, v in cand.raw.items() if k != "pouet_id"} | {"source_url": cand.source_url},
        )

        # provenance for the source record itself (field-level provenance is
        # written for the selected/review set in stage D)
        db.add_provenance(self.conn, Provenance(
            entity_type="production", entity_id=cand.canonical_key, field_name="source_record",
            source_id=cand.source_id, source_record_id=cand.source_record_id,
            source_url=cand.source_url,
            observed_value={"title": cand.display_title, "release_date": cand.iso_date,
                            "type": cand.production_type, "source": cand.source_id},
            confidence="high", notes="record as published by the source",
        ))
        stats["provenance"] += 1

    # ------------------------------------------------------------------
    # Stage C+D: deduplication + curation
    # ------------------------------------------------------------------
    def curate(self, *, target: int | None = None, limit: int | None = None) -> dict[str, Any]:
        rules = dict(self.settings.rules)
        if target:
            rules["target_size"] = int(target)
        thresholds = {k: float(v) for k, v in rules.get("thresholds", {}).items()}

        candidates = db.load_all_candidates(self.conn, limit=limit)
        log.info("loaded %s candidates for curation", len(candidates))

        dedup: DedupReport = deduplicate(candidates, thresholds)
        merged_away: dict[str, str] = {}
        for decision in dedup.merged:
            survivor, gone = decision.survivor, decision.candidate
            merged_away[gone.canonical_key] = survivor.canonical_key
            db.record_merge(
                self.conn, survivor_key=survivor.canonical_key, merged_key=gone.canonical_key,
                method=decision.match.label, confidence=decision.match.confidence,
                evidence=decision.match.evidence,
            )
            # preserve the losing record's title as an alias of the survivor
            survivor_id = self._production_id(survivor.canonical_key)
            gone_id = self._production_id(gone.canonical_key)
            if survivor_id and gone_id:
                row = self.conn.execute("SELECT title FROM productions WHERE id = ?", (gone_id,)).fetchone()
                if row:
                    db.add_alias(self.conn, survivor_id, row["title"], source_id=gone.source_id,
                                 source_record_id=gone.source_record_id)
            if gone_id:
                db.set_production_scores(
                    self.conn, gone.canonical_key, curation_score=None, popularity_score=None,
                    significance_score=None,
                    rationale=json.dumps({"merged_into": survivor.canonical_key,
                                          "confidence": decision.match.confidence,
                                          "evidence": decision.match.evidence}),
                    selection_status="rejected",
                )

        ambiguous = review_keys(dedup)
        for key in sorted(ambiguous):
            self._production_id(key)  # ensure it exists
            db.set_production_scores(
                self.conn, key, curation_score=None, popularity_score=None,
                significance_score=None,
                rationale=json.dumps({"reason": "ambiguous match across sources",
                                      "action": "needs_review"}),
                selection_status="needs_review",
            )

        pool = [c for c in candidates if c.canonical_key not in merged_away
                and c.canonical_key not in ambiguous]
        result: SelectionResult = select(pool, rules)

        for rank, item in enumerate(result.selected, start=1):
            db.set_production_scores(
                self.conn, item.canonical_key, curation_score=item.curation,
                popularity_score=item.popularity, significance_score=item.significance,
                rationale=item.rationale, selection_status="selected", selection_rank=rank,
            )
            self._write_field_provenance(item)
        for item, reason in result.rejected:
            db.set_production_scores(
                self.conn, item.canonical_key, curation_score=item.curation,
                popularity_score=item.popularity, significance_score=item.significance,
                rationale=json.dumps({"reason": reason, "components": item.components},
                                     ensure_ascii=False, sort_keys=True),
                selection_status="rejected",
            )

        db.record_job(
            self.conn, job_type="curate", source_id="internal", target_key=f"target={len(result.selected)}",
            status="success", attempt_increment=True,
            detail=json.dumps({
                "target": rules.get("target_size"),
                "selected": len(result.selected),
                "rejected": len(result.rejected),
                "needs_review": len(ambiguous),
                "merged": len(dedup.merged),
                "candidates": len(candidates),
            }, sort_keys=True),
        )
        self.conn.commit()
        summary = {
            "candidates": len(candidates),
            "selected": len(result.selected),
            "rejected": len(result.rejected),
            "needs_review": len(ambiguous),
            "merged": len(dedup.merged),
            "target": rules.get("target_size"),
        }
        log.info("curation finished: %s", summary)
        return summary

    def _production_id(self, canonical_key: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM productions WHERE canonical_key = ?", (canonical_key,)
        ).fetchone()
        return int(row["id"]) if row else None

    def _write_field_provenance(self, item: Any) -> None:
        """Traceable evidence for every field of a selected production."""
        cand: ProductionCandidate = item.candidate
        fields: list[Provenance] = [
            Provenance("production", cand.canonical_key, "title", cand.source_id,
                       cand.source_record_id, cand.source_url, cand.display_title),
            Provenance("production", cand.canonical_key, "release_date", cand.source_id,
                       cand.source_record_id, cand.source_url,
                       {"raw": getattr(cand, "release_date", None),
                        "year": cand.year, "iso": cand.iso_date}),
            Provenance("production", cand.canonical_key, "production_type", cand.source_id,
                       cand.source_record_id, cand.source_url, cand.production_type),
            Provenance("production", cand.canonical_key, "description", cand.source_id,
                       cand.source_record_id, cand.source_url, cand.description),
            Provenance("production", cand.canonical_key, "curation_score", "internal",
                       None, "docs/methodology.md",
                       {"components": item.components, "popularity": item.popularity,
                        "significance": item.significance, "curation": item.curation},
                       confidence="medium", notes="computed by this pipeline, not a source claim"),
        ]
        for ref in list(cand.groups) + list(cand.credits):
            fields.append(Provenance(
                "production", cand.canonical_key, f"creator:{ref.normalized_name}", cand.source_id,
                cand.source_record_id, cand.source_url, {"name": ref.name, "role": ref.role},
            ))
        for canonical, family in cand.normalized_platforms:
            fields.append(Provenance(
                "production", cand.canonical_key, f"platform:{canonical}", cand.source_id,
                cand.source_record_id, cand.source_url, {"canonical": canonical, "family": family},
            ))
        for rel in cand.releases:
            fields.append(Provenance(
                "production", cand.canonical_key, "release", cand.source_id,
                cand.source_record_id, cand.source_url,
                {"url": rel.url, "kind": rel.kind, "artifact_type": rel.artifact_type,
                 "label": rel.link_label},
            ))
        for vid in cand.videos:
            fields.append(Provenance(
                "production", cand.canonical_key, "video", cand.source_id,
                cand.source_record_id, cand.source_url,
                {"url": vid.canonical_url, "link_type": vid.link_type,
                 "video_kind": vid.video_kind, "confidence": vid.match_confidence},
            ))
        for key, value in sorted(cand.cross_ids.items()):
            fields.append(Provenance(
                "production", cand.canonical_key, f"cross_id:{key}", cand.source_id,
                cand.source_record_id, cand.source_url, value,
            ))
        db.add_provenance_batch(self.conn, fields)

    # ------------------------------------------------------------------
    # Stage E: video enrichment
    # ------------------------------------------------------------------
    def enrich(self, *, limit: int | None = None) -> dict[str, Any]:
        adapters = self.adapters()
        youtube: YouTubeAdapter | None = adapters.get("youtube_data_api")  # type: ignore[assignment]
        verification_cfg = self.settings.verification
        budget = int(verification_cfg.get("max_video_checks", 600))
        stats = {
            "videos_checked": 0, "verified_match": 0, "probable_match": 0,
            "needs_review": 0, "rejected": 0, "unavailable": 0,
            "productions_without_video": 0, "search_queries": 0, "search_added": 0,
            "search_unavailable": 0, "quota_exceeded": 0, "budget_exhausted": False,
        }

        rows = list(self.conn.execute(
            "SELECT p.* FROM productions p WHERE p.selection_status = 'selected' "
            "OR p.selection_status = 'needs_review' ORDER BY COALESCE(p.selection_rank, 1000000), p.canonical_key"
        ))
        if limit:
            rows = rows[: int(limit)]

        for row in rows:
            production_id = int(row["id"])
            videos = list(self.conn.execute(
                "SELECT * FROM videos WHERE production_id = ? ORDER BY id", (production_id,)
            ))
            pending = [v for v in videos if v["verification_status"] == "untested"]
            for video in pending:
                if stats["videos_checked"] >= budget:
                    stats["budget_exhausted"] = True
                    break
                if self._verify_video(youtube, production_id, video, stats, explicit=True):
                    stats["videos_checked"] += 1

            has_usable = any(
                v["verification_status"] in ("verified_match", "probable_match")
                for v in videos
            )
            if not has_usable and not pending:
                stats["productions_without_video"] += 1
                if youtube is None or not youtube.api_key:
                    stats["search_unavailable"] += 1
                    db.record_job(
                        self.conn, job_type="video_search", source_id="youtube_data_api",
                        target_key=row["canonical_key"], status="skipped",
                        detail="no YOUTUBE_API_KEY: search-based discovery disabled, "
                               "explicit-link extraction only",
                    )
                    continue
                if stats["videos_checked"] >= budget:
                    stats["budget_exhausted"] = True
                    continue
                try:
                    added = self._search_videos(youtube, row, stats)
                except YouTubeQuotaExceeded:
                    stats["quota_exceeded"] += 1
                    db.record_job(self.conn, job_type="video_search", source_id="youtube_data_api",
                                  target_key=row["canonical_key"], status="failed",
                                  error="quota exceeded", attempt_increment=True)
                    break
                except NoApiKey:
                    stats["search_unavailable"] += 1
                    continue
                stats["search_added"] += added
            if stats["budget_exhausted"]:
                break

        self.conn.commit()
        log.info("video enrichment finished: %s", stats)
        return stats

    def _verify_video(self, youtube: YouTubeAdapter | None, production_id: int,
                      video: Any, stats: dict[str, Any], *, explicit: bool) -> bool:
        """Verify one video link. Returns True when a network check was made."""
        row = self.conn.execute("SELECT p.* FROM productions p WHERE p.id = ?", (production_id,)).fetchone()
        prod_title = row["title"] if row else ""
        prod_year = row["release_year"] if row else None
        creators = [
            r["canonical_name"] for r in self.conn.execute(
                "SELECT c.canonical_name FROM production_creators pc "
                "JOIN creators c ON c.id = pc.creator_id WHERE pc.production_id = ?",
                (production_id,),
            )
        ]
        if self.offline:
            db.update_video_check(
                self.conn, int(video["id"]), status="untested",
                evidence="not checked (--offline)", confidence=None,
            )
            return False
        yt = youtube or self._youtube_adapter()
        if yt is None:
            db.update_video_check(
                self.conn, int(video["id"]), status="untested",
                evidence="youtube adapter disabled", confidence=None,
            )
            return False
        meta = yt.oembed(self.client, video["canonical_url"])
        if not meta.available:
            stats["unavailable"] += 1
            db.update_video_check(
                self.conn, int(video["id"]), status="unavailable",
                evidence=meta.detail or "oEmbed reported the video as unavailable",
                confidence=0.0, detail=meta.detail,
            )
            return True
        decision = match_video(
            production_title=prod_title, video_title=meta.title, creators=creators,
            year=prod_year, explicit_link=explicit, thresholds=self.settings.thresholds,
        )
        stats[decision.status] = stats.get(decision.status, 0) + 1
        db.update_video_check(
            self.conn, int(video["id"]), status=decision.status, evidence=decision.evidence,
            confidence=decision.confidence, title=meta.title, channel=meta.channel_name,
            detail=meta.detail,
        )
        return True

    def _youtube_adapter(self) -> YouTubeAdapter | None:
        cfg = self.settings.sources.get("sources", {}).get("youtube_data_api")
        if not cfg:
            return None
        return YouTubeAdapter(cfg, api_key=self.settings.youtube_api_key)

    def _search_videos(self, youtube: YouTubeAdapter, row: Any, stats: dict[str, Any]) -> int:
        production_id = int(row["id"])
        creators = [
            r["canonical_name"] for r in self.conn.execute(
                "SELECT c.canonical_name FROM production_creators pc "
                "JOIN creators c ON c.id = pc.creator_id WHERE pc.production_id = ?",
                (production_id,),
            )
        ]
        query = build_search_query(row["title"], creators[:2], row["release_year"])
        stats["search_queries"] += 1
        results = youtube.search(self.client, query, max_results=5)
        db.record_job(self.conn, job_type="video_search", source_id="youtube_data_api",
                      target_key=row["canonical_key"], status="success",
                      detail=json.dumps({"query": query, "results": len(results)}))
        added = 0
        for meta in results:
            decision = match_video(
                production_title=row["title"], video_title=meta.title, creators=creators,
                year=row["release_year"], explicit_link=False,
                thresholds=self.settings.thresholds,
            )
            if decision.status == "rejected":
                continue
            cand = VideoCandidate(
                url=meta.url, source_id="youtube_data_api", link_type="search result",
                video_kind="demo capture", title=meta.title, channel_name=meta.channel_name,
                published_at=meta.published_at, duration_seconds=meta.duration_seconds,
                match_confidence=decision.confidence, match_evidence=decision.evidence,
                verification_status=decision.status,
            )
            db.upsert_video(self.conn, production_id, cand)
            added += 1
        return added

    # ------------------------------------------------------------------
    # Stage F: executable / release link verification
    # ------------------------------------------------------------------
    def verify_links(self, *, limit: int | None = None, retry_failed: bool = False) -> dict[str, Any]:
        from .source_adapters.scene_org import check_link

        verification = self.settings.verification
        budget = int(verification.get("max_link_checks", 600))
        per_host = int(verification.get("max_link_checks_per_host", 40))
        if limit:
            budget = min(budget, int(limit))
        host_counts: dict[str, int] = {}
        stats = {"checked": 0, "verified": 0, "landing_ok": 0, "redirected": 0, "broken": 0,
                 "blocked": 0, "inaccessible": 0, "untested_protocol": 0, "untested": 0,
                 "budget_exhausted": False, "host_capped": 0}

        statuses = ["untested"] + (["broken", "inaccessible", "blocked"] if retry_failed else [])
        placeholders = ",".join("?" for _ in statuses)
        rows = list(self.conn.execute(
            f"""
            SELECT r.*, p.canonical_key, p.selection_rank, p.selection_status
              FROM releases r JOIN productions p ON p.id = r.production_id
             WHERE r.verification_status IN ({placeholders})
               AND p.selection_status IN ('selected', 'needs_review')
             ORDER BY COALESCE(p.selection_rank, 1000000), r.id
            """,
            statuses,
        ))

        for row in rows:
            if stats["checked"] >= budget:
                stats["budget_exhausted"] = True
                break
            from urllib.parse import urlsplit

            host = (urlsplit(row["download_url"]).netloc or "").lower()
            if host_counts.get(host, 0) >= per_host:
                stats["host_capped"] += 1
                continue
            if self.offline:
                break
            try:
                result = check_link(self.client, row["download_url"])
            except Exception as exc:  # a single bad URL must not abort the stage
                log.warning("link check crashed for %s: %s", row["download_url"], exc)
                stats["inaccessible"] = stats.get("inaccessible", 0) + 1
                db.update_release_check(self.conn, int(row["id"]), status="inaccessible",
                                        detail=f"check crashed: {type(exc).__name__}: {exc}")
                stats["checked"] += 1
                continue
            host_counts[host] = host_counts.get(host, 0) + 1
            stats["checked"] += 1
            stats[result.status] = stats.get(result.status, 0) + 1
            db.update_release_check(
                self.conn, int(row["id"]), status=result.status, detail=result.detail,
                url_kind=result.url_kind, landing_page_url=result.landing_page_url,
            )
            db.record_job(
                self.conn, job_type="verify_link", source_id=row["source_id"],
                target_key=f"{row['canonical_key']}|{row['download_url']}",
                status=_link_job_status(result.status),
                response_status=result.http_status, detail=result.detail,
                attempt_increment=True,
                error=None if result.status in ("verified", "redirected", "landing_ok",
                                                 "untested_protocol")
                else f"{result.status}: {result.detail}",
            )
            if stats["checked"] % 20 == 0:
                self.conn.commit()
        self.conn.commit()
        log.info("link verification finished: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Stage G: final verification (integrity, no network)
    # ------------------------------------------------------------------
    def verify(self, *, target: int | None = None) -> dict[str, Any]:
        from .verification import run_integrity_checks

        report = run_integrity_checks(self.conn, target=target or self.settings.target_size)
        # The job succeeded whenever the checks actually ran; "attention" means
        # warnings were found (surfaced in the report), not that this stage
        # crashed. Only a real check failure is recorded as failed.
        db.record_job(
            self.conn, job_type="verify", source_id="internal", target_key="integrity",
            status="failed" if report["status"] == "fail" else "success",
            detail=json.dumps({k: v for k, v in report.items() if k != "checks"}, sort_keys=True),
            attempt_increment=True,
        )
        self.conn.commit()
        return report


def _latest_dump(input_dir: Path) -> Path | None:
    matches = sorted(Path(input_dir).glob("pouetdatadump-prods-*"))
    return matches[-1] if matches else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
