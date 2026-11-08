"""
Project SQLite databases are never opened through a planted link
================================================================

SQLite opens features.db / assistant.db / logs.db and their -wal/-shm/-journal
files by path and follows symlinks. The agent can plant symlinks and hard links
in the project, so the server checks every project database before opening it
(api.database.check_project_database_path) and refuses with a clear error instead
of reading or writing a database outside the project, or creating a fresh one.

Run with: python -m pytest test_project_database_links.py
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from api import database
from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "project"
    (folder / ".autoforge").mkdir(parents=True)
    yield folder
    database.dispose_engine(folder)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


def _outside_db(outside: Path, name: str = "other.db") -> Path:
    """A database outside the project with one stuck feature row."""
    path = outside / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE features (id INTEGER PRIMARY KEY, passes BOOLEAN, in_progress BOOLEAN)")
    conn.execute("INSERT INTO features (passes, in_progress) VALUES (0, 1)")
    conn.commit()
    conn.close()
    return path


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _in_progress(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT in_progress FROM features").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["features.db", "features.db-wal", "features.db-shm", "features.db-journal"])
def test_check_refuses_symlink_at_database_or_companion(project: Path, outside: Path, name: str) -> None:
    (outside / "target").write_bytes(b"x")
    (project / ".autoforge" / name).symlink_to(outside / "target")

    with pytest.raises(sio.UnsafeProjectPathError, match="Refusing to open the project database"):
        database.check_project_database_path(project, project / ".autoforge" / "features.db")


def test_check_refuses_hard_link_and_symlinked_dir(project: Path, outside: Path) -> None:
    import os

    db = _outside_db(outside)
    os.link(db, project / ".autoforge" / "features.db")
    with pytest.raises(sio.UnsafeProjectPathError):
        database.check_project_database_path(project, project / ".autoforge" / "features.db")

    (project / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sio.UnsafeProjectPathError):
        database.check_project_database_path(project, project / "linked" / "features.db")


def test_check_accepts_missing_and_regular_files(project: Path) -> None:
    db_path = project / ".autoforge" / "features.db"
    database.check_project_database_path(project, db_path)  # nothing there yet
    database.check_project_database_path(project, project / "missing" / "features.db")
    db_path.write_bytes(b"")
    (project / ".autoforge" / "features.db-wal").write_bytes(b"")
    database.check_project_database_path(project, db_path)


def test_create_database_refuses_linked_database(project: Path, outside: Path) -> None:
    db = _outside_db(outside)
    (project / ".autoforge" / "features.db").symlink_to(db)

    with pytest.raises(sio.UnsafeProjectPathError):
        database.create_database(project)

    assert _tables(db) == {"features"}  # no schedules/migrations written into it


def test_create_database_refuses_symlinked_autoforge_dir(project: Path, outside: Path) -> None:
    (project / ".autoforge").rmdir()
    (project / ".autoforge").symlink_to(outside, target_is_directory=True)

    with pytest.raises(sio.UnsafeProjectPathError):
        database.create_database(project)

    assert list(outside.iterdir()) == []  # no fresh features.db created outside


def test_create_database_refuses_linked_wal(project: Path, outside: Path) -> None:
    (outside / "wal").write_bytes(b"keep")
    (project / ".autoforge" / "features.db-wal").symlink_to(outside / "wal")

    with pytest.raises(sio.UnsafeProjectPathError):
        database.create_database(project)

    assert (outside / "wal").read_bytes() == b"keep"


def test_assistant_database_refuses_linked_database(project: Path, outside: Path) -> None:
    from server.services import assistant_database

    db = _outside_db(outside)
    (project / ".autoforge" / "assistant.db").symlink_to(db)
    try:
        with pytest.raises(sio.UnsafeProjectPathError):
            assistant_database.get_engine(project)
    finally:
        assistant_database.dispose_engine(project)

    assert _tables(db) == {"features"}


def test_logs_database_refuses_linked_database(project: Path, outside: Path) -> None:
    from structured_logging import get_log_query, get_logger

    db = _outside_db(outside)
    (project / ".autoforge" / "logs.db").symlink_to(db)

    with pytest.raises(sio.UnsafeProjectPathError):
        get_logger(project, agent_id="a", console_output=False)
    with pytest.raises(sio.UnsafeProjectPathError):
        get_log_query(project)

    assert _tables(db) == {"features"}


def test_progress_queries_refuse_linked_database(project: Path, outside: Path, capsys) -> None:
    import progress

    db = _outside_db(outside)
    (project / ".autoforge" / "features.db").symlink_to(db)
    (project / "features.db").symlink_to(db)  # clear_stuck_features reads the root location

    with pytest.raises(sio.UnsafeProjectPathError):
        progress.has_features(project)
    assert progress.count_passing_tests(project) == (0, 0, 0, 0)
    assert progress.clear_stuck_features(project) == 0

    assert _in_progress(db) == 1
    assert "Refusing to open the project database" in capsys.readouterr().out


def test_stale_feature_cleanup_refuses_linked_database(project: Path, outside: Path) -> None:
    from api.database import Feature
    from server.services.process_manager import AgentProcessManager

    _engine, session_maker = database.create_database(outside)  # a real features.db elsewhere
    session = session_maker()
    session.add(Feature(priority=1, category="c", name="n", description="d", steps=[], in_progress=True))
    session.commit()
    session.close()
    database.dispose_engine(outside)
    db = outside / ".autoforge" / "features.db"
    (project / ".autoforge" / "features.db").symlink_to(db)

    AgentProcessManager("demo", project, Path(__file__).parent)._cleanup_stale_features()

    assert _in_progress(db) == 1


def test_detach_checkpoint_skips_linked_database(project: Path, outside: Path) -> None:
    import detach

    db = outside / "other.db"
    keeper = sqlite3.connect(db)  # keeps the outside WAL from being checkpointed on close
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("CREATE TABLE t (x INTEGER)")
        keeper.execute("INSERT INTO t VALUES (1)")
        keeper.commit()
        wal_size = (outside / "other.db-wal").stat().st_size
        (project / "features.db").symlink_to(db)

        detach._checkpoint_databases(project)

        assert wal_size > 0 and (outside / "other.db-wal").stat().st_size == wal_size
    finally:
        keeper.close()


def test_wal_probe_does_not_follow_planted_probe_link(project: Path, outside: Path) -> None:
    import os

    probe = project / ".autoforge" / f".wal-probe-{os.getpid()}.db"
    probe.symlink_to(outside / "created.db")  # dangling: SQLite would create it

    database._probe_wal_support(project / ".autoforge")

    assert list(outside.iterdir()) == []


def test_assistant_db_opens_for_a_legacy_project_without_autoforge_dir(tmp_path):
    """A re-registered legacy project (features.db at the root, no .autoforge/) must not 500 the assistant."""
    from server.services import assistant_database as adb

    project = tmp_path / "legacy"
    project.mkdir()
    (project / "features.db").write_bytes(b"")
    try:
        conversation = adb.create_conversation(project, "legacy")
        assert adb.get_conversation(project, conversation.id) is not None
    finally:
        adb.dispose_engine(project)
