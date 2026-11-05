"""Tests for the column-mapping additions to services.etl.parse_file.

These exercise parse_file directly (no DB needed at this layer) with synthetic
CSV bytes, covering: explicit column_mapping winning over auto-detection,
header_row offsetting for both the explicit-mapping and legacy columns_hint
paths, the new validation errors, and a regression pin for the two paths that
must stay byte-for-byte unchanged when the new parameters are omitted.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from schemas.upload import ColumnMappingEntry
from services import etl


def csv_bytes(rows):
    return ("\n".join(",".join(str(c) for c in row) for row in rows)).encode("utf-8")


def test_explicit_mapping_wins_over_id_row_lookalike():
    # The header row itself contains a token that looks like a known technical
    # id ("site_code") — auto-detection would normally seize on this, but an
    # explicit column_mapping must bypass _detect_id_row entirely.
    content = csv_bytes(
        [
            ["site_code", "My Depth", "My Value"],
            ["S-001", "10", "5.2"],
        ]
    )
    mapping = [
        ColumnMappingEntry(source_index=0, target_id="sample_code"),
        ColumnMappingEntry(source_index=1, target_id="attr:DEPTH_FROM_CM"),
        ColumnMappingEntry(source_index=2, target_id=None),  # ignored
    ]
    parsed = etl.parse_file(content, "file.csv", column_mapping=mapping)
    assert parsed.column_ids == ["sample_code", "attr:DEPTH_FROM_CM"]
    assert parsed.rows == [{"_row": 2, "sample_code": "S-001", "attr:DEPTH_FROM_CM": "10"}]


def test_explicit_mapping_with_header_row_offset():
    content = csv_bytes(
        [
            ["Título del archivo"],
            ["", ""],
            ["Codigo", "Valor"],
            ["S-001", "5.2"],
        ]
    )
    mapping = [
        ColumnMappingEntry(source_index=0, target_id="sample_code"),
        ColumnMappingEntry(source_index=1, target_id="attr:DEPTH_FROM_CM"),
    ]
    parsed = etl.parse_file(content, "file.csv", column_mapping=mapping, header_row=3)
    assert parsed.rows == [{"_row": 4, "sample_code": "S-001", "attr:DEPTH_FROM_CM": "5.2"}]


def test_columns_hint_with_header_row_offset():
    content = csv_bytes(
        [
            ["Título del archivo"],
            ["Codigo", "Valor"],
            ["S-001", "5.2"],
        ]
    )
    parsed = etl.parse_file(
        content, "file.csv", columns_hint=["sample_code", "attr:DEPTH_FROM_CM"], header_row=2
    )
    assert parsed.rows == [{"_row": 3, "sample_code": "S-001", "attr:DEPTH_FROM_CM": "5.2"}]


def test_columns_hint_without_header_row_is_unchanged():
    # Regression pin: omitting header_row must behave exactly as before this
    # change (single assumed header row, blank rows stripped up front).
    content = csv_bytes(
        [
            ["", ""],
            ["Codigo", "Valor"],
            ["S-001", "5.2"],
        ]
    )
    parsed = etl.parse_file(content, "file.csv", columns_hint=["sample_code", "attr:DEPTH_FROM_CM"])
    assert parsed.rows == [{"_row": 2, "sample_code": "S-001", "attr:DEPTH_FROM_CM": "5.2"}]


def test_official_template_id_row_path_is_unchanged():
    # Regression pin for the auto-detected hidden id-row path (branch 2):
    # columns_hint/header_row must have zero effect when a real id row exists.
    content = csv_bytes(
        [
            ["Sample Code", "Depth From"],
            ["sample_code", "attr:DEPTH_FROM_CM"],
            ["S-001", "5.2"],
        ]
    )
    parsed = etl.parse_file(content, "file.csv", columns_hint=["ignored_hint"], header_row=99)
    assert parsed.rows == [{"_row": 3, "sample_code": "S-001", "attr:DEPTH_FROM_CM": "5.2"}]


def test_mapping_source_index_out_of_range_raises():
    content = csv_bytes([["Codigo", "Valor"], ["S-001", "5.2"]])
    mapping = [ColumnMappingEntry(source_index=5, target_id="sample_code")]
    with pytest.raises(ValueError, match="only has 2 column"):
        etl.parse_file(content, "file.csv", column_mapping=mapping)


def test_mapping_duplicate_target_raises():
    content = csv_bytes([["A", "B"], ["1", "2"]])
    mapping = [
        ColumnMappingEntry(source_index=0, target_id="sample_code"),
        ColumnMappingEntry(source_index=1, target_id="sample_code"),
    ]
    with pytest.raises(ValueError, match="mapped from two different source columns"):
        etl.parse_file(content, "file.csv", column_mapping=mapping)


def test_mapping_all_targets_empty_raises():
    content = csv_bytes([["A", "B"], ["1", "2"]])
    mapping = [
        ColumnMappingEntry(source_index=0, target_id=None),
        ColumnMappingEntry(source_index=1, target_id=""),
    ]
    with pytest.raises(ValueError, match="at least one column"):
        etl.parse_file(content, "file.csv", column_mapping=mapping)


def test_header_row_out_of_range_raises():
    content = csv_bytes([["A", "B"], ["1", "2"]])
    mapping = [ColumnMappingEntry(source_index=0, target_id="sample_code")]
    with pytest.raises(ValueError, match="out of range"):
        etl.parse_file(content, "file.csv", column_mapping=mapping, header_row=10)
