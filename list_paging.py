"""Stable keyset cursors for ordered conversation pages."""

import base64
import json
from datetime import datetime

from fastapi import HTTPException
from bson import ObjectId
from datetime import timezone


def thread_cursor(row: dict) -> str:
    at = row["last_at"]
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    value = json.dumps([at.isoformat(), row["key"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def cursor_position(value: str) -> tuple:
    try:
        raw = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(raw, list) or len(raw) != 2 or not isinstance(raw[1], str):
            raise ValueError()
        at = datetime.fromisoformat(raw[0])
        if at.tzinfo is None:
            raise ValueError()
        return at.astimezone(timezone.utc).replace(tzinfo=None), raw[1]
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid conversation page cursor") from error


def record_cursor(row: dict, date_field: str) -> str:
    at = row.get(date_field)
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    if isinstance(at, datetime) and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    raw = json.dumps([at.isoformat() if isinstance(at, datetime) else None, str(row["_id"])], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def record_after(value: str, date_field: str) -> dict:
    try:
        raw = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(raw, list) or len(raw) != 2 or not ObjectId.is_valid(raw[1]):
            raise ValueError()
        identifier = ObjectId(raw[1])
        if raw[0] is None:
            return {date_field: None, "_id": {"$lt": identifier}}
        at = datetime.fromisoformat(raw[0])
        if at.tzinfo is None:
            raise ValueError()
        return {"$or": [{date_field: {"$lt": at}}, {date_field: at, "_id": {"$lt": identifier}}, {date_field: None}]}
    except (ValueError, TypeError, UnicodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid record page cursor") from error
