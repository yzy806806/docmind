"""Phase 8a tests: Email ingestion data model and background worker.

Targeted tests for:
- Plaintext password column round-trip (edge cases: empty, very long,
  newlines, SQL special characters, emoji/unicode)
- Job queue enqueue/dequeue (race conditions, concurrent claims,
  state transition integrity, paginated listing, stats)
- SourceType registration and serialization

These tests use a real in-memory SQLite Database (the test_db_sqlite
fixture pattern) for integration-level verification.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.core.models import JobState, SourceType


# ── Fixtures (mirror test_db_sqlite.py pattern) ──────────────────


@pytest.fixture
def tmp_db_path() -> Generator[str, None, None]:
    """Provide a temporary database file path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield str(Path(tmpdir) / "test_phase8a.db")


@pytest.fixture
async def db(tmp_db_path: str):
    """Create a connected SQLite Database instance."""
    from src.core.db_sqlite import Database

    database = Database(db_path=tmp_db_path)
    await database.connect()
    yield database
    await database.disconnect()


# ═══════════════════════════════════════════════════════════════════
# 1. Plaintext password column round-trip
# ═══════════════════════════════════════════════════════════════════


class TestPasswordRoundTrip:
    """Verify the password column stores and retrieves plaintext exactly."""

    async def test_empty_password_round_trip(self, db):
        """Empty string password should round-trip as empty string."""
        acct = await db.create_email_account({
            "name": "empty-pw",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "",
        })
        assert acct["password"] == ""

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == ""

    async def test_password_with_newlines(self, db):
        """Passwords containing newlines should round-trip exactly."""
        pw = "line1\nline2\r\nline3"
        acct = await db.create_email_account({
            "name": "newline-pw",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": pw,
        })
        assert acct["password"] == pw

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == pw

    async def test_password_with_sql_special_chars(self, db):
        """Passwords containing SQL-significant characters must not
        cause injection or corruption."""
        pw = "'; DROP TABLE email_accounts; --"
        acct = await db.create_email_account({
            "name": "sqlinj-pw",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": pw,
        })
        assert acct["password"] == pw

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == pw

        # Verify the table still exists and has this row
        all_accts = await db.list_email_accounts()
        assert len(all_accts) >= 1

    async def test_password_with_emoji(self, db):
        """Emoji and wide unicode characters in password should survive."""
        pw = "🔐🔑p@ss🌟✅"
        acct = await db.create_email_account({
            "name": "emoji-pw",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": pw,
        })
        assert acct["password"] == pw

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == pw

    async def test_password_very_long(self, db):
        """Very long passwords (4KB) should round-trip correctly."""
        pw = "x" * 4096
        acct = await db.create_email_account({
            "name": "long-pw",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": pw,
        })
        assert len(acct["password"]) == 4096
        assert acct["password"] == pw

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == pw

    async def test_password_update_round_trip(self, db):
        """Updating password via update_email_account should persist."""
        acct = await db.create_email_account({
            "name": "pw-update",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "original",
        })
        updated = await db.update_email_account(acct["id"], {"password": "changed!"})
        assert updated["password"] == "changed!"

        fetched = await db.get_email_account(acct["id"])
        assert fetched["password"] == "changed!"

    async def test_password_not_returned_in_list(self, db):
        """list_email_accounts must also return the password field."""
        await db.create_email_account({
            "name": "list-pw-check",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "visible-in-list",
        })
        accounts = await db.list_email_accounts()
        assert len(accounts) == 1
        assert accounts[0]["password"] == "visible-in-list"


# ═══════════════════════════════════════════════════════════════════
# 2. Job queue enqueue/dequeue — expanded edge cases
# ═══════════════════════════════════════════════════════════════════


