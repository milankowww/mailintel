from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .orm import baseline_statements

try:
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local setup
    raise RuntimeError("psycopg is required. Install project dependencies and run a local PostgreSQL server.") from exc


def connect_database(database_url: str):
    return psycopg.connect(database_url, row_factory=dict_row, autocommit=False)


def init_database(connection) -> None:
    with connection.cursor() as cursor:
        for statement in baseline_statements():
            cursor.execute(statement)
    connection.commit()


@contextmanager
def transaction(connection) -> Iterator[object]:
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
