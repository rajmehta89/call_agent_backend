"""All stored times are UTC and reach the browser marked as UTC ("Z")."""

from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

import main  # noqa: F401  registers the datetime encoder
from timeutil import api_time, local_midnight_utc, utc_iso, utc_now


def test_utc_now_is_naive_utc():
    now = utc_now()
    assert now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=2)


def test_strings_and_api_values_say_utc():
    moment = datetime(2026, 10, 20, 4, 25, 35)
    assert utc_iso(moment) == "2026-10-20T04:25:35Z"
    assert api_time(moment) == "2026-10-20T04:25:35Z"
    assert api_time("already a string") == "already a string"


def test_every_api_response_marks_datetimes_as_utc():
    assert jsonable_encoder({"call_date": datetime(2026, 10, 17, 17, 55, 38)}) == {"call_date": "2026-10-17T17:55:38Z"}


def test_local_midnight_is_a_utc_moment_within_the_last_day():
    midnight = local_midnight_utc()
    assert timedelta(0) <= utc_now() - midnight <= timedelta(days=1, hours=1)
