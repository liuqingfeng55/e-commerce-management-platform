"""Add durable shipping tasks and simulated warehouse receipts.

Revision ID: 0002_shipping
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_shipping"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shipping_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("request_no", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("tracking_no", sa.String(64), nullable=True),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("simulation_mode", sa.String(20), server_default="NORMAL", nullable=False),
        sa.Column("simulation_failures", sa.Integer(), server_default="2", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="shipping_one_task_per_order"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'RETRY', 'SUCCEEDED', 'MANUAL')",
            name="shipping_valid_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="shipping_attempts_nonnegative"),
        sa.CheckConstraint(
            "simulation_mode IN ('NORMAL', 'TRANSIENT', 'BUG', 'PERMANENT', 'LOST_RESPONSE')",
            name="shipping_valid_simulation",
        ),
        sa.CheckConstraint("simulation_failures BETWEEN 0 AND 20", name="shipping_valid_failures"),
        sa.CheckConstraint(
            "(status = 'PROCESSING' AND lease_token IS NOT NULL AND locked_until IS NOT NULL) "
            "OR (status <> 'PROCESSING' AND lease_token IS NULL AND locked_until IS NULL)",
            name="shipping_valid_lease",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR (tracking_no IS NOT NULL AND shipped_at IS NOT NULL)",
            name="shipping_success_has_receipt",
        ),
    )
    op.create_index("ix_shipping_tasks_due", "shipping_tasks", ["status", "next_retry_at"])
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
    # Existing PAID orders also need shipping, even if logically deleted.
    op.execute(
        "INSERT INTO shipping_tasks (order_id, request_no) "
        "SELECT o.id, 'ship-order-' || o.id::text FROM orders o "
        "JOIN payments p ON p.order_id = o.id WHERE o.status = 'PAID' "
        "ON CONFLICT (order_id) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("warehouse_shipments")
    op.drop_index("ix_shipping_tasks_lease", table_name="shipping_tasks")
    op.drop_index("ix_shipping_tasks_due", table_name="shipping_tasks")
    op.drop_table("shipping_tasks")
