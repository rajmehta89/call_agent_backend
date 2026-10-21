"""Conversation status (the sidebar STATUS group): known stages only, and a default for every thread."""

from routers import inbox_api


def test_stages_are_unique_and_new_lead_is_the_default():
    ids = [stage for stage, _ in inbox_api.STAGES]
    assert len(ids) == len(set(ids)) == 9
    assert inbox_api.DEFAULT_STAGE == ids[0] == "new_lead"

