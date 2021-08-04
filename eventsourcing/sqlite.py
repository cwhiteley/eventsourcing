import sqlite3
import threading
from distutils.util import strtobool
from sqlite3 import Connection, Cursor
from types import TracebackType
from typing import Any, Dict, List, Mapping, Optional, Type
from uuid import UUID

from eventsourcing.persistence import (
    AggregateRecorder,
    ApplicationRecorder,
    DatabaseError,
    DataError,
    InfrastructureFactory,
    IntegrityError,
    InterfaceError,
    InternalError,
    Notification,
    NotSupportedError,
    OperationalError,
    PersistenceError,
    ProcessRecorder,
    ProgrammingError,
    StoredEvent,
    Tracking,
)


class Transaction:
    def __init__(self, connection: Connection):
        self.connection = connection

    def __enter__(self) -> Cursor:
        # We must issue a "BEGIN" explicitly
        # when running in auto-commit mode.
        self.connection.execute("BEGIN")
        self.cursor = self.connection.cursor()
        return self.cursor

    def __exit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> None:
        try:
            self.cursor.close()
            if exc_val:
                # Roll back all changes
                # if an exception occurs.
                self.connection.rollback()
                raise exc_val
            else:
                self.connection.commit()
        except sqlite3.InterfaceError as e:
            raise InterfaceError(e)
        except sqlite3.DataError as e:
            raise DataError(e)
        except sqlite3.OperationalError as e:
            raise OperationalError(e)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(e)
        except sqlite3.InternalError as e:
            raise InternalError(e)
        except sqlite3.ProgrammingError as e:
            raise ProgrammingError(e)
        except sqlite3.NotSupportedError as e:
            raise NotSupportedError(e)
        except sqlite3.DatabaseError as e:
            raise DatabaseError(e)
        except sqlite3.Error as e:
            raise PersistenceError(e)


class SQLiteDatastore:
    def __init__(self, db_name: str):
        self.db_name = db_name
        self.connections: Dict[int, Connection] = {}
        self.is_journal_mode_wal = False
        self.journal_mode_was_changed_to_wal = False

    def transaction(self) -> Transaction:
        thread_id = threading.get_ident()
        try:
            c = self.connections[thread_id]
        except KeyError:
            c = self.create_connection()
            self.connections[thread_id] = c
        return Transaction(c)

    def create_connection(self) -> Connection:
        # Make a connection to an SQLite database.
        try:
            c = sqlite3.connect(
                database=self.db_name,
                uri=True,
                check_same_thread=False,
                isolation_level=None,  # Auto-commit mode.
                cached_statements=True,
                # timeout=15,
            )
        except (sqlite3.Error, TypeError) as e:
            raise InterfaceError(e)

        # Use WAL (write-ahead log) mode if file-based database.
        if ":memory:" not in self.db_name and "mode=memory" not in self.db_name:
            if not self.is_journal_mode_wal:
                cursor = c.cursor()
                cursor.execute("PRAGMA journal_mode;")
                mode = cursor.fetchone()[0]
                if mode.lower() == "wal":
                    self.is_journal_mode_wal = True
                else:
                    cursor.execute("PRAGMA journal_mode=WAL;")
                    self.is_journal_mode_wal = True
                    self.journal_mode_was_changed_to_wal = True

        # Set the row factory.
        c.row_factory = sqlite3.Row

        # Return the connection.
        return c

    def close_connection(self) -> None:
        thread_id = threading.get_ident()
        try:
            c = self.connections.pop(thread_id)
        except KeyError:
            pass
        else:
            c.close()

    def close_all_connections(self) -> None:
        for c in self.connections.values():
            c.close()
        self.connections.clear()

    def __del__(self) -> None:
        self.close_all_connections()


class SQLiteAggregateRecorder(AggregateRecorder):
    def __init__(
        self,
        datastore: SQLiteDatastore,
        events_table_name: str = "stored_events",
    ):
        assert isinstance(datastore, SQLiteDatastore)
        self.datastore = datastore
        self.events_table_name = events_table_name
        self.create_table_statements = self.construct_create_table_statements()
        self.insert_events_statement = (
            f"INSERT INTO {self.events_table_name} VALUES (?,?,?,?)"
        )
        # noinspection SqlResolve
        self.select_events_statement = (
            "SELECT * " f"FROM {self.events_table_name} " "WHERE originator_id=? "
        )

    def construct_create_table_statements(self) -> List[str]:
        statement = (
            "CREATE TABLE IF NOT EXISTS "
            f"{self.events_table_name} ("
            "originator_id TEXT, "
            "originator_version INTEGER, "
            "topic TEXT, "
            "state BLOB, "
            "PRIMARY KEY "
            "(originator_id, originator_version)) "
            "WITHOUT ROWID"
        )
        return [statement]

    def create_table(self) -> None:
        with self.datastore.transaction() as c:
            for statement in self.create_table_statements:
                c.execute(statement)

    def insert_events(self, stored_events: List[StoredEvent], **kwargs: Any) -> None:
        with self.datastore.transaction() as c:
            self._insert_events(c, stored_events, **kwargs)

    def _insert_events(
        self,
        c: Cursor,
        stored_events: List[StoredEvent],
        **kwargs: Any,
    ) -> None:
        params = []
        for stored_event in stored_events:
            params.append(
                (
                    stored_event.originator_id.hex,
                    stored_event.originator_version,
                    stored_event.topic,
                    stored_event.state,
                )
            )
        c.executemany(self.insert_events_statement, params)

    def select_events(
        self,
        originator_id: UUID,
        gt: Optional[int] = None,
        lte: Optional[int] = None,
        desc: bool = False,
        limit: Optional[int] = None,
    ) -> List[StoredEvent]:
        statement = self.select_events_statement
        params: List[Any] = [originator_id.hex]
        if gt is not None:
            statement += "AND originator_version>? "
            params.append(gt)
        if lte is not None:
            statement += "AND originator_version<=? "
            params.append(lte)
        statement += "ORDER BY originator_version "
        if desc is False:
            statement += "ASC "
        else:
            statement += "DESC "
        if limit is not None:
            statement += "LIMIT ? "
            params.append(limit)
        stored_events = []
        with self.datastore.transaction() as c:
            c.execute(statement, params)
            for row in c.fetchall():
                stored_events.append(
                    StoredEvent(
                        originator_id=UUID(row["originator_id"]),
                        originator_version=row["originator_version"],
                        topic=row["topic"],
                        state=row["state"],
                    )
                )
        return stored_events


