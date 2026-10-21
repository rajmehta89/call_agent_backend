"""Sign-in: wrong passwords lock the email for a while, and deactivated accounts are told so."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.auth_api as auth_api
from auth_service import hash_password


class FakeUsers:
    def __init__(self, users):
        self.users = users

    def find_one(self, query):
        return next((user for user in self.users if user["email"] == query.get("email")), None)


class FakeFailures:
    def __init__(self):
        self.rows = []

    def count_documents(self, query):
        return sum(1 for row in self.rows if row["email"] == query["email"] and row["at"] > query["at"]["$gt"])

    def insert_one(self, row):
        self.rows.append(row)

    def delete_many(self, query):
        self.rows = [row for row in self.rows if row["email"] != query["email"]]


class FakeMongo:
    def __init__(self, users):
        self.users = FakeUsers(users)
        self.login_failures = FakeFailures()

    def is_connected(self):
        return True


@pytest.fixture
def login(monkeypatch):
    users = [{"_id": "u1", "email": "ok@example.com", "password_hash": hash_password("right-password"), "role": "agent"},
             {"_id": "u2", "email": "gone@example.com", "password_hash": hash_password("right-password"), "role": "agent", "active": False}]
    monkeypatch.setattr(auth_api, "mongo_client", FakeMongo(users))
    monkeypatch.setattr(auth_api, "_auth_response", lambda user: {"token": "t", "user": {"id": user["_id"]}})
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app)
    return lambda email, password: client.post("/api/auth/login", json={"email": email, "password": password})


def test_wrong_passwords_lock_the_email(login):
    for _ in range(auth_api.LOGIN_MAX_FAILURES):
        assert login("ok@example.com", "wrong").status_code == 401
    # Locked now, even with the right password.
    assert login("ok@example.com", "right-password").status_code == 429


def test_success_clears_earlier_failures(login):
    for _ in range(auth_api.LOGIN_MAX_FAILURES - 1):
        login("ok@example.com", "wrong")
    assert login("ok@example.com", "right-password").status_code == 200
    assert login("ok@example.com", "wrong").status_code == 401  # counting starts again


def test_deactivated_account_is_told_so(login):
    response = login("gone@example.com", "right-password")
    assert response.status_code == 403 and "deactivated" in response.json()["detail"]
