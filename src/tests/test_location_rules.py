"""A site needs coordinates OR its admin 1 + admin 2; without coordinates it is placed at the
centre of its area and flagged as approximate.

The rule itself is checked without a database (with a stand-in for the gazetteer); the gazetteer
lookups and the placement on import run against the dev database and are skipped when it is not
reachable or has no admin areas loaded (``scripts/load_admin_areas.py``).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from sqlalchemy import text

from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import Project, Site
from services import etl
from services.admin_areas import AdminResolver, Placement, Unplaced, country_to_iso3, normalize_name

COLUMNS = [
    "project_code", "project_name", "site_code", "event_date", "sample_code", "sample_type_code",
    "latitude", "longitude", "admin_1", "admin_2", "site_country",
]


def _parse(*rows, columns=COLUMNS):
    csv = ",".join(columns) + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
    return etl.parse_file(csv.encode("utf-8"), "f.csv")


def _issues(parsed, resolver=None, project_countries=None):
    specs, _ = etl.resolve_columns(None, parsed.column_ids)
    issues, _, _ = etl.validate(parsed, specs, {"SOIL"}, admin_resolver=resolver, project_countries=project_countries)
    return issues


def _codes(issues, severity=None):
    return sorted(i["code"] for i in issues if severity is None or i["severity"] == severity)


BASE = ["P1", "Project", "S1", "2024-03-01", "M1", "SOIL"]


class FakeResolver:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def resolve(self, country, admin_1, admin_2):
        self.asked.append((country, admin_1, admin_2))
        return self.answer


# --- the rule -------------------------------------------------------------------------------

def test_coordinates_alone_are_enough():
    parsed = _parse(BASE + ["4.6", "-74.0", "", "", ""])
    assert _codes(_issues(parsed)) == []


def test_admin_levels_alone_are_enough_and_the_site_is_flagged_as_approximate():
    resolver = FakeResolver(Placement(1.85, -76.05, "ADMIN_2", "Pitalito"))
    parsed = _parse(BASE + ["", "", "Huila", "Pitalito", "Colombia"])
    issues = _issues(parsed, resolver)
    assert _codes(issues, "error") == []
    assert _codes(issues, "warning") == ["APPROXIMATE_LOCATION"]
    assert resolver.asked == [("Colombia", "Huila", "Pitalito")]


def test_neither_coordinates_nor_admin_levels_is_an_error():
    issues = _issues(_parse(BASE + ["", "", "", "", "Colombia"]))
    assert _codes(issues, "error") == ["LOCATION_REQUIRED"]


def test_one_admin_level_is_not_enough():
    issues = _issues(_parse(BASE + ["", "", "Huila", "", "Colombia"]))
    assert _codes(issues, "error") == ["LOCATION_REQUIRED"]


def test_latitude_without_longitude_is_an_error_even_with_admin_levels():
    issues = _issues(_parse(BASE + ["4.6", "", "Huila", "Pitalito", "Colombia"]))
    assert _codes(issues, "error") == ["COORDINATES_INCOMPLETE"]


def test_coordinates_win_over_admin_levels_and_nothing_is_looked_up():
    resolver = FakeResolver(Unplaced("NOT_FOUND", "x"))
    parsed = _parse(BASE + ["4.6", "-74.0", "Huila", "Nowhere", "Colombia"])
    assert _codes(_issues(parsed, resolver)) == []
    assert resolver.asked == []


def test_an_area_that_cannot_be_placed_is_an_error_that_says_so():
    resolver = FakeResolver(Unplaced("NOT_FOUND", "'Huila / Nowhere' is not a known area of Colombia"))
    issues = _issues(_parse(BASE + ["", "", "Huila", "Nowhere", "Colombia"]), resolver)
    [issue] = [i for i in issues if i["severity"] == "error"]
    assert issue["code"] == "ADMIN_NOT_FOUND" and "Nowhere" in issue["message"] and "coordinates" in issue["message"]


def test_a_missing_country_has_its_own_error():
    resolver = FakeResolver(Unplaced("NO_COUNTRY", "the country is not given"))
    issues = _issues(_parse(BASE + ["", "", "Huila", "Pitalito", ""]), resolver)
    assert _codes(issues, "error") == ["COUNTRY_REQUIRED"]


def test_the_country_of_an_existing_project_is_used_when_the_file_gives_none():
    resolver = FakeResolver(Placement(1.85, -76.05, "ADMIN_2", "Pitalito"))
    _issues(_parse(BASE + ["", "", "Huila", "Pitalito", ""]), resolver, project_countries={"P1": "Colombia"})
    assert resolver.asked == [("Colombia", "Huila", "Pitalito")]


def test_falling_back_to_the_department_is_only_a_warning():
    resolver = FakeResolver(Placement(2.5, -75.5, "ADMIN_1", "Huila", "'Pitalto' was not found in Huila"))
    issues = _issues(_parse(BASE + ["", "", "Huila", "Pitalto", "Colombia"]), resolver)
    assert _codes(issues, "error") == [] and _codes(issues, "warning") == ["ADMIN_LEVEL_FALLBACK"]


def test_a_file_with_no_location_columns_gets_one_finding_not_one_per_row():
    cols = ["project_code", "project_name", "site_code", "event_date", "sample_code", "sample_type_code"]
    rows = [[*BASE[:4], f"M{i}", "SOIL"] for i in range(3)]
    issues = _issues(_parse(*rows, columns=cols))
    assert _codes(issues, "error") == ["LOCATION_COLUMNS_MISSING"]


def test_without_a_gazetteer_only_the_rule_is_checked():
    assert _codes(_issues(_parse(BASE + ["", "", "Huila", "Pitalito", "Colombia"]))) == []


def test_latitude_and_longitude_are_no_longer_required_on_their_own():
    required = set(etl.REQUIRED_STRUCTURAL_IDS)
    assert not {"latitude", "longitude", "admin_1", "admin_2"} & required
    group = {c.id for c in etl.STRUCTURAL_COLUMNS if c.required_one_of == "location"}
    assert group == {"latitude", "longitude", "admin_1", "admin_2"}


# --- names ----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Pitalito", "pitalito"), ("  PITALITO ", "pitalito"), ("Municipio de Pitalito", "pitalito"),
        ("San José del Guaviare", "san jose del guaviare"), ("Bogotá, D.C.", "bogota"),
        ("Departamento del Huila", "huila"), ("Cantón Arajuno", "arajuno"), (None, ""), ("", ""),
    ],
)
def test_names_are_compared_without_case_accents_or_the_kind_of_area(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "raw, iso3", [("Colombia", "COL"), ("PERÚ", "PER"), ("peru", "PER"), ("Ecuador", "ECU"), ("col", "COL"), ("Narnia", None), ("", None)],
)
def test_countries_by_name_or_code(raw, iso3):
    assert country_to_iso3(raw) == iso3


# --- against the gazetteer ------------------------------------------------------------------

@pytest.fixture
def db():
    session = SessionLocal()
    try:
        loaded = session.execute(text("select count(*) from admin_areas where iso3 = 'COL'")).scalar()
    except Exception:
        session.close()
        pytest.skip("database not reachable or migration 0013 not applied")
    if not loaded:
        session.close()
        pytest.skip("no admin areas loaded: run scripts/load_admin_areas.py COL")
    yield session
    session.rollback()
    session.close()


def test_a_municipality_is_found_however_it_is_written(db):
    resolver = AdminResolver(db)
    for country, a1, a2 in [("Colombia", "Huila", "Pitalito"), ("COLOMBIA", "HUILA", "pitalito"), ("Colombia", "Huila", "Municipio de Pitalito")]:
        found = resolver.resolve(country, a1, a2)
        assert isinstance(found, Placement) and found.precision == "ADMIN_2"
        assert 1.5 < found.lat < 2.2 and -76.5 < found.lon < -75.5  # Pitalito, Huila


def test_a_small_misspelling_is_tolerated_but_not_a_different_place(db):
    resolver = AdminResolver(db)
    assert resolver.resolve("Colombia", "Huila", "Pitalto").area == "Pitalito"
    assert isinstance(resolver.resolve("Colombia", "Huila", "Atlantis"), Placement)  # falls back to Huila...
    assert resolver.resolve("Colombia", "Huila", "Atlantis").precision == "ADMIN_1"


def test_the_department_tells_two_municipalities_of_the_same_name_apart(db):
    resolver = AdminResolver(db)
    names = db.execute(text(
        "select name_norm from admin_areas where iso3='COL' and level=2 group by name_norm having count(*) > 1 limit 1"
    )).scalar()
    if not names:
        pytest.skip("no repeated municipality names")
    parents = [r[0] for r in db.execute(text(
        "select parent_name_norm from admin_areas where iso3='COL' and level=2 and name_norm=:n"), {"n": names})]
    points = {resolver.resolve("Colombia", parent, names).lat for parent in parents}
    assert len(points) == len(set(parents))


def test_unknown_places_and_countries_say_why(db):
    resolver = AdminResolver(db)
    assert resolver.resolve("Colombia", "Nowhere", "Nothing").reason == "NOT_FOUND"
    assert resolver.resolve("Narnia", "A", "B").reason == "COUNTRY_NOT_LOADED"
    assert resolver.resolve(None, "A", "B").reason == "NO_COUNTRY"


def test_a_site_without_coordinates_is_stored_at_its_area_flagged_approximate(db):
    project = Project(code="TST_LOC_DS", name="Location test")
    db.add(project)
    db.flush()
    inserted = {"sites": 0}
    row = {"site_country": "Colombia", "admin_1": "Huila", "admin_2": "Pitalito"}
    site = etl._get_or_create_site(db, {}, project, "S1", None, None, row, inserted, AdminResolver(db))
    db.flush()
    lat = db.execute(text("select st_y(geom) from sites where id=:i"), {"i": site.id}).scalar()
    assert site.location_precision == "ADMIN_2" and 1.5 < lat < 2.2

    exact = etl._get_or_create_site(db, {}, project, "S2", 4.6, -74.0, {}, inserted, AdminResolver(db))
    db.flush()
    assert exact.location_precision == "EXACT"

    with pytest.raises(ValueError, match="could not be placed"):
        etl._get_or_create_site(db, {}, project, "S3", None, None, {"site_country": "Colombia", "admin_1": "X", "admin_2": "Y"}, inserted, AdminResolver(db))


# --- through the real route -----------------------------------------------------------------

def _template_with_rows(client, rows):
    """The platform's own template (coordinates and admin levels), with one dict per row filled in."""
    import io

    from openpyxl import load_workbook

    cols = [*etl.REQUIRED_STRUCTURAL_IDS, "latitude", "longitude", "admin_1", "admin_2", "site_country"]
    resp = client.get("/upload/template", params={"columns": cols, "lang": "es"})
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb["Template"]
    ids = [c.value for c in ws[2]]
    for n, row in enumerate(rows, start=1):
        full = {
            "project_code": "TST_LOC_ROUTE", "project_name": "Route test", "site_code": f"S-{n}",
            "event_date": "2024-04-15", "sample_code": f"M-{n}", "sample_type_code": "SOIL", **row,
        }
        ws.append([full.get(i, "") for i in ids])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from dependencies.auth import CurrentUser, get_current_user, require_database_editor, require_editor
    from main import app
    from routes import upload as upload_routes

    db.commit = db.flush  # type: ignore[method-assign]
    real_close, db.close = db.close, (lambda: None)  # type: ignore[method-assign]
    monkeypatch.setattr(upload_routes, "SessionLocal", lambda: db)
    user = CurrentUser(sub="t", email=None, username="tester", roles=["admin"])
    for dep in (get_current_user, require_editor, require_database_editor):
        app.dependency_overrides[dep] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()
    db.close = real_close  # type: ignore[method-assign]