class SQLiteApplicationRecorder(
    SQLiteAggregateRecorder,
    ApplicationRecorder,
):
    def __init__(
        self,
        datastore: SQLiteDatastore,
        events_table_name: str = "stored_events",
    ):
        super().__init__(datastore, events_table_name)
        self.select_max_notification_id_statement = (
            f"SELECT MAX(rowid) FROM {self.events_table_name}"
        )
        self.select_notifications_statement = (
            f"SELECT rowid, * FROM {self.events_table_name} "
            "WHERE rowid>=? ORDER BY rowid LIMIT ?"
        )

    def construct_create_table_statements(self) -> List[str]:
        statement = (
            "CREATE TABLE IF NOT EXISTS "
            f"{self.events_table_name} ("
            "originator_id TEXT, "
            "originator_version INTEGER, "
            "topic TEXT, "
            "state BLOB, "
            "PRIMARY KEY "
            "(originator_id, originator_version))"
        )
        return [statement]

    def select_notifications(self, start: int, limit: int) -> List[Notification]:
        """
        Returns a list of event notifications
        from 'start', limited by 'limit'.
        """
        notifications = []
        with self.datastore.transaction() as c:
            c.execute(self.select_notifications_statement, [start, limit])
            for row in c.fetchall():
                notifications.append(
                    Notification(
                        id=row["rowid"],
                        originator_id=UUID(row["originator_id"]),
                        originator_version=row["originator_version"],
                        topic=row["topic"],
                        state=row["state"],
                    )
                )
        return notifications

    def max_notification_id(self) -> int:
        """
        Returns the maximum notification ID.
        """
        with self.datastore.transaction() as c:
            c.execute(self.select_max_notification_id_statement)
            max_id = c.fetchone()[0] or 0
        return max_id


class SQLiteProcessRecorder(
    SQLiteApplicationRecorder,
    ProcessRecorder,
):
    def __init__(
        self,
        datastore: SQLiteDatastore,
        events_table_name: str = "stored_events",
    ):
        super().__init__(datastore, events_table_name)
        # noinspection SqlResolve
        self.insert_tracking_statement = "INSERT INTO tracking VALUES (?,?)"
        self.select_max_tracking_id_statement = (
            "SELECT MAX(notification_id) FROM tracking WHERE application_name=?"
        )

    def construct_create_table_statements(self) -> List[str]:
        statements = super().construct_create_table_statements()
        statements.append(
            "CREATE TABLE IF NOT EXISTS tracking ("
            "application_name text, "
            "notification_id int, "
            "PRIMARY KEY "
            "(application_name, notification_id)) "
            "WITHOUT ROWID"
        )
        return statements

    def max_tracking_id(self, application_name: str) -> int:
        params = [application_name]
        with self.datastore.transaction() as c:
            c.execute(self.select_max_tracking_id_statement, params)
            max_id = c.fetchone()[0] or 0
        return max_id

    def _insert_events(
        self,
        c: Cursor,
        stored_events: List[StoredEvent],
        **kwargs: Any,
    ) -> None:
        super()._insert_events(c, stored_events, **kwargs)
        tracking: Optional[Tracking] = kwargs.get("tracking", None)
        if tracking is not None:
            c.execute(
                self.insert_tracking_statement,
                (
                    tracking.application_name,
                    tracking.notification_id,
                ),
            )


class Factory(InfrastructureFactory):
    SQLITE_DBNAME = "SQLITE_DBNAME"
    CREATE_TABLE = "CREATE_TABLE"

    def __init__(self, application_name: str, env: Mapping):
        super().__init__(application_name, env)
        db_name = self.getenv(self.SQLITE_DBNAME)
        if not db_name:
            raise EnvironmentError(
                "SQLite database name not found "
                "in environment with key "
                f"'{self.SQLITE_DBNAME}'"
            )
        self.datastore = SQLiteDatastore(db_name=db_name)

    def aggregate_recorder(self, purpose: str = "events") -> AggregateRecorder:
        events_table_name = "stored_" + purpose
        recorder = SQLiteAggregateRecorder(
            datastore=self.datastore,
            events_table_name=events_table_name,
        )
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def application_recorder(self) -> ApplicationRecorder:
        recorder = SQLiteApplicationRecorder(datastore=self.datastore)
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def process_recorder(self) -> ProcessRecorder:
        recorder = SQLiteProcessRecorder(datastore=self.datastore)
        if self.env_create_table():
            recorder.create_table()
        return recorder

    def env_create_table(self) -> bool:
        default = "yes"
        return bool(strtobool(self.getenv(self.CREATE_TABLE, default) or default))
