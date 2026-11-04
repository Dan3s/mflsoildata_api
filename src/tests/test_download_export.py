"""What the data download puts in the file: columns, name and the filters it records."""
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from openpyxl import load_workbook

from routes import download


def _rec(**overrides):
    base = {field: "" for field in download.EXPORT_FIELDS}
    base.update(project_code="CLIMALOCA", site_code="S1", variable_code="SOIL_PH", value=6.1)
    base.update(overrides)
    return base


def test_columns_nobody_filled_in_are_left_out():
    fields = download._export_fields([_rec(), _rec(site_code="S2")])
    assert "qualifier" not in fields and "laboratory" not in fields
    assert "event_date" not in fields and "country" not in fields
    # the core columns never disappear, even when empty
    assert {"project_code", "site_code", "variable_code", "value", "unit"} <= set(fields)


def test_an_optional_column_with_any_value_stays():
    fields = download._export_fields([_rec(), _rec(country="Colombia", event_date="2019-03-01")])
    assert "country" in fields and "event_date" in fields
    assert fields == [f for f in download.EXPORT_FIELDS if f in fields]  # order preserved


def test_country_is_part_of_the_export():
    assert "country" in download.EXPORT_FIELDS and "admin_1" in download.EXPORT_FIELDS


def test_filename_names_the_project_matrix_and_day():
    name = download._export_filename([_rec(), _rec()], "SOIL", "xlsx")
    assert name.startswith("mfl_CLIMALOCA_SOIL_") and name.endswith(".xlsx")


def test_filename_for_several_projects_and_for_no_data():
    two = download._export_filename([_rec(), _rec(project_code="OTHER")], None, "csv")
    assert two.startswith("mfl_2-projects_") and two.endswith(".csv")
    assert download._export_filename([], None, "csv").startswith("mfl_no-data_")


def test_filename_is_safe_for_a_header():
    name = download._export_filename([_rec(project_code='A/B "x"')], None, "csv")
    assert '"' not in name and "/" not in name and " " not in name


def test_metadata_lists_only_the_filters_that_were_applied():
    rows = download._metadata_rows(3, {"institution": "CIAT", "country": None, "matrix": "SOIL"})
    keys = [k for k, _ in rows]
    assert "Filtro · Institución" in keys and "Filtro · Matriz" in keys
    assert "Filtro · País" not in keys


def test_xlsx_has_no_empty_columns_and_records_the_filters():
    content, _, ext = download._export_xlsx([_rec()], True, {"institution": "CIAT"})
    wb = load_workbook(io.BytesIO(content))
    header = [c.value for c in wb["Datos"][1]]
    assert ext == "xlsx" and "qualifier" not in header and "laboratory" not in header
    assert ["Filtro · Institución", "CIAT"] in [[c.value for c in row] for row in wb["Metadatos"].iter_rows()]


def test_csv_header_matches_the_kept_columns():
    content, _, _ = download._export_csv([_rec(country="Colombia")], False)
    header = content.decode("utf-8-sig").splitlines()[0].split(",")
    assert "country" in header and "qualifier" not in header
