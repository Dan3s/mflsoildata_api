"""Cascading-filter endpoints: what each selector may offer under the others' values.

The tests plant two small projects in different countries inside a transaction that is
always rolled back, so they behave the same on an empty CI database and on a dev one with
real data (every assertion is about the planted rows). Skipped when the database is not
reachable.
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text

from dependencies.auth import CurrentUser, get_current_user, get_optional_user
from main import app
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import (
    Measurement,
    Project,
    ProjectMetadata,
    Sample,
    SampleType,
    SamplingEvent,
    Site,
    Variable,
)
from routes import download as download_routes
from routes import map as map_routes

COUNTRY_A, COUNTRY_B = "Zzland", "Yyland"
INSTITUTE = "Zz Institute"


@pytest.fixture
def session(monkeypatch):
    db = SessionLocal()
    try:
        db.execute(text("select 1"))
    except Exception:
        db.close()
        pytest.skip("database not reachable")
    db.commit = db.flush  # type: ignore[method-assign]
    real_close, db.close = db.close, (lambda: None)  # type: ignore[method-assign]
    monkeypatch.setattr(download_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(map_routes, "SessionLocal", lambda: db)
    yield db
    db.rollback()
    real_close()


@pytest.fixture
def world(session):
    """Project A (Zzland, dated 2020-05-01, variable A) and project B (Yyland, undated, variable B)."""
    sample_type = SampleType(sample_type_code="ZZ_FACETS", name="Zz facets")
    session.add(sample_type)

    def project(code, country, event_date, variable_code, institute=None):
        proj = Project(code=code, name=code)
        session.add(proj)
        session.flush()
        if institute:
            session.add(ProjectMetadata(project_id=proj.id, data_source=institute))
        geom = func.ST_SetSRID(func.ST_MakePoint(-74.0, 4.6), 4326)  # sites.geom is NOT NULL
        site = Site(project_id=proj.id, site_code=f"{code}_S1", country=country, geom=geom)
        session.add(site)
        session.flush()
        event = SamplingEvent(project_id=proj.id, site_id=site.id, event_date=event_date)
        session.add(event)
        session.flush()
        sample = Sample(event_id=event.id, site_id=site.id, sample_type_id=sample_type.id, sample_code="1")
        session.add(sample)
        variable = Variable(variable_code=variable_code, name=variable_code, domain="LAB_RESULT", value_type="numeric", matrix="SOIL")
        session.add(variable)
        session.flush()
        session.add(Measurement(sample_id=sample.id, variable_id=variable.id, value_numeric=1.0))
        session.flush()
        return proj, variable

    proj_a, var_a = project("ZZ_FACETS_A", COUNTRY_A, date(2020, 5, 1), "ZZ_FACETS_VAR_A", INSTITUTE)
    proj_b, var_b = project("ZZ_FACETS_B", COUNTRY_B, None, "ZZ_FACETS_VAR_B")
    return {"a": proj_a, "b": proj_b, "var_a": var_a, "var_b": var_b}


@pytest.fixture
def client(session):
    user = CurrentUser(sub="t", email=None, username="tester", roles=["admin"])
    for dep in (get_current_user, get_optional_user):
        app.dependency_overrides[dep] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_download_facets_by_country_offer_only_that_countrys_data(client, world):
    facets = client.get("/download/facets", params={"country": COUNTRY_A}).json()
    assert facets["project_ids"] == [world["a"].id]
    assert facets["variable_ids"] == [world["var_a"].id]
    assert facets["variable_counts"] == {str(world["var_a"].id): 1}
    assert facets["date_min"] == facets["date_max"] == "2020-05-01"


def test_download_facets_choosing_a_project_narrows_variables_but_not_the_project_list(client, world):
    facets = client.get("/download/facets", params={"project_ids": [world["a"].id]}).json()
    # variables follow the chosen project ...
    assert world["var_a"].id in facets["variable_ids"] and world["var_b"].id not in facets["variable_ids"]
    # ... but the other project stays reachable, so the person is never trapped
    assert {world["a"].id, world["b"].id} <= set(facets["project_ids"])


def test_download_facets_report_no_dates_when_nothing_chosen_has_one(client, world):
    facets = client.get("/download/facets", params={"project_ids": [world["b"].id]}).json()
    assert facets["variable_ids"] == [world["var_b"].id]
    assert facets["date_min"] is None and facets["date_max"] is None


def test_download_facets_institution_filter_keeps_only_that_institutions_projects(client, world):
    facets = client.get("/download/facets", params={"institution": INSTITUTE}).json()
    assert facets["project_ids"] == [world["a"].id]


def test_download_facets_for_a_country_with_no_data_are_empty(client, world):
    facets = client.get("/download/facets", params={"country": "Atlantis"}).json()
    assert facets["project_ids"] == [] and facets["variable_ids"] == []
    assert facets["date_min"] is None


def test_map_facets_projects_narrow_with_the_country_but_countries_do_not(client, world):
    facets = client.get("/map/facets", params={"country": COUNTRY_A}).json()
    assert facets["projects"] == [{"id": world["a"].id, "count": 1}]
    # the country list leaves its own filter out: the other country is still on offer
    offered = {c["value"]: c["count"] for c in facets["countries"]}
    assert offered[COUNTRY_A] == 1 and offered[COUNTRY_B] == 1


def test_map_facets_countries_narrow_with_the_project(client, world):
    facets = client.get("/map/facets", params={"project_id": world["b"].id}).json()
    assert [c["value"] for c in facets["countries"]] == [COUNTRY_B]
    # and the project list still offers the other project
    assert {p["id"] for p in facets["projects"]} >= {world["a"].id, world["b"].id}


def test_map_facets_institutions_follow_the_other_filters(client, world):
    in_a = client.get("/map/facets", params={"country": COUNTRY_A}).json()
    in_b = client.get("/map/facets", params={"country": COUNTRY_B}).json()
    assert INSTITUTE in in_a["institutions"]
    assert INSTITUTE not in in_b["institutions"]


def test_map_facets_unknown_institution_offers_nothing(client, world):
    facets = client.get("/map/facets", params={"institution": "Nowhere Institute"}).json()
    assert facets["projects"] == [] and facets["countries"] == []
