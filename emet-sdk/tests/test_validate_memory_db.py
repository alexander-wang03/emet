"""What `validate_memory_db` reports, and that it leaves no connection open.

Memories belong to the soul and travel with the bundle, so a column that
scopes a soul table to one body is an error. That rule had no test until
0.5.6, and the function left its connection open: a `sqlite3.Connection`
used as a context manager ends a transaction and keeps the file open
(Python's sqlite3 documentation, "How to use the connection context
manager", read 2026-11-26).

Two things show an open connection here, and each test names which it uses:

- **The connection itself.** `opened` wraps `sqlite3.connect` and keeps every
  connection handed out. A closed one refuses a statement with "Cannot
  operate on a closed database", on every Python the project supports.
- **The garbage collector.** From Python 3.13 a connection deleted before
  `close()` emits `ResourceWarning: unclosed database` (the same page,
  "Changed in version 3.13"). The suites run with warnings as errors, so
  that warning fails whichever test is running when the collector gets to
  it. The last test here collects inside the test instead, so the failure
  lands on the function that caused it. On 3.11 and 3.12 sqlite3 never
  warns, so that test is skipped there, and the first kind of test is the
  one that can fail.
"""

from __future__ import annotations

import gc
import shutil
import sqlite3
import sys
import warnings
from contextlib import closing
from pathlib import Path

import pytest

from emet_sdk.cli import main
from emet_sdk.validate import validate_memory_db

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

#: The real function, kept before any test wraps it, for building databases.
_connect = sqlite3.connect

#: The soul's three tables and two indexes as 0.6 plans them: `DESIGN.md`
#: §9.1's statements (read 2026-11-26), their comments left out. No column
#: names a body.
SOUL_TABLES = (
    "CREATE TABLE people (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, pronouns TEXT,"
    " voiceprint BLOB, enrolled_at TEXT NOT NULL, last_seen_at TEXT, profile TEXT,"
    " is_owner INTEGER DEFAULT 0)",
    "CREATE TABLE memories (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,"
    " episode_id TEXT REFERENCES episodes(id), source_person_id TEXT REFERENCES people(id),"
    " subject_person_id TEXT REFERENCES people(id), content TEXT NOT NULL, kind TEXT NOT NULL,"
    " sensitivity INTEGER NOT NULL DEFAULT 1, confidence REAL DEFAULT 1.0,"
    " salience REAL DEFAULT 0.5, last_recalled_at TEXT, superseded_by TEXT REFERENCES memories(id))",
    "CREATE TABLE episodes (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,"
    " participants TEXT, summary TEXT, transcript TEXT)",
    "CREATE INDEX idx_mem_subject ON memories(subject_person_id, salience DESC)",
    "CREATE INDEX idx_mem_source ON memories(source_person_id)",
)

#: A soul file's text. SQLite refuses any non-empty file that lacks its
#: header, however short.
NOT_A_DATABASE = b"bundle_version: '0.1'\nidentity:\n  name: Emet\n"


