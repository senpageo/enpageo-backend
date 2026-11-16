"""Exportiert eine Region (Bezirk oder ganz Berlin) zellweise aus der Datenbank, nur lesend.

    python export_region.py --region Neukölln --out data/neukoelln [--jobs 2]
    python export_region.py --region "*" --out data/berlin          # ganz Berlin

Je 1-km-Zelle (EPSG:25833) eine Datei <out>/<i>_<j>.csv aus export_cell.sql. Wiederaufnehmbar: Zellen,
deren Datei mit der Prüfmarke #END endet, werden übersprungen. Eine Datei ohne #END gilt als kaputt
(abgebrochene Verbindung) und wird neu geholt; erst nach erfolgreichem Lauf wird .tmp umbenannt.

Datenbankzugang an genau einer Stelle: Umgebungsvariable PSQL_CMD (Standard: psql im DB-Container auf
der VPS über ssh). Zieht die DB um, nur PSQL_CMD ändern, z. B. PSQL_CMD='psql service=enpageo_ro'.
"""
import argparse
import csv
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
CELL = 1000
DEFAULT_PSQL = "ssh -o BatchMode=yes enpageo-vps docker exec -i enpageo_db psql -U enpageo -d enpageo"


def psql(args, sql: bytes, timeout=1800) -> bytes:
    cmd = os.environ.get("PSQL_CMD", DEFAULT_PSQL)
    base = shlex.split(cmd)
    # über ssh landen die Argumente in einer entfernten Shell, deshalb dort quoten
    extra = [shlex.quote(a) for a in args] if base[0] == "ssh" else args
    res = subprocess.run(base + extra, input=sql, capture_output=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace").strip())
    return res.stdout


def list_cells(region: str):
    sql = f"""
    SET default_transaction_read_only = on;
    WITH region AS (
        SELECT geom FROM emc.district WHERE name = :'region'
        UNION ALL SELECT geom FROM emc.city WHERE :'region' = '*' AND name = 'Berlin'
    ), env AS (SELECT ST_Extent(geom) AS e FROM region)
    SELECT i, j FROM env,
         generate_series(floor(ST_XMin(e) / {CELL})::int, floor(ST_XMax(e) / {CELL})::int) i,
         generate_series(floor(ST_YMin(e) / {CELL})::int, floor(ST_YMax(e) / {CELL})::int) j
    WHERE EXISTS (SELECT 1 FROM region r
                  WHERE ST_Intersects(r.geom, ST_MakeEnvelope(i * {CELL}, j * {CELL}, (i + 1) * {CELL}, (j + 1) * {CELL}, 25833)))
    ORDER BY i, j;
    """
    out = psql(["-At", "-F", ",", "-q", "-v", f"region={region}"], sql.encode())
    return [tuple(map(int, line.split(","))) for line in out.decode().split() if line]


def complete(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 10:
        return False
    with open(path, "rb") as f:
        f.seek(-8, os.SEEK_END)
        return f.read().strip().endswith(b"#END")


def export_cell(i, j, region, out: Path, sql: bytes, retries=2):
    target = out / f"{i}_{j}.csv"
    if complete(target):
        return i, j, "skip", 0.0, target.stat().st_size
    tmp = target.with_suffix(".tmp")
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            data = psql(["-q", "-v", "ON_ERROR_STOP=1", "-v", f"x0={i * CELL}", "-v", f"y0={j * CELL}",
                         "-v", f"size={CELL}", "-v", f"region={region}"], sql)
            tmp.write_bytes(data)
            if not complete(tmp):
                raise RuntimeError("Prüfmarke #END fehlt")
            tmp.replace(target)
            return i, j, "ok", time.time() - t0, target.stat().st_size
        except Exception as e:  # noqa: BLE001 - jede Zelle soll unabhängig scheitern dürfen
            err = e
            time.sleep(2 * (attempt + 1))
    return i, j, f"FEHLER: {err}", 0.0, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True, help="Bezirksname aus emc.district oder '*' für Berlin")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=2, help="parallele Abfragen (VPS hat 2 Kerne)")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sql = (HERE / "export_cell.sql").read_bytes()
    cells = list_cells(args.region)
    print(f"{len(cells)} Zellen für Region {args.region!r}", flush=True)

    t0 = time.time()
    failed = 0
    with open(out / "export_log.csv", "a", newline="", encoding="utf-8") as logf, \
            ThreadPoolExecutor(max_workers=args.jobs) as pool:
        log = csv.writer(logf)
        futures = [pool.submit(export_cell, i, j, args.region, out, sql) for i, j in cells]
        for n, fut in enumerate(as_completed(futures), 1):
            i, j, status, secs, size = fut.result()
            failed += status.startswith("FEHLER")
            log.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), i, j, status, round(secs, 1), size])
            logf.flush()
            print(f"[{n}/{len(cells)}] {i}_{j} {status} {secs:.1f}s {size / 1e6:.1f} MB", flush=True)
    print(f"fertig in {time.time() - t0:.0f} s, {failed} Zellen fehlgeschlagen", flush=True)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
