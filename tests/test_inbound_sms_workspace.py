"""An SMS reply goes to whoever sent the message it answers: that sender's workspace and lead."""

import asyncio
from unittest.mock import patch

from bson import ObjectId

import routers.leads_api_mongo as leads_api

PERSON, OURS = "+61412345678", "+61485047270"


class FakeRequest:
    def __init__(self, form):
        self._form = form

    async def form(self):
        return self._form


class Collection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.inserted = []

    def find_one(self, query, projection=None, sort=None):
        def matches(row):
            for key, value in query.items():
                if isinstance(value, dict) and "$in" in value:
                    if row.get(key) not in value["$in"]:
                        return False
                elif row.get(key) != value:
                    return False
            return True
        rows = [row for row in self.rows if matches(row)]
        return max(rows, key=lambda row: row.get("created_at", 0)) if rows else None

    def insert_one(self, row):
        self.inserted.append(row)
        return type("Result", (), {"inserted_id": ObjectId()})()

    def update_one(self, *args, **kwargs):
        return None


class Mongo:
    def __init__(self, leads, messages):
        self.leads, self.sms_messages, self.calls = Collection(leads), Collection(messages), Collection([])


def receive(mongo):
    with patch.object(leads_api, "mongo_client", mongo), patch.object(leads_api, "_require_db"), \
         patch.object(leads_api.realtime, "publish"):
        asyncio.run(leads_api.receive_inbound_sms(FakeRequest({"From": PERSON, "To": OURS, "Body": "Yes", "MessageSid": "SM1"})))
    return mongo.sms_messages.inserted[-1]


def test_reply_goes_to_the_workspace_and_lead_that_sent_the_message():
    # The same person is a lead in two workspaces; workspace "rj" texted them last. The "rj" lead came from a call
    # and has no workspace of its own.
    other, rj = ObjectId(), ObjectId()
    mongo = Mongo(
        leads=[{"_id": other, "phone": PERSON, "workspace_id": "team"}, {"_id": rj, "phone": PERSON}],
        messages=[{"direction": "outbound", "to": PERSON, "from": OURS, "workspace_id": "team", "lead_id": str(other), "created_at": 1},
                  {"direction": "outbound", "to": PERSON, "from": OURS, "workspace_id": "rj", "lead_id": str(rj), "created_at": 2}])
    saved = receive(mongo)
    assert (saved["workspace_id"], saved["lead_id"], saved["direction"], saved["body"]) == ("rj", str(rj), "inbound", "Yes")


def test_a_message_nobody_answered_falls_back_to_the_leads_workspace_then_default():
    lead = ObjectId()
    assert receive(Mongo([{"_id": lead, "phone": PERSON, "workspace_id": "team"}], []))["workspace_id"] == "team"
    assert receive(Mongo([{"_id": lead, "phone": PERSON}], []))["workspace_id"] == "default"
