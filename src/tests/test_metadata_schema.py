"""Validation rules of the dataset ficha (schemas.metadata).

The upload form sends these values as codes; the API is what guarantees nothing
else is stored, so the enumerated fields are checked here without a database.
"""
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from schemas.metadata import ProjectMetadataUpsert


def test_sampling_type_can_hold_both_values():
    meta = ProjectMetadataUpsert(sampling_type=["FARM_COMPOSITE", "SINGLE_POINT"])
    assert meta.sampling_type == ["FARM_COMPOSITE", "SINGLE_POINT"]


def test_sampling_type_takes_a_single_value_as_a_list_of_one_and_drops_repeats():
    assert ProjectMetadataUpsert(sampling_type="SINGLE_POINT").sampling_type == ["SINGLE_POINT"]
    assert ProjectMetadataUpsert(sampling_type=["SINGLE_POINT", "SINGLE_POINT"]).sampling_type == ["SINGLE_POINT"]


@pytest.mark.parametrize("value", [None, [], ""])
def test_sampling_type_empty_means_nothing_was_chosen(value):
    # "" is what a cleared single value looks like; none of them is a choice.
    if value == "":
        with pytest.raises(ValidationError):
            ProjectMetadataUpsert(sampling_type=value)
    else:
        assert ProjectMetadataUpsert(sampling_type=value).sampling_type is None


@pytest.mark.parametrize("value", ["composite", ["farm"], ["SINGLE_POINT", "RASTER"]])
def test_sampling_type_rejects_anything_else(value):
    with pytest.raises(ValidationError):
        ProjectMetadataUpsert(sampling_type=value)


def test_raster_is_no_longer_a_geometry_type():
    with pytest.raises(ValidationError):
        ProjectMetadataUpsert(spatial_data_type="RASTER")


@pytest.mark.parametrize("value", ["POINT", "LINE", "POLYGON"])
def test_the_other_geometry_types_still_work(value):
    assert ProjectMetadataUpsert(spatial_data_type=value).spatial_data_type == value


def test_research_group_is_free_text_and_only_set_fields_are_dumped():
    meta = ProjectMetadataUpsert(research_group="Soil Health and Land Restoration")
    # The upload endpoint stores model_dump(exclude_unset=True): nothing else leaks in.
    assert meta.model_dump(exclude_unset=True) == {"research_group": "Soil Health and Land Restoration"}
