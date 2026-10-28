"""The small English region-name table must match phonenumbers (rebuild it with scripts/build_region_names.py)."""

import json
import os

import phonenumbers
from phonenumbers import geocoder

import voice_turns

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "region_names_en.json")


def test_table_matches_the_library():
    with open(PATH, encoding="utf-8") as handle:
        names = json.load(handle)
    assert set(names) == set(phonenumbers.SUPPORTED_REGIONS)
    assert all(names[region] == (geocoder._region_display_name(region, "en") or region) for region in names)


def test_caller_country_comes_from_the_table():
    assert voice_turns._region_name("AU") == "Australia" and voice_turns._region_name("PK") == "Pakistan"
    assert voice_turns._region_name("ZZ") == "ZZ"
    assert voice_turns._country(phonenumbers.parse("+61412345678")) == "Australia"
    assert voice_turns._country(phonenumbers.parse("+919876543210")) == "India"
