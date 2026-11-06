"""Print the real collection metrics used for the final report (read-only)."""
from __future__ import annotations

import sys

from demoscene_collector import database as db


def one(conn, sql: str, *params):
    return int(conn.execute(sql, params).fetchone()["n"])


def main(db_path: str) -> None:
    conn = db.connect(db_path)

    print("== population ==")
    print("discovered productions :", one(conn, "SELECT COUNT(*) n FROM productions"))
    print("creators               :", one(conn, "SELECT COUNT(*) n FROM creators"))
    print("creator credits        :", one(conn, "SELECT COUNT(*) n FROM production_creators"))
    print("platforms              :", one(conn, "SELECT COUNT(*) n FROM platforms"))
    print("release links          :", one(conn, "SELECT COUNT(*) n FROM releases"))
    print("video links            :", one(conn, "SELECT COUNT(*) n FROM videos"))
    print("provenance rows        :", one(conn, "SELECT COUNT(*) n FROM provenance"))

    print("\n== selection ==")
    for r in conn.execute("SELECT selection_status s, COUNT(*) n FROM productions "
                          "GROUP BY 1 ORDER BY n DESC"):
        print(f"{r['s']:<14}: {r['n']}")
    print("merges logged          :", one(conn, "SELECT COUNT(*) n FROM merge_log"))
    print("aliases                :", one(conn, "SELECT COUNT(*) n FROM aliases"))

    print("\n== videos (whole population) ==")
    for r in conn.execute("SELECT verification_status s, COUNT(*) n FROM videos "
                          "GROUP BY 1 ORDER BY n DESC"):
        print(f"{r['s']:<18}: {r['n']}")

    print("\n== videos (selected only) ==")
    for r in conn.execute(
        "SELECT v.verification_status s, COUNT(*) n FROM videos v "
        "JOIN productions p ON p.id = v.production_id "
        "WHERE p.selection_status = 'selected' GROUP BY 1 ORDER BY n DESC"):
        print(f"{r['s']:<18}: {r['n']}")

    print("\n== releases (selected only) ==")
    for r in conn.execute(
        "SELECT r.verification_status s, COUNT(*) n FROM releases r "
        "JOIN productions p ON p.id = r.production_id "
        "WHERE p.selection_status = 'selected' GROUP BY 1 ORDER BY n DESC"):
        print(f"{r['s']:<18}: {r['n']}")

    print("\n== selected coverage ==")
    print("selected with >=1 successful link check:",
          one(conn, """SELECT COUNT(*) n FROM productions p WHERE p.selection_status='selected'
              AND EXISTS (SELECT 1 FROM releases r WHERE r.production_id=p.id
                          AND r.verification_status IN ('verified','landing_ok','redirected'))"""))
    print("selected with >=1 verified/probable video:",
          one(conn, """SELECT COUNT(*) n FROM productions p WHERE p.selection_status='selected'
              AND EXISTS (SELECT 1 FROM videos v WHERE v.production_id=p.id
                          AND v.verification_status IN ('verified_match','probable_match'))"""))
    print("selected with NO video link at all:",
          one(conn, """SELECT COUNT(*) n FROM productions p WHERE p.selection_status='selected'
              AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.production_id=p.id)"""))

    print("\n== distinct hosts checked (selected) ==")
    for r in conn.execute(
        "SELECT COUNT(DISTINCT lower(coalesce(nullif(substr(r.download_url, instr(r.download_url,'//')+2), ''), ''))) n "
        "FROM releases r JOIN productions p ON p.id=r.production_id "
        "WHERE p.selection_status='selected' AND r.last_checked_at IS NOT NULL"):
        print("hosts with recorded checks:", r["n"])

    print("\n== fetch jobs ==")
    for r in conn.execute("SELECT source_id, job_type, status, COUNT(*) n FROM fetch_jobs "
                          "GROUP BY 1,2,3 ORDER BY 1,2,3"):
        print(f"{r['source_id']:<16} {r['job_type']:<14} {r['status']:<10} {r['n']}")

    print("\n== source status ==")
    for r in conn.execute("SELECT name, status, access_method FROM sources ORDER BY name"):
        print(f"{r['name']:<16} {r['status']:<12} {r['access_method']}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/demoscene.sqlite3")
