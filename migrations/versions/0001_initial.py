"""Create users, products, orders and payments.

Revision ID: 0001_initial
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("available_stock", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.CheckConstraint("price_cents >= 0", name="product_price_nonnegative"),
        sa.CheckConstraint("available_stock >= 0", name="product_stock_nonnegative"),
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("product_name", sa.String(120), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("quantity > 0", name="order_quantity_positive"),
        sa.CheckConstraint("unit_price_cents >= 0", name="order_price_nonnegative"),
        sa.CheckConstraint("total_cents >= 0", name="order_total_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'PAID', 'CANCELLED')",
            name="order_valid_status",
        ),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("payment_no", sa.String(32), nullable=False, unique=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="payment_one_per_order"),
    )


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_table("orders")
    op.drop_table("products")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")

