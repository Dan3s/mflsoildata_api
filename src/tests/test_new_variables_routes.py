"""The upload -> review -> approve round trip for variables defined in the template.

Runs the real routes against the dev database, but through one session whose
``commit`` only flushes and which is rolled back at the end, so nothing is left
behind. Skipped when the database is not reachable.
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
from mflsoildata_orm.models import DatasetSubmission, Variable
from routes import submissions as submissions_routes
from routes import upload as upload_routes
from services import etl

REQUIRED = ",".join(etl.REQUIRED_STRUCTURAL_IDS)


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


def _filled_template(client, lang="es"):
    resp = client.get("/upload/template", params={"columns": REQUIRED.split(","), "lang": lang})
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.content))
    assert etl.NEW_VARIABLES_SHEET[lang] in wb.sheetnames
    ws = wb["Template"]
    ids = [c.value for c in ws[2]]
    row = {
        "project_code": "TST-NV-ROUTE", "project_name": "Route test", "site_code": "S-1",
        "latitude": "4.6", "longitude": "-74.0", "event_date": "2024-04-15",
        "sample_code": "M-1", "sample_type_code": "SOIL",
    }
    ws.append([row[i] for i in ids])
    col = len(ids) + 1
    ws.cell(row=1, column=col, value="Zz Route Analyte")
    ws.cell(row=ws.max_row, column=col, value="2.5")
    nv = wb[etl.NEW_VARIABLES_SHEET[lang]]
    header_row = len(etl.NEWVAR_INSTRUCTIONS[lang]) + 2
    for c, value in enumerate(["Zz Route Analyte", "Número", "Suelo (SOIL)", "mg/kg", "LAB_RESULT", "Ruta de prueba"], start=1):
        nv.cell(row=header_row + 1, column=c, value=value)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _validate(client, content):
    return client.post(
        "/upload/validate",
        files={"file": ("t.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def test_validate_reports_the_new_variable(client):
    resp = _validate(client, _filled_template(client))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error_count"] == 0
    assert body["summary"]["new_variables"] == 1 and body["summary"]["measurements"] == 1
    assert "Zz Route Analyte" in body["detected_variables"] and "SOIL" in body["detected_matrices"]


def test_unrecognised_column_is_a_warning_not_silence(client):
    content = _filled_template(client)
    wb = load_workbook(io.BytesIO(content))
    wb["Template"].cell(row=1, column=wb["Template"].max_column + 1, value="Comentarios")
    out = io.BytesIO()
    wb.save(out)
    body = _validate(client, out.getvalue()).json()
    undefined = next(i for i in body["issues"] if i["code"] == "UNDEFINED_COLUMN")
    assert (undefined["column"], undefined["row"], undefined["sheet"]) == ("Comentarios", 0, None)


def test_full_round_trip_upload_review_approve(client, session):
    body = _validate(client, _filled_template(client)).json()
    imported = client.post("/upload/import", json={"upload_id": body["upload_id"], "ignore_warnings": True})
    assert imported.status_code == 200, imported.text

    sub = session.query(DatasetSubmission).filter(DatasetSubmission.project_code == "TST_NV_ROUTE").one()
    assert [d["name"] for d in sub.new_variables] == ["Zz Route Analyte"]
    assert session.query(Variable).filter(Variable.name.ilike("Zz Route%")).count() == 0  # not yet in the catalogue

    listed = client.get("/submissions", params={"status": "PENDING"}).json()
    mine = next(s for s in listed if s["id"] == sub.id)
    assert mine["new_variables"][0]["code"] == "SOIL_ZZ_ROUTE_ANALYTE"

    approved = client.post(
        f"/submissions/{sub.id}/approve",
        json={"variable_resolutions": [{"code": "SOIL_ZZ_ROUTE_ANALYTE", "action": "create", "name": "Zz Route Edited"}]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["inserted"]["new_variables"] == 1
    created = session.query(Variable).filter(Variable.variable_code == "SOIL_ZZ_ROUTE_EDITED").one()
    assert created.name == "Zz Route Edited" and created.matrix == "SOIL" and created.domain == "LAB_RESULT"


def test_approve_defaults_to_creating_the_variable_as_described(client, session):
    body = _validate(client, _filled_template(client)).json()
    client.post("/upload/import", json={"upload_id": body["upload_id"], "ignore_warnings": True})
    sub = session.query(DatasetSubmission).filter(DatasetSubmission.project_code == "TST_NV_ROUTE").one()
    assert client.post(f"/submissions/{sub.id}/approve").status_code == 200
    assert session.query(Variable).filter(Variable.variable_code == "SOIL_ZZ_ROUTE_ANALYTE").count() == 1


def test_approve_rejects_a_decision_for_a_variable_the_submission_does_not_have(client, session):
    body = _validate(client, _filled_template(client)).json()
    client.post("/upload/import", json={"upload_id": body["upload_id"], "ignore_warnings": True})
    sub = session.query(DatasetSubmission).filter(DatasetSubmission.project_code == "TST_NV_ROUTE").one()
    resp = client.post(f"/submissions/{sub.id}/approve", json={"variable_resolutions": [{"code": "NOPE"}]})
    assert resp.status_code == 422
    assert "NOPE" in resp.json()["detail"]["message"]


def test_approve_surfaces_a_bad_merge_as_422(client, session):
    body = _validate(client, _filled_template(client)).json()
    client.post("/upload/import", json={"upload_id": body["upload_id"], "ignore_warnings": True})
    sub = session.query(DatasetSubmission).filter(DatasetSubmission.project_code == "TST_NV_ROUTE").one()
    resp = client.post(
        f"/submissions/{sub.id}/approve",
        json={"variable_resolutions": [{"code": "SOIL_ZZ_ROUTE_ANALYTE", "action": "merge", "variable_id": 999999999}]},
    )
    assert resp.status_code == 422
    assert "does not exist" in resp.json()["detail"]["message"]
