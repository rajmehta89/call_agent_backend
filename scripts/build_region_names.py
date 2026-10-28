"""Writes data/region_names_en.json: the English name of every region phonenumbers supports ("AU": "Australia").

The voice prompt names the caller's country. phonenumbers' own name table holds every language and costs ~95 MB
of memory once imported, more than a 512 MB Render server can spare during a call; this file holds the same English
names in a few KB. Run again after upgrading phonenumbers (tests/test_region_names.py checks they still match):

    python scripts/build_region_names.py
"""

import json
import os

import phonenumbers
from phonenumbers import geocoder

names = {region: geocoder._region_display_name(region, "en") or region for region in sorted(phonenumbers.SUPPORTED_REGIONS)}
path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "region_names_en.json")
with open(path, "w", encoding="utf-8") as handle:
    json.dump(names, handle, ensure_ascii=False, indent=0, sort_keys=True)
    handle.write("\n")
print(f"{len(names)} region names written to {path}")
