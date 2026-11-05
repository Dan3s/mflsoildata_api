"""Tests for services.dataverse's field-extraction logic.

_extract_ficha/_extract_files are pure functions over Dataverse's JSON field
shape, so they're tested directly with synthetic payloads (no network) —
including the two real Harvard/demo.dataverse.org datasets used to validate
this during development, plus the less common compound fields (grantNumber,
relatedDatasets, timePeriodCovered, geospatial bounding box) that weren't
present in either live example.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import dataverse


def primitive(type_name, value, multiple=False):
    return {"typeName": type_name, "multiple": multiple, "typeClass": "primitive", "value": value}


def controlled(type_name, value, multiple=True):
    return {"typeName": type_name, "multiple": multiple, "typeClass": "controlledVocabulary", "value": value}


def compound(type_name, entries):
    return {
        "typeName": type_name,
        "multiple": True,
        "typeClass": "compound",
        "value": [
            {k: primitive(k, v) for k, v in entry.items()}
            for entry in entries
        ],
    }


def test_extract_ficha_real_shape_demo_dataverse():
    # Trimmed, real shape captured from https://demo.dataverse.org (public
    # sandbox) during development — see the plan doc for how this was verified.
    citation_fields = [
        primitive("title", "Reproductive Health Laws Around the World"),
        compound("author", [{"authorName": "Finlay, Jocelyn", "authorAffiliation": "Harvard University"}]),
        compound(
            "datasetContact",
            [{"datasetContactName": "Finlay, Jocelyn", "datasetContactEmail": "jfinlay@hsph.harvard.edu"}],
        ),
        compound("dsDescription", [{"dsDescriptionValue": "A numeric representation..."}]),
        controlled("subject", ["Social Sciences"]),
        compound("keyword", [{"keywordValue": "reproductive health"}, {"keywordValue": "contraception"}]),
    ]
    ficha = dataverse._extract_ficha(citation_fields, None)
    assert ficha["title"] == "Reproductive Health Laws Around the World"
    assert ficha["authors"] == ["Finlay, Jocelyn (Harvard University)"]
    assert ficha["contact_name"] == "Finlay, Jocelyn"
    assert ficha["contact_email"] == "jfinlay@hsph.harvard.edu"
    assert ficha["abstract"] == "A numeric representation..."
    assert ficha["subject"] == "Social Sciences"
    assert ficha["keywords"] == ["reproductive health", "contraception"]


def test_extract_ficha_second_contact_becomes_backup_contact():
    citation_fields = [
        compound(
            "datasetContact",
            [
                {"datasetContactName": "Finlay, Jocelyn", "datasetContactEmail": "jfinlay@hsph.harvard.edu"},
                {"datasetContactName": "Doe, Jane", "datasetContactEmail": "jane.doe@example.org"},
            ],
        ),
    ]
    ficha = dataverse._extract_ficha(citation_fields, None)
    assert ficha["contact_name"] == "Finlay, Jocelyn"
    assert ficha["contact_email"] == "jfinlay@hsph.harvard.edu"
    assert ficha["contact_name_2"] == "Doe, Jane"
    assert ficha["contact_email_2"] == "jane.doe@example.org"


def test_extract_ficha_single_contact_has_no_backup():
    citation_fields = [
        compound(
            "datasetContact",
            [{"datasetContactName": "Finlay, Jocelyn", "datasetContactEmail": "jfinlay@hsph.harvard.edu"}],
        ),
    ]
    ficha = dataverse._extract_ficha(citation_fields, None)
    assert ficha["contact_name_2"] is None
    assert ficha["contact_email_2"] is None


def test_extract_ficha_grant_related_datasets_and_time_period():
    citation_fields = [
        compound(
            "grantNumber",
            [{"grantNumberAgency": "Bill & Melinda Gates Foundation", "grantNumberValue": "INV-12345"}],
        ),
        controlled("relatedDatasets", ["doi:10.5072/FK2/OTHER1", "doi:10.5072/FK2/OTHER2"]),
        compound(
            "timePeriodCovered",
            [{"timePeriodCoveredStart": "2020-01-01", "timePeriodCoveredEnd": "2021-12-31"}],
        ),
        controlled("dataSources", ["Field surveys 2020-2021"]),
    ]
    ficha = dataverse._extract_ficha(citation_fields, None)
    assert ficha["funding_information"] == "INV-12345 — Bill & Melinda Gates Foundation"
    assert ficha["related_datasets"] == ["doi:10.5072/FK2/OTHER1", "doi:10.5072/FK2/OTHER2"]
    assert ficha["temporal_coverage_start"] == "2020-01-01"
    assert ficha["temporal_coverage_end"] == "2021-12-31"
    assert ficha["data_source"] == "Field surveys 2020-2021"


def test_extract_ficha_geospatial_bounding_box():
    geospatial_fields = [
        compound(
            "geographicBoundingBox",
            [{"westLongitude": "-75.5", "eastLongitude": "-74.0", "northLatitude": "5.2", "southLatitude": "4.1"}],
        ),
    ]
    ficha = dataverse._extract_ficha([], geospatial_fields)
    assert ficha["geographic_bounding_box"] == "O: -75.5, E: -74.0, N: 5.2, S: 4.1"


def test_extract_ficha_missing_fields_are_none_not_errors():
    ficha = dataverse._extract_ficha([], None)
    assert ficha["title"] is None
    assert ficha["authors"] is None
    assert ficha["geographic_bounding_box"] is None


def test_extract_files():
    latest_version = {
        "files": [
            {
                "restricted": True,
                "dataFile": {"id": 21, "filename": "Chrysanthemum.jpg", "contentType": "image/jpeg", "filesize": 879394},
            },
            {
                "restricted": False,
                "dataFile": {
                    "id": 28,
                    "filename": "gss.tab",
                    "contentType": "text/tab-separated-values",
                    "filesize": 527005,
                    "originalFileName": "gss.dta",
                },
            },
            {"restricted": False, "dataFile": {}},  # no id -> dropped
            {
                "restricted": False,
                "categories": ["Documentation"],
                "dataFile": {"id": 35, "filename": "README.md", "contentType": "text/markdown", "filesize": 512},
            },
        ]
    }
    files = dataverse._extract_files(latest_version)
    assert len(files) == 3
    assert files[0] == {
        "id": 21,
        "filename": "Chrysanthemum.jpg",
        "content_type": "image/jpeg",
        "size": 879394,
        "restricted": True,
        "original_filename": None,
        "categories": None,
    }
    assert files[1]["original_filename"] == "gss.dta"
    assert files[2]["categories"] == ["Documentation"]
