from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="product_price_nonnegative"),
        CheckConstraint("available_stock >= 0", name="product_stock_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    price_cents: Mapped[int] = mapped_column(Integer)
    available_stock: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="order_quantity_positive"),
        CheckConstraint("unit_price_cents >= 0", name="order_price_nonnegative"),
        CheckConstraint("total_cents >= 0", name="order_total_nonnegative"),
        CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'PAID', 'CANCELLED')",
            name="order_valid_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="PENDING_PAYMENT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("order_id", name="payment_one_per_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    payment_no: Mapped[str] = mapped_column(String(32), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ShippingTask(Base):
    __tablename__ = "shipping_tasks"
    __table_args__ = (
        UniqueConstraint("order_id", name="shipping_one_task_per_order"),
        CheckConstraint(
            "status IN ('PENDING', 'SUCCEEDED', 'FAILED')",
            name="shipping_valid_status",
        ),
        CheckConstraint("attempt_count >= 0", name="shipping_attempts_nonnegative"),
        CheckConstraint(
            "status <> 'SUCCEEDED' OR (tracking_no IS NOT NULL AND shipped_at IS NOT NULL)",
            name="shipping_success_has_receipt",
        ),
        Index("ix_shipping_tasks_due", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    status: Mapped[str] = mapped_column(String(20), server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    tracking_no: Mapped[str | None] = mapped_column(String(64))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
