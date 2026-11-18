"""Second synthetic dataset for trying out the chatbot (the first is CHATBOT_TEST_DEMO).

    python scripts/seed_chatbot_demo_2.py            # create (or re-create) it
    python scripts/seed_chatbot_demo_2.py --remove   # delete it

Everything in it is invented. It differs from the first demo on purpose, so questions can tell
them apart: six sites in a made-up country, two campaigns a year apart, two depths, six variables
(not only pH), two regions with different soils, an agroforestry effect on organic matter, and
**two sites without coordinates** that sit at the centre of their municipality
(location_precision = ADMIN_2), like a real upload with admin 1 / admin 2 only.

Test data: remove it (``--remove``) before the platform's data goes to production.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv()

from sqlalchemy import func  # noqa: E402

from mflsoildata_orm.database import SessionLocal  # noqa: E402
from mflsoildata_orm.models import (  # noqa: E402
    Measurement,
    Project,
    ProjectMetadata,
    Sample,
    SampleType,
    SamplingEvent,
    Site,
    Variable,
)

CODE = "CHATBOT_TEST_DEMO_2"
COUNTRY = "Cafelandia"

# code, admin_1, admin_2, (lat, lon) or None for a site without coordinates, system
SITES = [
    ("CF-01", "Región Norte", "Municipio Alfa", (6.31, -75.62), "agroforestal"),
    ("CF-02", "Región Norte", "Municipio Alfa", (6.34, -75.58), "pleno sol"),
    ("CF-03", "Región Norte", "Municipio Beta", (6.52, -75.71), "agroforestal"),
    ("CF-04", "Región Sur", "Municipio Gamma", (5.88, -75.44), "pleno sol"),
    ("CF-05", "Región Sur", "Municipio Gamma", None, "agroforestal"),  # no coordinates
    ("CF-06", "Región Sur", "Municipio Delta", None, "pleno sol"),  # no coordinates
]
# where an admin-only site is drawn: the centre of its municipality (made up, like the country)
CENTRES = {"Municipio Gamma": (5.91, -75.40), "Municipio Delta": (5.72, -75.55)}
CAMPAIGNS = [date(2025, 3, 10), date(2026, 3, 12)]
DEPTHS = [("0-20", 0), ("20-40", 1)]
VARIABLES = {  # code: (mean, sd)
    "SOIL_PH_UN": (5.4, 0.35),
    "SOIL_MO_G_KG": (42.0, 6.0),
    "SOIL_N_TOTAL": (0.28, 0.05),
    "SOIL_P_DISP_MG_KG": (14.0, 4.0),
    "SOIL_K_CMOL_KG": (0.45, 0.12),
    "SOIL_ARCILLA": (36.0, 7.0),
}


def remove(db) -> None:
    project = db.query(Project).filter(Project.code == CODE).first()
    if not project:
        return
    site_ids = [s.id for s in db.query(Site.id).filter(Site.project_id == project.id)]
    sample_ids = [s.id for s in db.query(Sample.id).filter(Sample.site_id.in_(site_ids))] if site_ids else []
    if sample_ids:
        db.query(Measurement).filter(Measurement.sample_id.in_(sample_ids)).delete(synchronize_session=False)
        db.query(Sample).filter(Sample.id.in_(sample_ids)).delete(synchronize_session=False)
    db.query(SamplingEvent).filter(SamplingEvent.project_id == project.id).delete(synchronize_session=False)
    db.query(Site).filter(Site.project_id == project.id).delete(synchronize_session=False)
    db.query(ProjectMetadata).filter(ProjectMetadata.project_id == project.id).delete(synchronize_session=False)
    db.delete(project)
    db.flush()


def create(db) -> dict:
    rng = random.Random(20260617)
    variables = {v.variable_code: v for v in db.query(Variable).filter(Variable.variable_code.in_(VARIABLES))}
    missing = set(VARIABLES) - set(variables)
    if missing:
        raise SystemExit(f"Variables not in the catalogue: {sorted(missing)}")
    soil = db.query(SampleType).filter(SampleType.sample_type_code == "SOIL").one()

    project = Project(
        code=CODE,
        name="[PRUEBA] Demo 2 del chatbot — café y agroforestería",
        country=COUNTRY,
        description="Proyecto ficticio creado solo para probar el asistente de IA (chatbot). No contiene datos reales.",
    )
    db.add(project)
    db.flush()

    counts = {"sites": 0, "events": 0, "samples": 0, "measurements": 0}
    for code, admin_1, admin_2, coords, system in SITES:
        precision, (lat, lon) = ("EXACT", coords) if coords else ("ADMIN_2", CENTRES[admin_2])
        site = Site(
            project_id=project.id, site_code=code, country=COUNTRY, admin_1=admin_1, admin_2=admin_2,
            geom=func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), location_precision=precision,
            altitude_m=1200 + 40 * len(code) + rng.randint(-60, 60),
            notes={"chatbot_test_data": True, "sistema": system},
        )
        db.add(site)
        db.flush()
        counts["sites"] += 1
        for campaign in CAMPAIGNS:
            event = SamplingEvent(
                project_id=project.id, site_id=site.id, event_date=campaign, event_type="Muestreo",
                activity=f"Campaña {campaign.year}", organization="Equipo MFL", responsible="Datos sintéticos",
            )
            db.add(event)
            db.flush()
            counts["events"] += 1
            for depth, level in DEPTHS:
                sample = Sample(
                    event_id=event.id, site_id=site.id, sample_type_id=soil.id,
                    sample_code=f"{code}-{campaign.year}-{depth}", notes={"profundidad_cm": depth},
                )
                db.add(sample)
                db.flush()
                counts["samples"] += 1
                for var_code, (mean, sd) in VARIABLES.items():
                    value = rng.gauss(mean, sd)
                    if admin_1 == "Región Sur":  # the south is more acid and sandier
                        value += {"SOIL_PH_UN": -0.6, "SOIL_ARCILLA": -9.0, "SOIL_K_CMOL_KG": -0.1}.get(var_code, 0)
                    if var_code == "SOIL_MO_G_KG":
                        value *= 1.0 - 0.35 * level  # organic matter falls with depth
                        value *= 1.18 if system == "agroforestal" else 1.0
                        value *= 1.06 if campaign.year == 2026 else 1.0  # and has built up since 2025
                    if var_code == "SOIL_N_TOTAL":
                        value *= 1.0 - 0.3 * level
                    variable = variables[var_code]
                    db.add(Measurement(
                        sample_id=sample.id, variable_id=variable.id, unit_id=variable.default_unit_id,
                        value_numeric=round(max(value, 0.01), 2),
                    ))
                    counts["measurements"] += 1

    db.add(ProjectMetadata(
        project_id=project.id,
        title="[DATOS DE PRUEBA] Demo 2 del chatbot — café y agroforestería (no son datos reales)",
        abstract=(
            "Dataset ficticio para probar el asistente de IA (chatbot). Seis sitios de café en un país inventado, "
            "con dos campañas (2025 y 2026), dos profundidades y seis variables de suelo (pH, materia orgánica, "
            "nitrógeno total, fósforo disponible, potasio y arcilla). Dos sitios no tienen coordenadas y se ubican "
            "en el centro de su municipio. Todos los valores son inventados."
        ),
        keywords=[{"uri": None, "label": k} for k in ("prueba", "demo", "chatbot", "café", "agroforestería")],
        expected_matrices=["SOIL"],
        temporal_coverage_start=CAMPAIGNS[0],
        temporal_coverage_end=CAMPAIGNS[-1],
        spatial_coverage="Cafelandia (país ficticio): Región Norte y Región Sur",
        sampling_design="Seis fincas, dos con y cuatro sin sombra; muestreo puntual a 0-20 y 20-40 cm, en marzo de 2025 y de 2026.",
        access_level="PUBLIC",
        license="CC-BY-4.0",
        language="es",
        sampling_type=["SINGLE_POINT"],
        data_source="Equipo MFL (datos sintéticos)",
        contact_name="Equipo MFL",
        contact_email="mfl-test@example.org",
        quality_controlled=False,
        qc_status="NOT_REVIEWED",
        notes={"chatbot_test_data": True},
    ))
    db.flush()
    return counts


def main(argv: list[str]) -> int:
    db = SessionLocal()
    try:
        remove(db)
        if "--remove" in argv:
            db.commit()
            print(f"{CODE} removed.")
            return 0
        counts = create(db)
        db.commit()
        print(f"{CODE} created: {counts}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