def make_db(path: Path, *statements: str) -> Path:
    with closing(_connect(path)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    return path


def is_open(conn: sqlite3.Connection) -> bool:
    """Whether a connection still holds its database."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        if "closed" in str(exc):
            return False
        raise
    return True


@pytest.fixture
def opened(monkeypatch):
    """Every connection `sqlite3.connect` hands out while the test runs.

    Anything still open is closed at teardown, so a test that fails here
    leaves no unclosed database behind to fail a later test in its place.
    """
    connections: list[sqlite3.Connection] = []

    def connect(*args, **kwargs):
        conn = _connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield connections
    for conn in connections:
        conn.close()


# ------------------------------------------------------------ what it reports


def test_a_soul_scoped_database_passes_and_is_closed(tmp_path, opened):
    db = make_db(
        tmp_path / "memory.db",
        *SOUL_TABLES,
        # The check reads the soul's three tables only, so a body column in
        # another table passes it.
        "CREATE TABLE calibration (body_id TEXT, offset_deg REAL)",
    )

    report = validate_memory_db(db)

    assert report.ok
    assert report.findings == []
    assert len(opened) == 1
    assert not is_open(opened[0])


def test_an_empty_database_passes_and_is_closed(tmp_path, opened):
    """A zero-length file is a database with no tables, so nothing is wrong
    with it yet."""
    db = tmp_path / "memory.db"
    db.write_bytes(b"")

    report = validate_memory_db(db)

    assert report.findings == []
    assert len(opened) == 1
    assert not is_open(opened[0])


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("memories", "body_id"),
        ("people", "Chassis_Ref"),      # matched whatever its case, reported as written
        ("episodes", "manifest_id"),
    ],
)
def test_a_body_column_in_a_soul_table_is_an_error_and_the_connection_is_closed(tmp_path, opened, table, column):
    db = make_db(tmp_path / "memory.db", *SOUL_TABLES, f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    report = validate_memory_db(db)

    assert [(f.severity, f.code, f.path) for f in report.findings] == [
        ("error", "memory_body_column", f"{table}.{column}")
    ]
    assert not report.ok
    assert len(opened) == 1
    assert not is_open(opened[0])


def test_every_body_column_is_reported_in_one_pass(tmp_path, opened):
    db = make_db(
        tmp_path / "memory.db",
        "CREATE TABLE people (id INTEGER PRIMARY KEY, body TEXT)",
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, body_id TEXT, chassis TEXT)",
    )

    report = validate_memory_db(db)

    assert sorted(f.path for f in report.errors) == ["memories.body_id", "memories.chassis", "people.body"]
    assert not is_open(opened[0])


def test_a_missing_file_is_reported_and_nothing_is_opened(tmp_path, opened):
    report = validate_memory_db(tmp_path / "nowhere" / "memory.db")

    assert [(f.severity, f.code) for f in report.findings] == [("error", "missing_file")]
    assert "nowhere" in report.findings[0].message
    assert opened == []


def test_a_path_given_as_text_is_read_like_a_path(tmp_path, opened):
    db = make_db(tmp_path / "memory.db", *SOUL_TABLES)

    assert validate_memory_db(str(db)).findings == []
    assert not is_open(opened[0])


def test_a_file_that_is_no_database_raises_and_is_closed(tmp_path, opened):
    """SQLite opens any file and reads it at the first statement, so the
    connection exists by the time the error is raised: the path on which the
    old `with` block left it open."""
    path = tmp_path / "memory.db"
    path.write_bytes(NOT_A_DATABASE)

    with pytest.raises(sqlite3.DatabaseError, match="not a database"):
        validate_memory_db(path)

    assert len(opened) == 1
    assert not is_open(opened[0])


def test_nothing_is_written_to_the_database(tmp_path, opened):
    """The connection is read-only: validating a bundle must never change it."""
    db = make_db(tmp_path / "memory.db", *SOUL_TABLES)
    before = db.read_bytes()

    validate_memory_db(db)

    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["memory.db"]
    assert len(opened) == 1
    assert not is_open(opened[0])


# -------------------------------------------- a path that holds a URI character

#: Windows refuses a `?` in a name; Linux and macOS take one.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses ? in a name")

#: Folder names that mean something inside a `file:` URI. Before 0.5.6 the
#: path went into the URI as written: SQLite cut `hash#tag` at the `#`, so it
#: opened a new empty file named `hash`, read-write, and the validator passed
#: a database it had never read; it cut `query?x=1` at the `?` the same way;
#: and it decoded `%41` to `A`, a folder that is not there. The rest were
#: read correctly then and must be still.
URI_FOLDERS = [
    "hash#tag",
    pytest.param("query?x=1", marks=_POSIX_ONLY),
    "percent%41",
    "100%",
    "with space",
    "a&b=c;d",
    "café",
]


@pytest.mark.parametrize("folder", URI_FOLDERS)
def test_a_path_holding_a_uri_character_is_still_the_file_read(tmp_path, opened, folder):
    (tmp_path / folder).mkdir()
    db = make_db(tmp_path / folder / "memory.db", *SOUL_TABLES, "ALTER TABLE memories ADD COLUMN body_id TEXT")

    report = validate_memory_db(db)

    assert [f.path for f in report.errors] == ["memories.body_id"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [folder]      # no stray file beside it
    assert sorted(p.name for p in (tmp_path / folder).iterdir()) == ["memory.db"]
    assert len(opened) == 1
    assert not is_open(opened[0])


@pytest.mark.parametrize("folder", ["plain", "hash#tag", pytest.param("query?x=1", marks=_POSIX_ONLY)])
def test_the_connection_is_read_only_whatever_the_path_holds(tmp_path, monkeypatch, folder):
    """`mode=ro` is the end of the URI, so a path that cut the URI short
    would also drop it. Each connection is asked to write as it is handed
    out, and has to refuse."""
    (tmp_path / folder).mkdir()
    db = make_db(tmp_path / folder / "memory.db", *SOUL_TABLES)
    refused: list[str] = []
    connections: list[sqlite3.Connection] = []

    def connect(*args, **kwargs):
        conn = _connect(*args, **kwargs)
        connections.append(conn)
        try:
            conn.execute("CREATE TABLE written (x)")
        except sqlite3.OperationalError as exc:
            refused.append(str(exc))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        assert validate_memory_db(db).findings == []
        assert len(connections) == 1
        assert not is_open(connections[0]), "the validator closed it before these lines close it"
    finally:
        for conn in connections:
            conn.close()

    assert refused == ["attempt to write a readonly database"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows reads two leading slashes as a network share")
def test_a_posix_path_that_begins_with_two_slashes_is_still_the_file_read(tmp_path, opened):
    """POSIX keeps two leading slashes, and in `file://srv/...` SQLite takes
    `srv` for a host and refuses it. The URI gives the path an empty host."""
    db = make_db(tmp_path / "memory.db", *SOUL_TABLES, "ALTER TABLE memories ADD COLUMN body_id TEXT")
    doubled = Path("/" + str(db))
    assert str(doubled).startswith("//")

    report = validate_memory_db(doubled)

    assert [f.path for f in report.errors] == ["memories.body_id"]
    assert len(opened) == 1
    assert not is_open(opened[0])


def test_a_relative_path_is_read_from_the_working_directory(tmp_path, opened, monkeypatch):
    make_db(tmp_path / "memory.db", *SOUL_TABLES, "ALTER TABLE people ADD COLUMN chassis TEXT")
    monkeypatch.chdir(tmp_path)

    report = validate_memory_db("memory.db")

    assert [f.path for f in report.errors] == ["people.chassis"]
    assert not is_open(opened[0])


# ----------------------------------------------------------- through its caller


def test_emet_validate_passes_a_memory_database_and_closes_it(tmp_path, opened, capsys):
    db = make_db(tmp_path / "memory.db", *SOUL_TABLES)

    assert main(["validate", str(db)]) == 0

    out = capsys.readouterr().out
    assert "(memory)" in out and out.startswith("ok")
    assert len(opened) == 1
    assert not is_open(opened[0])


def test_emet_validate_reports_a_file_that_is_no_database_and_closes_it(tmp_path, opened, capsys):
    path = tmp_path / "memory.db"
    path.write_bytes(NOT_A_DATABASE)

    assert main(["validate", str(path)]) == 1

    out = capsys.readouterr().out
    assert "read_error" in out
    assert "DatabaseError: file is not a database" in out
    assert "Traceback" not in out
    assert len(opened) == 1
    assert not is_open(opened[0])


def test_emet_validate_reads_the_memory_database_inside_a_bundle(tmp_path, opened, capsys):
    bundle = tmp_path / "emet.emet"
    bundle.mkdir()
    shutil.copyfile(EXAMPLES / "emet-soul.yaml", bundle / "soul.yaml")
    make_db(bundle / "memory.db", *SOUL_TABLES, "ALTER TABLE memories ADD COLUMN body_id TEXT")

    assert main(["validate", str(bundle)]) == 1

    out = capsys.readouterr().out
    assert "(bundle)" in out
    assert "memory_body_column memories.body_id" in out
    assert len(opened) == 1
    assert not is_open(opened[0])


# ----------------------------------------------------------- the suite's filter


def test_the_suite_runs_with_warnings_as_errors(request):
    """Every warning is an error in this suite, so an unclosed connection
    that no test here checks still fails a test when the collector finds
    it. The `filterwarnings` line of `pyproject.toml` that makes it so is
    held here: `error` first, and after it only an `ignore` that names the
    start of a message, a category and a module. A blanket
    `ignore::DeprecationWarning`, a deleted line or a `-W` that loosens it
    fails here."""
    lines = request.config.getini("filterwarnings")
    assert lines[:1] == ["error"]
    for line in lines[1:]:
        action, message, category, module = (line.split(":") + [""] * 4)[:4]
        assert action == "ignore" and message and category and module, f"too broad: {line!r}"
    with pytest.raises(DeprecationWarning, match="canary"):
        warnings.warn("canary", DeprecationWarning)


# ------------------------------------------------- what the collector would say


def _validate_and_forget(path: Path) -> None:
    """Run the validator and drop everything it made, the error included: a
    live traceback keeps the function's frame, and the frame its connection."""
    try:
        validate_memory_db(path)
    except sqlite3.DatabaseError:
        pass


@pytest.mark.skipif(sys.version_info < (3, 13), reason="sqlite3 warns of an unclosed connection from 3.13")
@pytest.mark.parametrize("content", ["a database", "not a database"])
def test_the_garbage_collector_finds_no_unclosed_database(tmp_path, content):
    """What warnings as errors would catch, caught here: from Python 3.13 an
    unclosed connection warns when it is deleted, and this test deletes it
    inside its own `catch_warnings`."""
    path = tmp_path / "memory.db"
    if content == "a database":
        make_db(path, *SOUL_TABLES)
    else:
        path.write_bytes(NOT_A_DATABASE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _validate_and_forget(path)
        gc.collect()

    unclosed = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
    assert unclosed == []