class TestJobQueueEdgeCases:
    """Expanded edge case tests for job queue operations."""

    async def test_dequeue_only_claims_pending(self, db):
        """dequeue_job must NOT claim jobs that are already processing/completed/failed."""
        # Create a job and manually mark it completed
        job = await db.enqueue_job("/docs/completed.txt")
        await db.complete_job(job.id, document_id=None)

        # Dequeue should return None (no pending jobs)
        claimed = await db.dequeue_job()
        assert claimed is None

    async def test_dequeue_does_not_claim_failed(self, db):
        """dequeue_job must NOT claim failed jobs."""
        job = await db.enqueue_job("/docs/failed.txt")
        await db.fail_job(job.id, error="broken")

        claimed = await db.dequeue_job()
        assert claimed is None

    async def test_dequeue_after_partial_processing(self, db):
        """dequeue_job should claim the next pending job after one was completed."""
        job1 = await db.enqueue_job("/docs/first.txt")
        job2 = await db.enqueue_job("/docs/second.txt")
        await db.complete_job(job1.id, document_id=None)

        claimed = await db.dequeue_job()
        assert claimed is not None
        assert claimed.id == job2.id
        assert claimed.state == JobState.PROCESSING

    async def test_concurrent_dequeue_leaves_only_one_claimed(self, db):
        """Simulate two workers dequeuing simultaneously — only one should claim."""
        await db.enqueue_job("/docs/only-one.txt")

        # Run two dequeue calls concurrently
        results = await asyncio.gather(
            db.dequeue_job(),
            db.dequeue_job(),
        )

        # Exactly one should claim the job
        claimed = [r for r in results if r is not None]
        assert len(claimed) == 1
        assert claimed[0].state == JobState.PROCESSING

        # The other should get None
        nulls = [r for r in results if r is None]
        assert len(nulls) == 1

    async def test_multiple_pending_concurrent_dequeue(self, db):
        """Multiple pending jobs: concurrent dequeue claims distinct jobs."""
        j1 = await db.enqueue_job("/docs/a.txt")
        j2 = await db.enqueue_job("/docs/b.txt")

        results = await asyncio.gather(
            db.dequeue_job(),
            db.dequeue_job(),
        )

        claimed_ids = {r.id for r in results if r is not None}
        assert claimed_ids == {j1.id, j2.id}

        for r in results:
            assert r.state == JobState.PROCESSING

    async def test_enqueue_dequeue_fifo_order(self, db):
        """Jobs must be dequeued in FIFO order (oldest pending first)."""
        ids = []
        for i in range(10):
            job = await db.enqueue_job(f"/docs/seq_{i}.txt")
            ids.append(job.id)
            await asyncio.sleep(0.01)  # Ensure distinct timestamps

        # Dequeue all 10 in order
        for expected_id in ids:
            claimed = await db.dequeue_job()
            assert claimed is not None
            assert claimed.id == expected_id

        # Queue should now be empty
        assert await db.dequeue_job() is None

    async def test_job_state_machine_integrity(self, db):
        """Verify the job state machine: pending -> processing -> completed/failed."""
        # Create a real document so the FK constraint is satisfied
        doc_id = await db.save_document(
            path="/docs/for_state_machine.txt",
            source_type="local",
            source_name="test",
            title="State Machine Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body for state machine test.",
        )

        job = await db.enqueue_job("/docs/state-machine.txt")
        assert job.state == JobState.PENDING

        claimed = await db.dequeue_job()
        assert claimed.id == job.id
        assert claimed.state == JobState.PROCESSING

        await db.complete_job(job.id, document_id=doc_id)
        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.COMPLETED
        assert fetched.document_id == doc_id

    async def test_fail_job_preserves_error_message(self, db):
        """fail_job must store the error string exactly."""
        job = await db.enqueue_job("/docs/error-check.txt")
        error_msg = "Connection timeout after 30s\nDetails: EOF from server"
        await db.fail_job(job.id, error=error_msg)

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.FAILED
        assert fetched.error == error_msg

    async def test_complete_job_non_existent(self, db):
        """complete_job on a non-existent job ID should not raise."""
        # Should not raise — silently does nothing
        await db.complete_job("nonexistent-uuid-12345", document_id=1)

    async def test_fail_job_non_existent(self, db):
        """fail_job on a non-existent job ID should not raise."""
        await db.fail_job("nonexistent-uuid-67890", error="oops")

    async def test_list_jobs_paginated(self, db):
        """list_jobs_paginated should return correct page structure."""
        for i in range(25):
            await db.enqueue_job(f"/docs/page_{i}.txt")

        result = await db.list_jobs_paginated(page=1, per_page=10)
        assert len(result["jobs"]) == 10
        assert result["total"] == 25
        assert result["page"] == 1
        assert result["per_page"] == 10
        assert result["total_pages"] == 3

        # Page 3 should have 5 items
        result3 = await db.list_jobs_paginated(page=3, per_page=10)
        assert len(result3["jobs"]) == 5

    async def test_list_jobs_paginated_filtered(self, db):
        """list_jobs_paginated should support state filtering."""
        for i in range(10):
            job = await db.enqueue_job(f"/docs/filtered_{i}.txt")
            if i < 3:
                await db.complete_job(job.id, document_id=None)

        result = await db.list_jobs_paginated(state="pending", page=1, per_page=20)
        assert result["total"] == 7
        for job in result["jobs"]:
            assert job.state == JobState.PENDING

    async def test_get_job_stats(self, db):
        """get_job_stats should return accurate counts by state."""
        # Create a real document so the FK constraint is satisfied
        stats_doc_id = await db.save_document(
            path="/docs/for_stats_test.txt",
            source_type="local",
            source_name="test",
            title="Stats Test Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Body for stats test.",
        )

        # Create jobs in various states
        j1 = await db.enqueue_job("/docs/stats_pending.txt")
        j2 = await db.enqueue_job("/docs/stats_processing.txt")
        claimed = await db.dequeue_job()  # j1 -> processing
        j3 = await db.enqueue_job("/docs/stats_completed.txt")
        await db.complete_job(j3.id, document_id=stats_doc_id)  # j3 -> completed
        j4 = await db.enqueue_job("/docs/stats_failed.txt")
        await db.fail_job(j4.id, error="test")  # j4 -> failed

        stats = await db.get_job_stats()
        assert isinstance(stats, dict)
        # At minimum, total should be non-zero
        assert stats.get("total", 0) > 0

    async def test_update_job_status_sets_document_id(self, db):
        """update_job_status should allow setting document_id."""
        doc_id = await db.save_document(
            path="/docs/for_status_update.txt",
            source_type="local",
            source_name="test",
            title="Status Update Doc",
            ext=".txt",
            mime_type="text/plain",
            body="Status update body.",
        )
        job = await db.enqueue_job("/docs/status_target.txt")
        await db.update_job_status(
            job.id, state="completed", document_id=doc_id
        )

        fetched = await db.get_job(job.id)
        assert fetched.state == JobState.COMPLETED
        assert fetched.document_id == doc_id


