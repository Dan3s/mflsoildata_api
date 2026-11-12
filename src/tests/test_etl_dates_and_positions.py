"""Regressions from the first real uploads (RUTAS_PDET and Córdoba).

* Approving a submission lost every sampling date: the staged rows went through
  JSON, which turns a date cell into "2024-03-01T00:00:00", and the date parser
  rejected that text without a word.
* A file with no technical-id row is read by position. Its 8th column ("País del
  Proyecto") was taken for the sample type and every column past the 8th was
  dropped, with nothing in the validation to say so.
* "<LQM" (limit of quantification of the method) is how Spanish-language labs
  write "<LOQ".
"""
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from services import etl

REQUIRED = list(etl.REQUIRED_STRUCTURAL_IDS)


@pytest.mark.parametrize(
    "text",
    ["2024-03-01T00:00:00", "2024-03-01 00:00:00", "2024-03-01T00:00:00.000", "2024-03-01T00:00:00Z", "2024-03-01T00:00"],
)
def test_a_date_that_went_through_json_is_still_a_date(text):
    assert etl.parse_date(text) == (date(2024, 3, 1), True)


def test_the_staged_row_round_trip_keeps_the_date():
    staged = json.loads(json.dumps({"event_date": datetime(2021, 11, 1)}, default=lambda o: o.isoformat()))
    assert etl.parse_date(staged["event_date"]) == (date(2021, 11, 1), True)


@pytest.mark.parametrize("text", ["Marzo - 2024", "2024-13-45T00:00:00", "tomorrow", "2024-03-01T25:00:00"])
def test_things_that_are_not_dates_are_still_refused(text):
    assert etl.parse_date(text) == (None, False)


@pytest.mark.parametrize("text", ["<LQM", "< LQM", "<LQ", "<LOQ", "BDL"])
def test_detection_limit_tokens_are_a_result_not_garbage(text):
    value, qualifier, ok = etl.parse_numeric(text)
    assert ok and value is None and qualifier


def _csv_file(header, row):
    return ("\n".join([",".join(header), ",".join(row)])).encode("utf-8")


# What the Córdoba / RUTAS_PDET files looked like: the platform's Spanish headers and no
# technical-id row, with the country where the 8th required column ought to be.
HEADER = [
    "Código de Proyecto", "Nombre del Proyecto", "Código del Sitio", "Latitud del Sitio", "Longitud del Sitio",
    "Fecha de Muestreo", "Código de Muestra", "País del Proyecto", "País del Sitio", "pH (Un) — SOIL",
]
ROW = ["P", "Proyecto", "S1", "8.2", "-76.0", "2024-03-01", "S1", "Colombia", "Colombia", "5.2"]


def test_columns_read_by_position_keep_the_files_headers():
    parsed = etl.parse_file(_csv_file(HEADER, ROW), "f.csv", columns_hint=REQUIRED)
    assert parsed.positional_headers == HEADER[: len(REQUIRED)]
    assert parsed.positional_extra == HEADER[len(REQUIRED):]


def test_a_country_read_as_the_sample_type_is_an_error_not_a_silent_assignment():
    parsed = etl.parse_file(_csv_file(HEADER, ROW), "f.csv", columns_hint=REQUIRED)
    issues = etl.positional_issues(parsed, [])
    wrong = [i for i in issues if i["code"] == "HEADER_MISMATCH"]
    assert [(i["column"], i["severity"], i["value"]) for i in wrong] == [("sample_type_code", "error", "País del Proyecto")]


def test_columns_past_the_selected_ones_are_reported_once_instead_of_dropped_in_silence():
    parsed = etl.parse_file(_csv_file(HEADER, ROW), "f.csv", columns_hint=REQUIRED)
    dropped = [i for i in etl.positional_issues(parsed, []) if i["code"] == "COLUMNS_IGNORED"]
    assert len(dropped) == 1 and dropped[0]["severity"] == "warning"
    assert "pH (Un) — SOIL" in dropped[0]["message"] and "País del Sitio" in dropped[0]["message"]


def test_a_file_laid_out_like_the_selection_raises_nothing():
    header = [etl.structural_label(c, "es") for c in REQUIRED]
    parsed = etl.parse_file(_csv_file(header, ["x"] * len(REQUIRED)), "f.csv", columns_hint=REQUIRED)
    assert etl.positional_issues(parsed, []) == []


def test_an_unrecognised_header_is_only_a_warning():
    header = [etl.structural_label(c, "es") for c in REQUIRED]
    header[2] = "Nombre raro"
    parsed = etl.parse_file(_csv_file(header, ["x"] * len(REQUIRED)), "f.csv", columns_hint=REQUIRED)
    issues = etl.positional_issues(parsed, [])
    assert [(i["column"], i["severity"]) for i in issues] == [("site_code", "warning")]


def test_explicit_mapping_has_no_positional_checks():
    mapping = [
        etl.ColumnMappingEntry(source_index=i, source_header=h, target_id=REQUIRED[i] if i < len(REQUIRED) else None)
        for i, h in enumerate(HEADER)
    ]
    parsed = etl.parse_file(_csv_file(HEADER, ROW), "f.csv", column_mapping=mapping)
    assert parsed.positional_headers is None and etl.positional_issues(parsed, []) == []
