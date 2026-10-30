"""Commit a complete knowledge replacement as one MongoDB transaction.

Atlas (or a local replica set) is required. There is deliberately no destructive
fallback for standalone MongoDB: unsupported transactions leave working data intact.
"""

from datetime import datetime
from typing import Any


def replace_source(database: Any, source_id: str, documents: list, source: dict | None, replaced_ids: list | None = None) -> None:
    old_ids = list(dict.fromkeys([source_id, *(replaced_ids or [])]))

    def commit(session):
        database.brain_documents.delete_many({"source_id": {"$in": old_ids}}, session=session)
        if source_id == "brain_settings":
            database.brain_documents.delete_many({"source_id": {"$exists": False}}, session=session)
        if documents:
            database.brain_documents.insert_many(documents, session=session)
        if source:
            database.brain_sources.update_one(
                {"source_id": source_id}, {"$set": source, "$setOnInsert": {"created_at": datetime.utcnow()}},
                upsert=True, session=session,
            )
            if replaced_ids:
                database.brain_sources.delete_many({"source_id": {"$in": [value for value in replaced_ids if value != source_id]}}, session=session)
        else:
            database.brain_sources.delete_many({"source_id": {"$in": old_ids}}, session=session)

    with database.client.start_session() as session:
        session.with_transaction(commit, max_commit_time_ms=15000)