def _validate(client, content):
    return client.post(
        "/upload/validate",
        files={"file": ("t.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ).json()


def test_a_file_without_coordinates_validates_when_its_admin_areas_are_known(client):
    content = _template_with_rows(client, [
        {"admin_1": "Huila", "admin_2": "Pitalito", "site_country": "Colombia"},
        {"admin_1": "Nariño", "admin_2": "Pasto", "site_country": "Colombia"},
    ])
    body = _validate(client, content)
    assert body["error_count"] == 0, body["issues"]
    assert {i["code"] for i in body["issues"] if i["severity"] == "warning"} == {"APPROXIMATE_LOCATION"}


def test_the_route_refuses_a_site_it_cannot_locate(client):
    content = _template_with_rows(client, [
        {"admin_1": "Huila", "admin_2": "Pitalito", "site_country": "Colombia"},  # fine
        {"admin_1": "Atlantis", "admin_2": "Poseidonia", "site_country": "Colombia"},  # unknown
        {"site_country": "Colombia"},  # nothing
        {"latitude": "4.6", "admin_1": "Huila", "admin_2": "Pitalito", "site_country": "Colombia"},  # half coordinates
    ])
    errors = {(i["row"], i["code"]) for i in _validate(client, content)["issues"] if i["severity"] == "error"}
    codes = {code for _, code in errors}
    assert {"ADMIN_NOT_FOUND", "LOCATION_REQUIRED", "COORDINATES_INCOMPLETE"} <= codes


def test_the_template_asks_for_the_location_as_a_group(client):
    groups = client.get("/upload/template-columns").json()["groups"]
    cols = {c["id"]: c for g in groups for c in g["columns"]}
    for cid in ("latitude", "longitude", "admin_1", "admin_2"):
        assert cols[cid]["required"] is False and cols[cid]["required_one_of"] == "location"
    assert cols["site_code"]["required"] is True and cols["site_code"].get("required_one_of") is None


def test_a_finding_about_a_column_carries_the_name_the_template_gives_it(client):
    content = _template_with_rows(client, [
        {"project_code": "", "latitude": "4.6", "longitude": "-74.0", "site_country": "Colombia"},
    ])
    issues = _validate(client, content)["issues"]
    [required] = [i for i in issues if i["code"] == "REQUIRED" and i["column"] == "project_code"]
    assert required["column_label"] == "Project Code"
