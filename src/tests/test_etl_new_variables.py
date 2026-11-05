"""Variables an uploader defines in the template's "New variables" sheet.

Covers the whole path that does not need a database (template generation, parsing
the sheet and linking hand-added columns to their definitions, catalog checks
against fake catalog rows) plus an end-to-end import that runs against the real
dev database inside a transaction it always rolls back.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from openpyxl import load_workbook

from mflsoildata_orm.models import Unit, Variable
from services import etl


def _structural_specs():
    return [
        etl.ColumnSpec(
            id=c.id, kind="structural", label=c.label, value_type=c.value_type,
            required=c.required, code=c.id, unit=c.unit, db_target=c.db_target,
        )
        for c in etl.STRUCTURAL_COLUMNS
    ]


def _template_bytes(lang="en", units=("mg/kg", "%"), domains=("LAB_RESULT",)):
    content, _, _ = etl.build_template(_structural_specs(), "xlsx", False, lang, list(units), list(domains))
    return content


def _filled_template(new_columns, defs, lang="en", sample="M-001", extra_headers=()):
    """Our template, as a team would edit it: columns added at the right (header
    in row 1, hidden id row left blank), definitions in the New variables sheet."""
    wb = load_workbook(io.BytesIO(_template_bytes(lang)))
    ws = wb["Template"]
    base = {
        "project_code": "TST-NV", "project_name": "New variables test", "site_code": "S-1",
        "latitude": "4.6", "longitude": "-74.0", "event_date": "2024-04-15",
        "sample_code": sample, "sample_type_code": "SOIL",
    }
    ids = [c.value for c in ws[2]]
    width = len(ids)
    data_row = [base.get(i) for i in ids]
    for offset, (header, value) in enumerate(new_columns, start=1):
        ws.cell(row=1, column=width + offset, value=header)
        data_row.append(value)
    for extra_offset, header in enumerate(extra_headers, start=len(new_columns) + 1):
        ws.cell(row=1, column=width + extra_offset, value=header)
        data_row.append("x")
    for col, value in enumerate(data_row, start=1):
        ws.cell(row=3, column=col, value=value)

    nv = wb[etl.NEW_VARIABLES_SHEET[lang]]
    header_row = len(etl.NEWVAR_INSTRUCTIONS[lang]) + 2
    for i, d in enumerate(defs, start=1):
        for col, value in enumerate(d, start=1):
            nv.cell(row=header_row + i, column=col, value=value)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --- template ----------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "es"])
def test_template_has_new_variables_sheet_and_hidden_lists(lang):
    wb = load_workbook(io.BytesIO(_template_bytes(lang)))
    assert etl.NEW_VARIABLES_SHEET[lang] in wb.sheetnames
    assert wb[etl.LISTS_SHEET].sheet_state == "hidden"
    nv = wb[etl.NEW_VARIABLES_SHEET[lang]]
    header_row = len(etl.NEWVAR_INSTRUCTIONS[lang]) + 2
    assert [c.value for c in nv[header_row]][:6] == etl.NEWVAR_HEADERS[lang]
    assert len(nv.data_validations.dataValidation) == 4  # type, matrix, unit, domain


def test_template_without_units_skips_unit_dropdown():
    content, _, _ = etl.build_template(_structural_specs(), "xlsx", False, "en", [])
    wb = load_workbook(io.BytesIO(content))
    assert len(wb["New variables"].data_validations.dataValidation) == 2  # type, matrix


def test_extra_sheets_are_never_read_as_data():
    # The data sheet is chosen by name, but a file whose Template sheet was renamed
    # must not fall back to one of the helper sheets.
    wb = load_workbook(io.BytesIO(_template_bytes()))
    wb["Template"].title = "Datos"
    out = io.BytesIO()
    wb.save(out)
    rows = etl._read_xlsx(out.getvalue())
    assert rows[0][0] == "Project Code"


# --- parsing -----------------------------------------------------------------------

def test_hand_added_column_is_linked_to_its_definition():
    content = _filled_template(
        [("Boro disponible", "1.5")],
        [("Boro disponible", "Number", "Soil (SOIL)", "mg/kg", "lab result", "Boron, Mehlich-3")],
    )
    parsed = etl.parse_file(content, "t.xlsx")
    assert "newvar:SOIL_BORO_DISPONIBLE" in parsed.column_ids
    assert parsed.rows[0]["newvar:SOIL_BORO_DISPONIBLE"] == "1.5"
    [d] = parsed.new_variable_defs
    assert (d["code"], d["value_type"], d["matrix"], d["unit"]) == ("SOIL_BORO_DISPONIBLE", "numeric", "SOIL", "mg/kg")
    assert d["domain"] == "LAB_RESULT"  # typed loosely, stored like the catalogue's own domains
    assert parsed.new_variable_issues == [] and parsed.ignored_columns == []


def test_spanish_labels_and_matrix_aliases_parse():
    content = _filled_template(
        [("Humedad", "12")],
        [("Humedad", "Número", "Suelo (SOIL)", "%", "Dominio propio", None)],
        lang="es",
    )
    [d] = etl.parse_file(content, "t.xlsx").new_variable_defs
    assert (d["value_type"], d["matrix"], d["domain"]) == ("numeric", "SOIL", "DOMINIO_PROPIO")


def test_blank_type_defaults_to_numeric_and_blank_matrix_to_none():
    content = _filled_template([("Algo nuevo", "3")], [("Algo nuevo", None, None, None, None, None)])
    [d] = etl.parse_file(content, "t.xlsx").new_variable_defs
    assert d["value_type"] == "numeric" and d["matrix"] is None and d["code"] == "ALGO_NUEVO"
    assert d["domain"] == "CUSTOM"  # unsure? leaving it empty is fine


def test_column_without_definition_is_reported_not_silently_dropped():
    content = _filled_template([], [], extra_headers=["Comentarios"])
    parsed = etl.parse_file(content, "t.xlsx")
    assert parsed.ignored_columns == ["Comentarios"]
    assert not any(c.startswith("newvar:") for c in parsed.column_ids)


def test_issue_rows_say_which_sheet_they_belong_to():
    content = _filled_template([("A", "1")], [("A", "Banana", None, None, None, None)])
    [issue] = etl.parse_file(content, "t.xlsx").new_variable_issues
    header_row = len(etl.NEWVAR_INSTRUCTIONS["en"]) + 2
    assert (issue["sheet"], issue["row"]) == ("New variables", header_row + 1)


def test_column_level_warnings_have_no_row_sheet_or_repeated_value():
    parsed = etl.ParsedFile(column_ids=[], rows=[], ignored_columns=["Notas"])
    _, issues = etl.process_new_variables(FakeDB(), parsed)
    [issue] = issues
    assert (issue["row"], issue["sheet"], issue["value"]) == (0, None, None)


def test_sheet_warnings_point_at_the_new_variables_sheet():
    d = {**_def("Huerfana"), "source_sheet": "Variables nuevas"}
    parsed = _parsed_with([d], column_ids=[], rows=[{"_row": 3}])
    _, issues = etl.process_new_variables(FakeDB(), parsed)
    [issue] = issues
    assert (issue["code"], issue["sheet"], issue["row"], issue["value"]) == ("NEW_VARIABLE_UNUSED", "Variables nuevas", 7, None)


def test_invalid_type_and_matrix_are_errors_and_leave_the_definition_out():
    content = _filled_template(
        [("A", "1"), ("B", "2")],
        [("A", "Banana", None, None, None, None), ("B", "Number", "Cielo", None, None, None)],
    )
    parsed = etl.parse_file(content, "t.xlsx")
    codes = {i["code"] for i in parsed.new_variable_issues}
    assert codes == {"NEW_VARIABLE_INVALID_TYPE", "NEW_VARIABLE_INVALID_MATRIX"}
    assert all(i["severity"] == "error" for i in parsed.new_variable_issues)
    assert parsed.new_variable_defs == []


def test_duplicate_definition_is_an_error():
    content = _filled_template(
        [("Boro", "1")],
        [("Boro", "Number", None, None, None, None), ("boro", "Number", None, None, None, None)],
    )
    parsed = etl.parse_file(content, "t.xlsx")
    assert [i["code"] for i in parsed.new_variable_issues] == ["NEW_VARIABLE_DUPLICATE"]
    assert len(parsed.new_variable_defs) == 1


def test_csv_carries_no_new_variables():
    csv = b"project_code,project_name,site_code,latitude,longitude,event_date,sample_code,sample_type_code\nP,N,S,1,2,2024-01-01,M,SOIL\n"
    parsed = etl.parse_file(csv, "t.csv")
    assert parsed.new_variable_defs == [] and parsed.ignored_columns == []


def test_official_template_without_additions_is_unchanged():
    content = _filled_template([], [])
    parsed = etl.parse_file(content, "t.xlsx")
    assert parsed.column_ids == [c.id for c in etl.STRUCTURAL_COLUMNS]
    assert parsed.new_variable_defs == [] and parsed.ignored_columns == []


# --- catalog checks (fake catalog) ---------------------------------------------------

class _Query:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeDB:
    def __init__(self, variables=(), units=()):
        self._by_model = {Variable: list(variables), Unit: list(units)}

    def query(self, model):
        return _Query(self._by_model[model])


def _variable(id_, code, name, matrix="SOIL", value_type="numeric"):
    return Variable(id=id_, variable_code=code, name=name, matrix=matrix, value_type=value_type)


def _parsed_with(defs, column_ids=None, rows=None):
    column_ids = column_ids if column_ids is not None else [f"newvar:{d['code']}" for d in defs]
    rows = rows if rows is not None else [{"_row": 3, **{c: "1" for c in column_ids}}]
    return etl.ParsedFile(column_ids=column_ids, rows=rows, new_variable_defs=defs)


def _def(name, matrix="SOIL", unit=None, value_type="numeric"):
    return {
        "code": etl.derive_variable_code(name, matrix), "name": name, "value_type": value_type,
        "matrix": matrix, "unit": unit, "domain": "CUSTOM", "description": None, "source_row": 7,
    }


def test_existing_code_links_the_column_to_the_catalog_variable():
    db = FakeDB(variables=[_variable(12, "SOIL_BORO", "Boro")])
    parsed = _parsed_with([_def("Boro")])
    staged, issues = etl.process_new_variables(db, parsed)
    assert staged == []
    assert parsed.column_ids == ["var:12"]
    assert parsed.rows[0] == {"_row": 3, "var:12": "1"}
    assert [i["code"] for i in issues] == ["NEW_VARIABLE_LINKED"]


def test_similar_name_is_flagged_for_the_reviewer():
    db = FakeDB(variables=[_variable(5, "SOIL_AL_DISP_MG_KG", "Al Disp (mg/kg)")])
    staged, issues = etl.process_new_variables(db, _parsed_with([_def("Al disp")]))
    assert staged[0]["similar_to"] == {"id": 5, "name": "Al Disp (mg/kg)", "matrix": "SOIL"}
    assert [i["code"] for i in issues] == ["NEW_VARIABLE_SIMILAR"]


def test_similar_name_in_another_matrix_is_not_a_match():
    db = FakeDB(variables=[_variable(5, "LEAF_BORO", "Boro", matrix="LEAF")])
    staged, issues = etl.process_new_variables(db, _parsed_with([_def("Boro", matrix="SOIL")]))
    assert "similar_to" not in staged[0] and issues == []


def test_unit_is_resolved_case_insensitively_or_warned():
    db = FakeDB(units=[Unit(id=3, unit_code="MG_KG", symbol="mg/kg")])
    staged, issues = etl.process_new_variables(db, _parsed_with([_def("Uno", unit="MG/KG"), _def("Dos", unit="furlongs")]))
    by_name = {d["name"]: d for d in staged}
    assert (by_name["Uno"]["unit_id"], by_name["Uno"]["unit"]) == (3, "mg/kg")
    assert by_name["Dos"]["unit_id"] is None
    assert [i["code"] for i in issues] == ["NEW_VARIABLE_UNIT_UNKNOWN"]


def test_definition_without_a_column_is_dropped_with_a_warning():
    parsed = _parsed_with([_def("Huerfana")], column_ids=[], rows=[{"_row": 3}])
    staged, issues = etl.process_new_variables(FakeDB(), parsed)
    assert staged == [] and [i["code"] for i in issues] == ["NEW_VARIABLE_UNUSED"]


def test_undefined_columns_become_warnings():
    parsed = etl.ParsedFile(column_ids=[], rows=[], ignored_columns=["Notas"])
    _, issues = etl.process_new_variables(FakeDB(), parsed)
    assert [(i["code"], i["severity"]) for i in issues] == [("UNDEFINED_COLUMN", "warning")]


def test_new_variable_cells_are_validated_with_the_declared_type():
    d = _def("Boro")
    specs, _ = etl.resolve_columns(FakeDB(), [f"newvar:{d['code']}"], new_variables=[d])
    assert specs[0].kind == "var" and specs[0].ref_id is None and specs[0].new_def is d
    parsed = _parsed_with([d], rows=[{"_row": 3, f"newvar:{d['code']}": "abc"}])
    issues, summary, _ = etl.validate(parsed, specs)
    assert any(i["code"] == "NOT_NUMERIC" for i in issues)
    assert summary["measurements"] == 1


def test_unknown_newvar_column_is_an_error():
    _, issues = etl.resolve_columns(FakeDB(), ["newvar:NOPE"], new_variables=[])
    assert issues[0]["code"] == "UNKNOWN_VARIABLE" and issues[0]["severity"] == "error"


# --- end to end against the dev database (always rolled back) -------------------------

@pytest.fixture
def db():
    from mflsoildata_orm.database import SessionLocal

    session = SessionLocal()
    try:
        session.execute(__import__("sqlalchemy").text("select 1"))
    except Exception:
        session.close()
        pytest.skip("database not reachable")
    yield session
    session.rollback()
    session.close()


def _stored(defs, rows=None):
    d = defs[0]
    col = f"newvar:{d['code']}"
    base = {
        "project_code": "TST-NV-ROLLBACK", "project_name": "Rolled back", "site_code": "S-1",
        "latitude": "4.6", "longitude": "-74.0", "event_date": "2024-04-15",
        "sample_code": "M-1", "sample_type_code": "SOIL",
    }
    row = {"_row": 3, **base, col: "1.5"}
    return etl.StoredUpload(
        upload_id="test", filename="t.xlsx", column_ids=[*base.keys(), col], rows=rows or [row],
        issues=[], summary={}, error_count=0, warning_count=0, new_variables=defs,
    )


def _nv_def():
    return {**_def("Zz Test Analyte", "SOIL"), "unit_id": None}


def test_import_creates_the_variable_and_its_measurement(db):
    from mflsoildata_orm.models import Measurement, Variable

    result = etl.import_upload(db, _stored([_nv_def()]))
    assert result["inserted"]["new_variables"] == 1 and result["inserted"]["measurements"] == 1
    variable = db.query(Variable).filter(Variable.variable_code == "SOIL_ZZ_TEST_ANALYTE").one()
    assert (variable.name, variable.matrix, variable.value_type) == ("Zz Test Analyte", "SOIL", "numeric")
    assert variable.notes["source"] == "upload"
    assert db.query(Measurement).filter(Measurement.variable_id == variable.id).count() == 1


def test_domain_defaults_to_custom_and_a_reviewer_can_set_it(db):
    from mflsoildata_orm.models import Variable

    d = _nv_def()
    etl.import_upload(db, _stored([d]))
    assert db.query(Variable).filter(Variable.variable_code == d["code"]).one().domain == "CUSTOM"

    d2 = {**_def("Zz Other Analyte", "SOIL"), "unit_id": None}
    etl.import_upload(db, _stored([d2]), {d2["code"]: {"action": "create", "domain": "lab result"}})
    assert db.query(Variable).filter(Variable.variable_code == d2["code"]).one().domain == "LAB_RESULT"


def test_reviewer_can_edit_before_creating(db):
    from mflsoildata_orm.models import Variable

    d = _nv_def()
    etl.import_upload(db, _stored([d]), {d["code"]: {"action": "create", "name": "Zz Renamed", "matrix": "LEAF"}})
    variable = db.query(Variable).filter(Variable.variable_code == "LEAF_ZZ_RENAMED").one()
    assert (variable.name, variable.matrix) == ("Zz Renamed", "LEAF")


def test_reviewer_can_merge_into_an_existing_variable(db):
    from mflsoildata_orm.models import Measurement, Variable

    target = db.query(Variable).filter(Variable.value_type == "numeric").first()
    if target is None:
        pytest.skip("no numeric variable in the dev database")
    d = _nv_def()
    result = etl.import_upload(db, _stored([d]), {d["code"]: {"action": "merge", "variable_id": target.id}})
    assert result["inserted"]["new_variables"] == 0 and result["inserted"]["measurements"] == 1
    assert db.query(Variable).filter(Variable.variable_code == d["code"]).count() == 0
    assert db.query(Measurement).filter(Measurement.variable_id == target.id).count() >= 1


def test_merge_into_a_different_value_type_is_refused(db):
    from mflsoildata_orm.models import Variable

    target = db.query(Variable).filter(Variable.value_type != "numeric").first()
    if target is None:
        pytest.skip("no non-numeric variable in the dev database")
    d = _nv_def()
    with pytest.raises(etl.VariableResolutionError):
        etl.import_upload(db, _stored([d]), {d["code"]: {"action": "merge", "variable_id": target.id}})


def test_merge_into_a_missing_variable_is_refused(db):
    d = _nv_def()
    with pytest.raises(etl.VariableResolutionError):
        etl.import_upload(db, _stored([d]), {d["code"]: {"action": "merge", "variable_id": 999999999}})


def test_invalid_matrix_override_is_refused(db):
    d = _nv_def()
    with pytest.raises(etl.VariableResolutionError):
        etl.import_upload(db, _stored([d]), {d["code"]: {"action": "create", "matrix": "CIELO"}})
