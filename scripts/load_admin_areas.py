"""Load the administrative-area gazetteer (admin_areas) from geoBoundaries.

    python scripts/load_admin_areas.py COL PER ECU

For each country (ISO3) it downloads the simplified ADM1 (department/state) and ADM2
(municipality/province/canton) boundaries of geoBoundaries gbOpen, stores for each area a point
that lies inside it, and works out which ADM1 each ADM2 belongs to (geoBoundaries does not say).
Re-running a country replaces what was loaded for it. Needs DATABASE_URL (from .env) and internet.

What ADM2 means is each country's own: municipality (Colombia), province (Peru), canton
(Ecuador). Boundaries: geoBoundaries (https://www.geoboundaries.org), CC BY 4.0 — credit it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv()

from sqlalchemy import text  # noqa: E402

from mflsoildata_orm.database import SessionLocal  # noqa: E402
from services.admin_areas import normalize_name  # noqa: E402

API = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/ADM{level}/"
SOURCE = "geoBoundaries gbOpen (CC BY 4.0)"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (fixed https hosts)
        return json.loads(resp.read().decode("utf-8"))


def _fetch_level(iso3: str, level: int) -> tuple[str, list[dict]]:
    meta = _get_json(API.format(iso3=iso3, level=level))
    url = meta.get("simplifiedGeometryGeoJSON") or meta["gjDownloadURL"]
    features = _get_json(url)["features"]
    return meta.get("boundaryName") or iso3, features


def load_country(db, iso3: str) -> None:
    iso3 = iso3.upper()
    print(f"{iso3}: downloading ADM1 and ADM2 ...")
    country, adm1 = _fetch_level(iso3, 1)
    _, adm2 = _fetch_level(iso3, 2)

    db.execute(text("DELETE FROM admin_areas WHERE iso3 = :iso3"), {"iso3": iso3})
    db.execute(text("CREATE TEMP TABLE IF NOT EXISTS _adm1 (name_norm text, geom geometry) ON COMMIT DROP"))
    db.execute(text("DELETE FROM _adm1"))

    point = "ST_PointOnSurface(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326))"
    for f in adm1:
        name = (f["properties"].get("shapeName") or "").strip()
        if not name:
            continue
        geo = json.dumps(f["geometry"])
        db.execute(
            text(
                "INSERT INTO admin_areas (iso3, country, level, name, name_norm, parent_name_norm, centroid, source) "
                f"VALUES (:iso3, :country, 1, :name, :norm, NULL, {point}, :source)"
            ),
            {"iso3": iso3, "country": country, "name": name, "norm": normalize_name(name), "g": geo, "source": SOURCE},
        )
        db.execute(
            text("INSERT INTO _adm1 (name_norm, geom) VALUES (:norm, ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326))"),
            {"norm": normalize_name(name), "g": geo},
        )

    for f in adm2:
        name = (f["properties"].get("shapeName") or "").strip()
        if not name:
            continue
        db.execute(
            text(
                "WITH p AS (SELECT " + point + " AS pt) "
                "INSERT INTO admin_areas (iso3, country, level, name, name_norm, parent_name_norm, centroid, source) "
                "SELECT :iso3, :country, 2, :name, :norm, "
                "(SELECT a.name_norm FROM _adm1 a WHERE ST_Intersects(a.geom, p.pt) LIMIT 1), p.pt, :source FROM p"
            ),
            {
                "iso3": iso3, "country": country, "name": name, "norm": normalize_name(name),
                "g": json.dumps(f["geometry"]), "source": SOURCE,
            },
        )

    no_parent = db.execute(
        text("SELECT count(*) FROM admin_areas WHERE iso3 = :iso3 AND level = 2 AND parent_name_norm IS NULL"),
        {"iso3": iso3},
    ).scalar()
    db.commit()
    print(f"{iso3}: {country} - {len(adm1)} ADM1, {len(adm2)} ADM2 loaded ({no_parent} ADM2 without a parent).")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    db = SessionLocal()
    try:
        for iso3 in argv:
            load_country(db, iso3)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
