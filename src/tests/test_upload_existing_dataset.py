"""Uploading data into a dataset that is already in the platform.

Same approach as the new-variables route tests: the real routes against the dev
database, through one session whose ``commit`` only flushes and which is rolled
back at the end. Skipped when the database is not reachable.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import text

from dependencies.auth import CurrentUser, get_current_user, require_database_editor, require_editor
from main import app
from mflsoildata_orm.database import SessionLocal
from mflsoildata_orm.models import DatasetSubmission, Project, ProjectMetadata
from routes import submissions as submissions_routes
from routes import upload as upload_routes
from services import etl

REQUIRED = ",".join(etl.REQUIRED_STRUCTURAL_IDS)
CODE = "TST_EXT_DS"


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
    monkeypatch.setattr(upload_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(submissions_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(upload_routes.keycloak_admin, "list_reviewer_emails", lambda: [])
    yield db
    db.rollback()
    real_close()


@pytest.fixture
def client(session):
    user = CurrentUser(sub="t", email=None, username="tester", roles=["admin"])
    for dep in (get_current_user, require_editor, require_database_editor):
        app.dependency_overrides[dep] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def dataset(session):
    project = Project(code=CODE, name="Existing dataset")
    session.add(project)
    session.flush()
    return project


def _file(client, project_codes):
    """A template with one row per entry of ``project_codes`` (None = left empty)."""
    resp = client.get("/upload/template", params={"columns": REQUIRED.split(","), "lang": "es"})
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb["Template"]
    ids = [c.value for c in ws[2]]
    for n, code in enumerate(project_codes, start=1):
        row = {
            "project_code": code, "project_name": "Name in the file", "site_code": f"S-{n}",
            "latitude": "4.6", "longitude": "-74.0", "event_date": "2024-04-15",
            "sample_code": f"M-{n}", "sample_type_code": "SOIL",
        }
        ws.append([row[i] for i in ids])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _validate(client, content, **form):
    return client.post(
        "/upload/validate",
        files={"file": ("t.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=form,
    )


def test_rows_that_leave_the_project_empty_are_filled_with_the_chosen_dataset(client, dataset):
    body = _validate(client, _file(client, [None, CODE]), project_code=CODE).json()
    assert body["error_count"] == 0, body["issues"]
    assert body["total_rows"] == 2


def test_a_row_naming_another_project_is_an_error(client, dataset):
    body = _validate(client, _file(client, [CODE, "SOMETHING_ELSE", "SOMETHING_ELSE"]), project_code=CODE).json()
    mismatches = [i for i in body["issues"] if i["code"] == "PROJECT_MISMATCH"]
    assert len(mismatches) == 1  # one per wrong code, not one per row
    assert mismatches[0]["severity"] == "error" and "2 fila" in mismatches[0]["message"]
    assert body["error_count"] >= 1


def test_without_a_chosen_dataset_nothing_changes(client, dataset):
    body = _validate(client, _file(client, [CODE, "SOMETHING_ELSE"])).json()
    assert not [i for i in body["issues"] if i["code"] == "PROJECT_MISMATCH"]


def test_a_dataset_that_does_not_exist_is_refused(client):
    resp = _validate(client, _file(client, [None]), project_code="NOPE_NOT_HERE")
    assert resp.status_code == 422


def test_the_submission_is_tied_to_the_dataset_and_approval_leaves_its_access_alone(client, session, dataset):
    body = _validate(client, _file(client, [None]), project_code=CODE).json()
    assert body["error_count"] == 0, body["issues"]
    done = client.post("/upload/import", json={"upload_id": body["upload_id"], "project_code": CODE})
    assert done.status_code == 200, done.text

    sub = session.query(DatasetSubmission).filter(DatasetSubmission.project_code == CODE).one()
    assert sub.project_id == dataset.id and sub.status == "PENDING"

    approved = client.post(f"/submissions/{sub.id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["project_id"] == dataset.id
    # An uploader adding to a dataset that was already there is not put on its access list.
    assert session.query(ProjectMetadata).filter(ProjectMetadata.project_id == dataset.id).count() == 0
