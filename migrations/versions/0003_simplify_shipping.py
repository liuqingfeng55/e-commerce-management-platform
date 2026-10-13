"""Keep local shipping results and retries in one task table.

Revision ID: 0003_simplify_shipping
Revises: 0002_shipping
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_simplify_shipping"
down_revision = "0002_shipping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Stop the old worker before upgrading: its leased tasks become ordinary
    # pending tasks, and its independently committed receipts move here.
    op.drop_constraint("shipping_valid_lease", "shipping_tasks", type_="check")
    op.drop_constraint("shipping_valid_status", "shipping_tasks", type_="check")
    op.drop_constraint("shipping_valid_simulation", "shipping_tasks", type_="check")
    op.drop_constraint("shipping_valid_failures", "shipping_tasks", type_="check")
    op.drop_index("ix_shipping_tasks_lease", table_name="shipping_tasks")

    op.execute(
        "UPDATE shipping_tasks SET "
        "status = CASE WHEN status = 'MANUAL' THEN 'FAILED' ELSE 'PENDING' END, "
        "next_retry_at = now(), updated_at = now() "
        "WHERE status IN ('PROCESSING', 'RETRY', 'MANUAL')"
    )
    for column in (
        "request_no", "lease_token", "locked_until", "simulation_mode", "simulation_failures"
    ):
        op.drop_column("shipping_tasks", column)

    op.create_check_constraint(
        "shipping_valid_status", "shipping_tasks",
        "status IN ('PENDING', 'SUCCEEDED', 'FAILED')",
    )

    # A warehouse receipt proves that shipping already happened, including when
    # the old worker lost the response or its task row was subsequently lost.
    # Keep an already-successful task's own receipt if the two records differ.
    op.execute(
        "INSERT INTO shipping_tasks (order_id, status, tracking_no, shipped_at) "
        "SELECT order_id, 'SUCCEEDED', tracking_no, shipped_at FROM warehouse_shipments "
        "ON CONFLICT (order_id) DO UPDATE SET "
        "tracking_no = CASE WHEN shipping_tasks.status = 'SUCCEEDED' "
        "THEN shipping_tasks.tracking_no ELSE EXCLUDED.tracking_no END, "
        "shipped_at = CASE WHEN shipping_tasks.status = 'SUCCEEDED' "
        "THEN shipping_tasks.shipped_at ELSE EXCLUDED.shipped_at END, "
        "status = 'SUCCEEDED', last_error = NULL, updated_at = now()"
    )
    # This is a one-time repair of historical paid orders, including hidden ones.
    op.execute(
        "INSERT INTO shipping_tasks (order_id) "
        "SELECT o.id FROM orders o JOIN payments p ON p.order_id = o.id "
        "WHERE o.status = 'PAID' ON CONFLICT (order_id) DO NOTHING"
    )
    op.drop_table("warehouse_shipments")


def downgrade() -> None:
    op.drop_constraint("shipping_valid_status", "shipping_tasks", type_="check")
    op.add_column("shipping_tasks", sa.Column("request_no", sa.String(64), nullable=True))
    op.add_column("shipping_tasks", sa.Column("lease_token", sa.String(32), nullable=True))
    op.add_column(
        "shipping_tasks", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "shipping_tasks",
        sa.Column("simulation_mode", sa.String(20), server_default="NORMAL", nullable=False),
    )
    op.add_column(
        "shipping_tasks",
        sa.Column("simulation_failures", sa.Integer(), server_default="2", nullable=False),
    )
    op.execute(
        "UPDATE shipping_tasks SET request_no = 'ship-order-' || order_id::text, "
        "status = CASE WHEN status = 'FAILED' THEN 'MANUAL' ELSE status END"
    )
    op.alter_column("shipping_tasks", "request_no", existing_type=sa.String(64), nullable=False)
    op.create_unique_constraint("shipping_tasks_request_no_key", "shipping_tasks", ["request_no"])
    op.create_check_constraint(
        "shipping_valid_status", "shipping_tasks",
        "status IN ('PENDING', 'PROCESSING', 'RETRY', 'SUCCEEDED', 'MANUAL')",
    )
    op.create_check_constraint(
        "shipping_valid_simulation", "shipping_tasks",
        "simulation_mode IN ('NORMAL', 'TRANSIENT', 'BUG', 'PERMANENT', 'LOST_RESPONSE')",
    )
    op.create_check_constraint(
        "shipping_valid_failures", "shipping_tasks", "simulation_failures BETWEEN 0 AND 20",
    )
    op.create_check_constraint(
        "shipping_valid_lease", "shipping_tasks",
        "(status = 'PROCESSING' AND lease_token IS NOT NULL AND locked_until IS NOT NULL) "
        "OR (status <> 'PROCESSING' AND lease_token IS NULL AND locked_until IS NULL)",
    )
    op.create_index("ix_shipping_tasks_lease", "shipping_tasks", ["status", "locked_until"])
    op.create_table(
        "warehouse_shipments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("request_no", sa.String(64), nullable=False, unique=True),
        sa.Column("tracking_no", sa.String(64), nullable=False, unique=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="warehouse_one_shipment_per_order"),
    )
    # Removed lease/simulation history cannot be recovered. Successful receipts
    # can: reconstruct them so an old worker also sees that these orders shipped.
    op.execute(
        "INSERT INTO warehouse_shipments (order_id, request_no, tracking_no, shipped_at) "
        "SELECT order_id, request_no, tracking_no, shipped_at FROM shipping_tasks "
        "WHERE status = 'SUCCEEDED'"
    )
