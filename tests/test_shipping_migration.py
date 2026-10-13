"""Exercise real PostgreSQL migrations in an isolated schema of a test database.

Run separately with TEST_DATABASE_URL pointing to a disposable *_test database:
    python -m pytest tests/test_shipping_migration.py
"""

import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


ROOT = Path(__file__).resolve().parents[1]
OLD_SHIPPED_AT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
TASK_SHIPPED_AT = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
FUTURE_RETRY_AT = datetime(2099, 1, 1, tzinfo=timezone.utc)


@dataclass
class MigrationDatabase:
    engine: Engine
    url: str

    def migrate(self, action: str, revision: str) -> None:
        environment = os.environ.copy()
        # Alembic's env.py reads app.db.DATABASE_URL. A subprocess avoids changing
        # the application's imported engine or any other test fixture's session.
        environment["DATABASE_URL"] = self.url
        environment["SHIPPING_WORKER_ENABLED"] = "0"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", action, revision],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def migration_database() -> Generator[MigrationDatabase, None, None]:
    raw_url = os.getenv("SHIPPING_MIGRATION_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL *_test database")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql" or not (url.database or "").endswith("_test"):
        pytest.fail("Migration tests require a PostgreSQL database name ending in _test")

    # A private schema keeps all migration tables (including alembic_version)
    # separate from public tables used by the API's existing client fixture.
    schema = "shipping_migration_" + uuid4().hex
    admin = create_engine(url)
    engine = None
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        existing_options = url.query.get("options", "")
        scoped_url = url.update_query_dict({
            "options": f"{existing_options} -csearch_path={schema}".strip(),
        })
        engine = create_engine(scoped_url)
        yield MigrationDatabase(engine, scoped_url.render_as_string(hide_password=False))
    finally:
        if engine is not None:
            engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def seed_legacy_shipping(engine: Engine) -> None:
    """Cover receipt recovery, all old states, hidden orders and missing jobs."""
    states = {
        1: "PENDING", 2: "PROCESSING", 3: "RETRY", 4: "MANUAL", 5: "SUCCEEDED",
        7: "PROCESSING", 8: "RETRY", 9: "MANUAL", 13: "PENDING", 14: "SUCCEEDED",
    }
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, username, password_hash) VALUES (1, 'migration-user', 'unused')"
        ))
        connection.execute(text(
            "INSERT INTO products (id, name, price_cents, available_stock, created_by) "
            "VALUES (1, 'migration-product', 100, 20, 1)"
        ))
        for order_id in range(1, 16):
            connection.execute(text(
                "INSERT INTO orders (id, user_id, product_id, product_name, quantity, "
                "unit_price_cents, total_cents, status, deleted_at) "
                "VALUES (:id, 1, 1, 'migration-product', 1, 100, 100, :status, :deleted_at)"
            ), {
                "id": order_id,
                # A receipt must be preserved regardless of current order status.
                "status": "CANCELLED" if order_id == 6 else "PENDING_PAYMENT" if order_id == 12 else "PAID",
                "deleted_at": OLD_SHIPPED_AT if order_id in (6, 10) else None,
            })
            if order_id not in (6, 11, 12):
                connection.execute(text(
                    "INSERT INTO payments (order_id, payment_no, amount_cents) "
                    "VALUES (:id, :payment_no, 100)"
                ), {"id": order_id, "payment_no": f"migration-payment-{order_id}"})

        for order_id, status in states.items():
            connection.execute(text(
                "INSERT INTO shipping_tasks (order_id, request_no, status, attempt_count, "
                "next_retry_at, lease_token, locked_until, last_error, tracking_no, shipped_at, "
                "simulation_mode, simulation_failures) "
                "VALUES (:id, :request_no, :status, :attempt_count, :retry_at, :lease_token, "
                ":locked_until, :last_error, :tracking_no, :shipped_at, 'BUG', 3)"
            ), {
                "id": order_id, "request_no": f"old-request-{order_id}", "status": status,
                "attempt_count": order_id,
                "retry_at": FUTURE_RETRY_AT,
                "lease_token": "old-worker-token" if status == "PROCESSING" else None,
                "locked_until": FUTURE_RETRY_AT if status == "PROCESSING" else None,
                "last_error": None if status == "SUCCEEDED" else f"old error {order_id}",
                "tracking_no": f"TASK-{order_id}" if status == "SUCCEEDED" else None,
                "shipped_at": TASK_SHIPPED_AT if status == "SUCCEEDED" else None,
            })
        for order_id in range(1, 7):
            connection.execute(text(
                "INSERT INTO warehouse_shipments (order_id, request_no, tracking_no, shipped_at) "
                "VALUES (:id, :request_no, :tracking_no, :shipped_at)"
            ), {
                "id": order_id, "request_no": f"old-request-{order_id}",
                "tracking_no": f"WAREHOUSE-{order_id}", "shipped_at": OLD_SHIPPED_AT,
            })


def task_rows(engine: Engine) -> dict[int, dict]:
    with engine.connect() as connection:
        return {
            row["order_id"]: dict(row)
            for row in connection.execute(text("SELECT * FROM shipping_tasks ORDER BY order_id")).mappings()
        }


def assert_rejected(engine: Engine, statement: str) -> None:
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text(statement))


