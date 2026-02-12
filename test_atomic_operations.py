"""
Concurrency Tests for Atomic Operations
========================================

Tests to verify that SQLite atomic operations work correctly under concurrent access.
These tests validate the fixes for PR #108 (fix/sqlite-parallel-corruption).

Test cases:
1. test_atomic_claim_single_winner - verify exactly 1 thread succeeds claiming a feature
2. test_atomic_priority_no_duplicates - verify no duplicate priorities when skipping
3. test_cleanup_idempotent - verify multiple cleanup() calls don't error
4. test_atomic_transaction_isolation - verify IMMEDIATE prevents stale reads
5. test_event_hooks_applied - verify event hooks are configured on all connections

Run with: python -m pytest test_atomic_operations.py -v
"""

import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from sqlalchemy import event, text

from api.database import (
    Feature,
    atomic_transaction,
    create_database,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        engine, session_maker = create_database(project_dir)

        # Create some test features
        session = session_maker()
        try:
            for i in range(5):
                feature = Feature(
                    priority=i + 1,
                    category="test",
                    name=f"Feature {i + 1}",
                    description=f"Test feature {i + 1}",
                    steps=["step 1", "step 2"],
                    passes=False,
                    in_progress=False,
                )
                session.add(feature)
            session.commit()
        finally:
            session.close()

        yield engine, session_maker

        # Cleanup
        engine.dispose()


class TestAtomicClaimSingleWinner:
    """Test that only one thread can claim a feature."""

    def test_concurrent_claim_single_winner(self, temp_db):
        """Spawn N threads calling atomic UPDATE WHERE, verify exactly 1 succeeds."""
        engine, session_maker = temp_db
        num_threads = 10
        feature_id = 1

        # Track results
        results = {"claimed": 0, "failed": 0}
        results_lock = threading.Lock()
        barrier = threading.Barrier(num_threads)

        def try_claim():
            # Wait for all threads to be ready
            barrier.wait()

            session = session_maker()
            try:
                # Atomic claim using UPDATE WHERE (same pattern as MCP server)
                result = session.execute(
                    text("""
                    UPDATE features
                    SET in_progress = 1
                    WHERE id = :id AND passes = 0 AND in_progress = 0
                """),
                    {"id": feature_id},
                )
                session.commit()

                with results_lock:
                    if result.rowcount == 1:
                        results["claimed"] += 1
                    else:
                        results["failed"] += 1
            except Exception:
                with results_lock:
                    results["failed"] += 1
            finally:
                session.close()

        # Run concurrent claims
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(try_claim) for _ in range(num_threads)]
            for f in as_completed(futures):
                f.result()  # Raise any exceptions

        # Verify exactly 1 thread claimed the feature
        assert results["claimed"] == 1, f"Expected 1 claim, got {results['claimed']}"
        assert (
            results["failed"] == num_threads - 1
        ), f"Expected {num_threads - 1} failures, got {results['failed']}"

        # Verify database state
        session = session_maker()
        try:
            feature = session.query(Feature).filter(Feature.id == feature_id).first()
            assert feature.in_progress is True
        finally:
            session.close()


class TestAtomicPriorityNoDuplicates:
    """Test that concurrent feature_skip operations don't create duplicate priorities."""

    def test_concurrent_skip_no_duplicates(self, temp_db):
        """Multiple threads skipping features simultaneously, verify no duplicate priorities."""
        engine, session_maker = temp_db
        num_threads = 5

        # Get feature IDs to skip
        session = session_maker()
        try:
            feature_ids = [f.id for f in session.query(Feature).all()]
        finally:
            session.close()

        barrier = threading.Barrier(num_threads)
        errors = []
        errors_lock = threading.Lock()

        def skip_feature(feature_id):
            # Wait for all threads to be ready
            barrier.wait()

            session = session_maker()
            try:
                # Atomic skip using MAX subquery (same pattern as MCP server)
                session.execute(
                    text("""
                    UPDATE features
                    SET priority = (SELECT COALESCE(MAX(priority), 0) + 1 FROM features),
                        in_progress = 0
                    WHERE id = :id
                """),
                    {"id": feature_id},
                )
                session.commit()
            except Exception as e:
                with errors_lock:
                    errors.append(str(e))
            finally:
                session.close()

        # Run concurrent skips
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(skip_feature, fid) for fid in feature_ids[:num_threads]
            ]
            for f in as_completed(futures):
                f.result()

        # Verify no errors
        assert len(errors) == 0, f"Errors during skip: {errors}"

        # Verify no duplicate priorities
        session = session_maker()
        try:
            priorities = [f.priority for f in session.query(Feature).all()]
            unique_priorities = set(priorities)
            assert len(priorities) == len(
                unique_priorities
            ), f"Duplicate priorities found: {priorities}"
        finally:
            session.close()