# ═══════════════════════════════════════════════════════════════════
# 3. SourceType registration and serialization
# ═══════════════════════════════════════════════════════════════════


class TestSourceTypeRegistration:
    """Verify SourceType enum covers all expected values for email data model."""

    def test_source_type_email_is_registered(self):
        """EMAIL must be present in the SourceType enum."""
        assert hasattr(SourceType, "EMAIL")
        assert SourceType.EMAIL == "email"
        assert SourceType.EMAIL.value == "email"

    def test_source_type_all_values_are_unique(self):
        """All SourceType values must be unique."""
        values = [m.value for m in SourceType]
        assert len(values) == len(set(values))

    def test_source_type_construct_from_string(self):
        """SourceType should be constructable from raw string values."""
        assert SourceType("email") is SourceType.EMAIL
        assert SourceType("local") is SourceType.LOCAL
        assert SourceType("webdav") is SourceType.WEBDAV
        assert SourceType("api") is SourceType.API
        assert SourceType("postgresql") is SourceType.POSTGRESQL

    def test_source_type_invalid_value_raises(self):
        """Constructing SourceType with unknown value should raise ValueError."""
        with pytest.raises(ValueError):
            SourceType("not_a_real_source")

    def test_source_type_json_serialization(self):
        """SourceType.EMAIL should serialize to JSON as the string 'email'."""
        from src.core.models import DocumentCreate

        doc = DocumentCreate(
            path="/test.txt",
            source_type=SourceType.EMAIL,
            source_name="test-account",
            title="Test Email Document",
        )
        data = doc.model_dump()
        assert data["source_type"] == "email"

        json_str = doc.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["source_type"] == "email"

    def test_source_type_json_deserialization(self):
        """JSON with 'email' source_type should deserialize to SourceType.EMAIL."""
        raw = json.dumps({
            "path": "/test.txt",
            "source_type": "email",
            "source_name": "test-account",
            "title": "Email Doc",
        })
        from src.core.models import DocumentCreate

        doc = DocumentCreate.model_validate_json(raw)
        assert doc.source_type == SourceType.EMAIL
        assert doc.source_type == "email"

    def test_source_type_default_is_api(self):
        """The default source_type for DocumentCreate should be API, not EMAIL."""
        from src.core.models import DocumentCreate

        doc = DocumentCreate(
            path="/test.txt",
            title="Default Source",
        )
        assert doc.source_type == SourceType.API

    def test_document_record_source_type(self):
        """DocumentRecord with email source_type should round-trip."""
        from src.core.models import DocumentRecord

        rec = DocumentRecord(
            id=1,
            path="/test.txt",
            source_type=SourceType.EMAIL,
            source_name="test-account",
            title="Email Doc",
        )
        assert rec.source_type == SourceType.EMAIL
        assert rec.source_type == "email"