def test_upgrade_recovers_receipts_and_legacy_tasks(migration_database: MigrationDatabase) -> None:
    database = migration_database
    database.migrate("upgrade", "0002_shipping")
    seed_legacy_shipping(database.engine)
    with database.engine.connect() as connection:
        before = connection.scalar(text("SELECT now()"))
    database.migrate("upgrade", "head")
    with database.engine.connect() as connection:
        after = connection.scalar(text("SELECT now()"))

    rows = task_rows(database.engine)
    assert set(rows) == set(range(1, 16)) - {11, 12}
    for order_id in (1, 2, 3, 4, 6):
        assert rows[order_id]["status"] == "SUCCEEDED"
        assert rows[order_id]["tracking_no"] == f"WAREHOUSE-{order_id}"
        assert rows[order_id]["shipped_at"] == OLD_SHIPPED_AT
        assert rows[order_id]["last_error"] is None
    for order_id in (5, 14):
        assert rows[order_id]["status"] == "SUCCEEDED"
        assert rows[order_id]["tracking_no"] == f"TASK-{order_id}"
        assert rows[order_id]["shipped_at"] == TASK_SHIPPED_AT
    for order_id in (7, 8, 10, 15):
        assert rows[order_id]["status"] == "PENDING"
        assert before <= rows[order_id]["next_retry_at"] <= after
    assert rows[9]["status"] == "FAILED"
    assert rows[9]["last_error"] == "old error 9"
    assert rows[13]["status"] == "PENDING"
    assert rows[13]["next_retry_at"] == FUTURE_RETRY_AT
    for order_id in (1, 2, 3, 4, 5, 7, 8, 9, 13, 14):
        assert rows[order_id]["attempt_count"] == order_id

    inspector = inspect(database.engine)
    assert "warehouse_shipments" not in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("shipping_tasks")} == {
        "id", "order_id", "status", "attempt_count", "next_retry_at", "last_error",
        "tracking_no", "shipped_at", "created_at", "updated_at",
    }
    assert {check["name"] for check in inspector.get_check_constraints("shipping_tasks")} == {
        "shipping_valid_status", "shipping_attempts_nonnegative", "shipping_success_has_receipt",
    }
    indexes = {index["name"] for index in inspector.get_indexes("shipping_tasks")}
    assert "ix_shipping_tasks_due" in indexes
    assert "ix_shipping_tasks_lease" not in indexes
    assert_rejected(database.engine, "UPDATE shipping_tasks SET status = 'PROCESSING' WHERE order_id = 7")
    assert_rejected(database.engine, "UPDATE shipping_tasks SET attempt_count = -1 WHERE order_id = 7")
    assert_rejected(database.engine, "UPDATE shipping_tasks SET status = 'SUCCEEDED' WHERE order_id = 7")
    assert_rejected(database.engine, "INSERT INTO shipping_tasks (order_id) VALUES (7)")


def test_downgrade_restores_old_schema_and_success_receipts(migration_database: MigrationDatabase) -> None:
    database = migration_database
    database.migrate("upgrade", "0002_shipping")
    seed_legacy_shipping(database.engine)
    database.migrate("upgrade", "head")
    simplified_rows = task_rows(database.engine)
    database.migrate("downgrade", "0002_shipping")

    rows = task_rows(database.engine)
    assert rows[9]["status"] == "MANUAL"
    for order_id, row in rows.items():
        assert row["request_no"] == f"ship-order-{order_id}"
        assert row["lease_token"] is None and row["locked_until"] is None
        assert row["simulation_mode"] == "NORMAL"
        assert row["simulation_failures"] == 2
    with database.engine.connect() as connection:
        receipts = {
            row["order_id"]: dict(row)
            for row in connection.execute(text("SELECT * FROM warehouse_shipments")).mappings()
        }
    succeeded_ids = {order_id for order_id, row in simplified_rows.items() if row["status"] == "SUCCEEDED"}
    assert set(receipts) == succeeded_ids
    for order_id in succeeded_ids:
        assert receipts[order_id]["tracking_no"] == simplified_rows[order_id]["tracking_no"]
        assert receipts[order_id]["shipped_at"] == simplified_rows[order_id]["shipped_at"]

    inspector = inspect(database.engine)
    assert "ix_shipping_tasks_lease" in {index["name"] for index in inspector.get_indexes("shipping_tasks")}
    assert_rejected(database.engine, "UPDATE shipping_tasks SET status = 'PROCESSING' WHERE order_id = 7")
    assert_rejected(database.engine, "UPDATE shipping_tasks SET status = 'FAILED' WHERE order_id = 7")
    assert_rejected(database.engine, "UPDATE shipping_tasks SET simulation_mode = 'UNKNOWN' WHERE order_id = 7")
    assert_rejected(database.engine, "UPDATE shipping_tasks SET simulation_failures = 21 WHERE order_id = 7")

    database.migrate("upgrade", "head")
    round_trip = task_rows(database.engine)
    for order_id, row in simplified_rows.items():
        for field in ("status", "attempt_count", "tracking_no", "shipped_at"):
            assert round_trip[order_id][field] == row[field]


def test_fresh_install_and_full_downgrade(migration_database: MigrationDatabase) -> None:
    database = migration_database
    database.migrate("upgrade", "head")
    assert "warehouse_shipments" not in inspect(database.engine).get_table_names()
    assert task_rows(database.engine) == {}
    database.migrate("downgrade", "base")
    assert inspect(database.engine).get_table_names() == ["alembic_version"]
