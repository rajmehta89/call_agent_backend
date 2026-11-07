"""A reply asking to stop blocks its sender; other replies do not, and no message is judged twice."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import campaign_replies


def test_a_stop_reply_blocks_only_its_sender(monkeypatch):
    db = MagicMock()
    seen = set()
    db.email_reply_checks.find_one.side_effect = lambda query: query["_id"] in seen or None
    db.email_reply_checks.insert_one.side_effect = lambda doc: seen.add(doc["_id"])
    monkeypatch.setattr(campaign_replies, "mongo_client", SimpleNamespace(db=db))
    monkeypatch.setattr(campaign_replies, "_last_check", campaign_replies.datetime.min)
    messages = [{"id": "1", "sender": "a@x.au", "subject": "Re: calls", "body": "please remove me"},
                {"id": "2", "sender": "b@y.au", "subject": "Re: calls", "body": "sounds good"}]
    monkeypatch.setattr(campaign_replies, "_recent_messages", lambda: messages)
    judged = []
    monkeypatch.setattr(campaign_replies, "_meaning",
                        lambda subject, body: judged.append(body) or ("stop" if "remove" in body else "interested"))
    assert campaign_replies.check_replies() == {"checked": 2, "stop": 1, "interested": 1}
    assert db.email_suppressions.update_one.call_count == 1
    assert db.email_suppressions.update_one.call_args[0][0] == {"email": "a@x.au"}
    monkeypatch.setattr(campaign_replies, "_last_check", campaign_replies.datetime.min)
    assert campaign_replies.check_replies()["checked"] == 0 and len(judged) == 2