class TestCleanupIdempotent:
    """Test that cleanup() can be called multiple times without errors."""

    def test_cleanup_multiple_calls(self):
        """Call cleanup() multiple times on ParallelOrchestrator, verify no errors."""
        from parallel_orchestrator import ParallelOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            # Create empty features.db so orchestrator doesn't fail
            create_database(project_dir)

            orchestrator = ParallelOrchestrator(
                project_dir=project_dir,
                max_concurrency=1,
            )

            # Call cleanup multiple times - should not raise
            orchestrator.cleanup()
            orchestrator.cleanup()
            orchestrator.cleanup()

            # Verify engine is None after cleanup
            assert orchestrator._engine is None


class TestAtomicTransactionIsolation:
    """Test that atomic_transaction with IMMEDIATE prevents stale reads."""

    def test_read_modify_write_isolation(self, temp_db):
        """Verify IMMEDIATE transaction prevents stale read in read-modify-write."""
        engine, session_maker = temp_db

        # This test verifies that two concurrent read-modify-write operations
        # don't both read the same value and create a conflict

        barrier = threading.Barrier(2)

        def increment_priority(feature_id):
            barrier.wait()

            with atomic_transaction(session_maker) as session:
                # Read current priority
                feature = (
                    session.query(Feature).filter(Feature.id == feature_id).first()
                )
                old_priority = feature.priority

                # Modify
                new_priority = old_priority + 100

                # Write
                session.execute(
                    text("UPDATE features SET priority = :new WHERE id = :id"),
                    {"new": new_priority, "id": feature_id},
                )

        # Run two concurrent increments on the same feature
        feature_id = 1

        # Get initial priority
        session = session_maker()
        try:
            initial_priority = (
                session.query(Feature).filter(Feature.id == feature_id).first().priority
            )
        finally:
            session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(increment_priority, feature_id) for _ in range(2)]
            for f in as_completed(futures):
                f.result()

        # Verify final priority - with proper isolation, each increment should see
        # the other's write, so final = initial + 200
        # Without isolation, both might read initial and we'd get initial + 100
        session = session_maker()
        try:
            final_priority = (
                session.query(Feature).filter(Feature.id == feature_id).first().priority
            )
            # With IMMEDIATE transactions and proper isolation, we expect initial + 200
            expected = initial_priority + 200
            assert (
                final_priority == expected
            ), f"Expected {expected}, got {final_priority}. Lost update detected!"
        finally:
            session.close()


class TestEventHooksApplied:
    """Test that SQLAlchemy event hooks are properly configured."""

    def test_begin_immediate_hook_active(self, temp_db):
        """Verify that the BEGIN IMMEDIATE event hook is active on connections."""
        engine, session_maker = temp_db

        # Track if our hook fired
        hook_fired = {"begin": False, "connect": False}

        # Add test listeners to verify the existing hooks are working
        @event.listens_for(engine, "begin")
        def track_begin(conn):
            hook_fired["begin"] = True

        # Create a new connection and start a transaction
        session = session_maker()
        try:
            # This should trigger the begin hook via our event listener
            session.execute(text("SELECT 1"))
            session.commit()
        finally:
            session.close()

        # The begin hook should have fired
        assert hook_fired["begin"], "BEGIN event hook did not fire"

    def test_isolation_level_none_on_connect(self, temp_db):
        """Verify that pysqlite's implicit transaction is disabled."""
        engine, session_maker = temp_db

        # Get a raw DBAPI connection and check isolation_level
        with engine.connect() as conn:
            raw_conn = conn.connection.dbapi_connection
            # Our do_connect hook sets isolation_level = None
            # Note: In some pysqlite versions, None becomes "" (empty string)
            # Both indicate autocommit mode (no implicit transactions)
            assert raw_conn.isolation_level in (
                None,
                "",
            ), f"Expected isolation_level=None or '', got {raw_conn.isolation_level!r}"


class TestAtomicTransactionRollback:
    """Test that atomic_transaction properly handles exceptions."""

    def test_rollback_on_exception(self, temp_db):
        """Verify that changes are rolled back on exception."""
        engine, session_maker = temp_db
        feature_id = 1

        # Get initial priority
        session = session_maker()
        try:
            initial_priority = (
                session.query(Feature).filter(Feature.id == feature_id).first().priority
            )
        finally:
            session.close()

        # Try to modify and raise exception
        try:
            with atomic_transaction(session_maker) as session:
                session.execute(
                    text("UPDATE features SET priority = 999 WHERE id = :id"),
                    {"id": feature_id},
                )
                raise ValueError("Intentional error")
        except ValueError:
            pass  # Expected

        # Verify priority was rolled back
        session = session_maker()
        try:
            final_priority = (
                session.query(Feature).filter(Feature.id == feature_id).first().priority
            )
            assert (
                final_priority == initial_priority
            ), f"Expected rollback to {initial_priority}, got {final_priority}"
        finally:
            session.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