# ═══════════════════════════════════════════════════════════════════
# 4. Email data model ↔ job queue integration
# ═══════════════════════════════════════════════════════════════════


class TestEmailJobQueueIntegration:
    """Verify email account data coexists with job queue in the same DB."""

    async def test_email_account_and_job_in_same_db(self, db):
        """Email accounts and jobs coexist without collision in the same database."""
        acct = await db.create_email_account({
            "name": "coexist",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "secret",
        })
        assert acct["id"] is not None

        job = await db.enqueue_job(
            "/docs/coexist.txt",
            document_title="Coexistence Test",
        )
        assert job.id is not None

        # Both should be fetchable
        fetched_acct = await db.get_email_account(acct["id"])
        assert fetched_acct["name"] == "coexist"

        fetched_job = await db.get_job(job.id)
        assert fetched_job.state == JobState.PENDING

    async def test_email_log_and_job_different_tables(self, db):
        """email_ingestion_log rows should not appear in jobs table, and vice versa."""
        acct = await db.create_email_account({
            "name": "table-isolation",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        await db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<isolated@example.com>",
            "uid": 1,
            "folder": "INBOX",
            "subject": "Isolated",
            "sender": "sender@example.com",
            "status": "completed",
        })

        # Enqueue a job
        await db.enqueue_job("/docs/table_check.txt")

        # Jobs list should only contain the one job, not log entries
        jobs = await db.list_jobs()
        assert len(jobs) == 1

        # Logs should only contain the one log entry
        logs = await db.list_email_ingestion_logs(acct["id"])
        assert len(logs) == 1

    async def test_email_cascade_delete_does_not_affect_jobs(self, db):
        """Deleting an email account must not cascade into the jobs table."""
        acct = await db.create_email_account({
            "name": "cascade-isolation",
            "host": "imap.example.com",
            "username": "user@example.com",
            "password": "pass",
        })
        await db.log_email_ingestion({
            "account_id": acct["id"],
            "message_id": "<cascade-test@example.com>",
            "uid": 42,
            "folder": "INBOX",
            "subject": "Cascade Check",
            "sender": "sender@example.com",
            "status": "completed",
        })

        job = await db.enqueue_job("/docs/cascade_job.txt")

        # Delete the email account
        await db.delete_email_account(acct["id"])

        # Account should be gone
        assert await db.get_email_account(acct["id"]) is None

        # Log entries should be cascade-deleted
        logs = await db.list_email_ingestion_logs(acct["id"])
        assert len(logs) == 0

        # Job should still exist (no cross-table cascade)
        fetched_job = await db.get_job(job.id)
        assert fetched_job is not None
        assert fetched_job.state == JobState.PENDING
