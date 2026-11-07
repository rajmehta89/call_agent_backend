from types import SimpleNamespace

import auth_service
import notification_service
import realtime


class FakeCollection:
    def __init__(self):
        self.documents = []

    def insert_one(self, document):
        self.documents.append(document)
        return SimpleNamespace(inserted_id=str(len(self.documents)))


class FakeUsers:
    def __init__(self, members, workspace="default"):
        self.members = members
        self.workspace = workspace

    def find(self, query):
        assert query == {"workspace_id": self.workspace, "active": {"$ne": False}}
        return self.members

    def distinct(self, field, query):
        assert field == "workspace_id"
        assert query == {"active": {"$ne": False}}
        return [self.workspace]

    def count_documents(self, query):
        return 0


def test_new_lead_posts_to_team_and_notifies_sales_member(monkeypatch):
    member = {"_id": "agent-1", "name": "Test Member", "email": "member@example.test"}
    notifications = FakeCollection()
    messages = FakeCollection()
    fake_mongo = SimpleNamespace(
        is_connected=lambda: True,
        notifications=notifications,
        team_messages=messages,
        users=FakeUsers([member]),
    )
    monkeypatch.setattr(notification_service, "mongo_client", fake_mongo)
    monkeypatch.setattr(auth_service, "has_permission", lambda user, permission: permission == "manage_assigned_leads")
    published = []
    monkeypatch.setattr(realtime, "publish", lambda *args, **kwargs: published.append((args, kwargs)))

    count = notification_service.notify_new_lead(
        {"_id": "lead-1", "name": "Test Lead", "phone": "+15555550123", "email": "lead@example.test"},
        source="manual",
    )

    assert count == 1
    assert messages.documents[0]["type"] == "new_lead"
    assert messages.documents[0]["link"] == "/inbox/channel"
    assert notifications.documents[0]["channel"] == "lead"
    assert notifications.documents[0]["recipient"] == member["email"]
    assert notifications.documents[0]["lead_id"] == "lead-1"
    assert notifications.documents[0]["link"] == "/leads"
    assert notifications.documents[0]["read"] is False
    assert published[0][0] == ("lead",)
    assert published[0][1] == {"key": "lead-1", "workspace": "default"}


def test_resolves_single_active_workspace_for_provider_webhooks(monkeypatch):
    class WorkspaceUsers:
        def distinct(self, field, query):
            assert field == "workspace_id"
            assert query == {"active": {"$ne": False}}
            return ["rj"]

        def count_documents(self, query):
            return 0

    monkeypatch.delenv("INBOUND_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(notification_service, "mongo_client", SimpleNamespace(
        is_connected=lambda: True,
        users=WorkspaceUsers(),
    ))
    assert notification_service.resolve_inbound_workspace_id() == "rj"


def test_new_lead_uses_resolved_workspace_for_provider_lead(monkeypatch):
    member = {"_id": "agent-1", "name": "Test Member", "email": "member@example.test"}
    notifications = FakeCollection()
    messages = FakeCollection()
    fake_mongo = SimpleNamespace(
        is_connected=lambda: True,
        notifications=notifications,
        team_messages=messages,
        users=FakeUsers([member], workspace="rj"),
    )
    monkeypatch.setattr(notification_service, "mongo_client", fake_mongo)
    monkeypatch.setattr(auth_service, "has_permission", lambda user, permission: permission == "manage_assigned_leads")
    published = []
    monkeypatch.setattr(realtime, "publish", lambda *args, **kwargs: published.append((args, kwargs)))

    notification_service.notify_new_lead({"_id": "lead-rj", "name": "Inbound lead"}, source="voice")

    assert messages.documents[0]["workspace_id"] == "rj"
    assert notifications.documents[0]["workspace_id"] == "rj"
    assert published[0][1]["workspace"] == "rj"


def test_csv_import_creates_one_channel_post_and_alert_per_sales_member(monkeypatch):
    member = {"_id": "agent-1", "name": "Test Member", "email": "member@example.test"}
    notifications = FakeCollection()
    messages = FakeCollection()
    fake_mongo = SimpleNamespace(
        is_connected=lambda: True,
        notifications=notifications,
        team_messages=messages,
        users=FakeUsers([member], workspace="rj"),
    )
    monkeypatch.setattr(notification_service, "mongo_client", fake_mongo)
    monkeypatch.setattr(auth_service, "has_permission", lambda user, permission: permission == "manage_assigned_leads")
    published = []
    monkeypatch.setattr(realtime, "publish", lambda *args, **kwargs: published.append((args, kwargs)))

    count = notification_service.notify_new_leads_imported(4, workspace_id="rj")

    assert count == 1
    assert len(messages.documents) == 1
    assert "4 new leads imported" in messages.documents[0]["text"]
    assert len(notifications.documents) == 1
    assert notifications.documents[0]["type"] == "new_lead"
    assert notifications.documents[0]["lead_count"] == 4
    assert notifications.documents[0]["workspace_id"] == "rj"
    assert notifications.documents[0]["link"] == "/leads"
    assert published[0][1]["workspace"] == "rj"
