"""Cascading-filter endpoints: what each selector may offer under the others' values.

Read-only against the dev database (skipped when it is not reachable). The properties
checked are the ones the UI relies on, whatever data happens to be there.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from dependencies.auth import CurrentUser, get_current_user, get_optional_user
from main import app
from mflsoildata_orm.database import SessionLocal


@pytest.fixture
def client():
    db = SessionLocal()
    try:
        db.execute(text("select 1"))
    except Exception:
        pytest.skip("database not reachable")
    finally:
        db.close()
    user = CurrentUser(sub="t", email=None, username="tester", roles=["admin"])
    for dep in (get_current_user, get_optional_user):
        app.dependency_overrides[dep] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_download_facets_narrow_with_the_chosen_project(client):
    everything = client.get("/download/facets").json()
    assert everything["project_ids"], "the dev database has no data to test against"
    project = everything["project_ids"][0]
    one = client.get("/download/facets", params={"project_ids": [project]}).json()

    assert set(one["variable_ids"]) <= set(everything["variable_ids"])
    # picking a project never shrinks the project list to itself: the others stay reachable
    assert one["project_ids"] == everything["project_ids"]
    assert set(one["variable_counts"]) == {str(v) for v in one["variable_ids"]}
    if one["date_min"] and one["date_max"]:
        assert one["date_min"] <= one["date_max"]


def test_download_facets_country_filter_removes_projects_without_sites_there(client):
    everything = client.get("/download/facets").json()
    narrowed = client.get("/download/facets", params={"country": "Atlantis"}).json()
    assert narrowed["project_ids"] == [] and narrowed["variable_ids"] == []
    assert narrowed["date_min"] is None
    assert everything["project_ids"]


def test_map_facets_each_list_ignores_its_own_filter(client):
    everything = client.get("/map/facets").json()
    country = everything["countries"][0]["value"]
    in_country = client.get("/map/facets", params={"country": country}).json()

    # the country list still offers every country (its own filter is left out) ...
    assert in_country["countries"] == everything["countries"]
    # ... while the projects narrow to those with sites there
    ids_everything = {p["id"] for p in everything["projects"]}
    ids_country = {p["id"] for p in in_country["projects"]}
    assert ids_country <= ids_everything and ids_country
    assert sum(p["count"] for p in in_country["projects"]) <= sum(p["count"] for p in everything["projects"])


def test_map_facets_unknown_institution_offers_nothing(client):
    facets = client.get("/map/facets", params={"institution": "Nowhere Institute"}).json()
    assert facets["projects"] == [] and facets["countries"] == []
