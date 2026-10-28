"""Call history must stay fast: the list leaves out per-call transcripts and timings, and the stats are one query."""

from datetime import datetime

import routers.calls_api as calls_api


class Cursor(list):
    def sort(self, *args):
        return self

    def skip(self, count):
        return self

    def limit(self, count):
        return self


class Calls:
    def __init__(self, docs=None, totals=None):
        self.docs, self.totals, self.projections, self.pipelines = docs or [], totals, [], []

    def find(self, query, projection=None):
        self.projections.append(projection)
        return Cursor(dict(doc) for doc in self.docs)

    def count_documents(self, query):
        return len(self.docs)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return iter([self.totals] if self.totals else [])


class Mongo:
    def __init__(self, calls):
        self.calls, self.leads = calls, Calls()

    def is_connected(self):
        return True


def test_list_view_leaves_out_transcripts_and_timings(monkeypatch):
    calls = Calls([{"_id": "a1", "call_date": datetime(2026, 10, 27), "status": "completed"}])
    monkeypatch.setattr(calls_api, "mongo_client", Mongo(calls))
    result = calls_api.get_calls({"direction": "inbound"}, 200, slim=True)
    assert result["success"] and result["data"][0]["_id"] == "a1"
    assert calls.projections == [calls_api.LIST_EXCLUDED_FIELDS]
    for field in ("transcript_with_tool_calls", "messages", "voice_turn_timings", "ai_responses", "transcription"):
        assert calls.projections[0][field] == 0
    assert "transcription" not in result["data"][0]


def test_full_view_still_returns_everything(monkeypatch):
    # The lead page reads transcripts from this list, so the default stays the full record.
    calls = Calls([{"_id": "a1", "call_date": datetime(2026, 10, 27)}])
    monkeypatch.setattr(calls_api, "mongo_client", Mongo(calls))
    result = calls_api.get_calls({}, 50)
    assert calls.projections == [None]
    assert result["data"][0]["transcription"] == [] and result["data"][0]["ai_responses"] == []


def test_stats_come_from_one_query(monkeypatch):
    totals = {"total": 9, "inbound": 7, "outbound": 2, "completed": 6, "failed": 1, "missed": 2, "today": 3, "week": 8,
              "transfers": 1, "interested": 4, "not_interested": 1, "neutral": 2, "with_analysis": 7,
              "total_duration": 600, "avg_duration": 66.666}
    calls = Calls(totals=totals)
    monkeypatch.setattr(calls_api, "mongo_client", Mongo(calls))
    monkeypatch.setattr(calls_api, "call_scope", lambda user: {})
    data = calls_api.get_call_stats({"_id": "u1", "role": "owner"})["data"]
    assert len(calls.pipelines) == 1
    assert data["total_calls"] == 9 and data["inbound_calls"] == 7 and data["missed_calls"] == 2
    assert data["calls_today"] == 3 and data["calls_this_week"] == 8 and data["human_transfers"] == 1
    assert data["status_counts"] == {"completed": 6, "failed": 1, "missed": 2}
    assert data["interest_counts"] == {"interested": 4, "not_interested": 1, "neutral": 2}
    assert data["average_duration"] == 66.67 and data["total_duration"] == 600


def test_stats_with_no_calls_are_zero(monkeypatch):
    monkeypatch.setattr(calls_api, "mongo_client", Mongo(Calls()))
    monkeypatch.setattr(calls_api, "call_scope", lambda user: {})
    data = calls_api.get_call_stats({"_id": "u1", "role": "owner"})["data"]
    assert data["total_calls"] == 0 and data["average_duration"] == 0 and data["status_counts"]["missed"] == 0
