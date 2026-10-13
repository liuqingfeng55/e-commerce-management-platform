from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price_cents: int = Field(ge=0)
    available_stock: int = Field(ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    price_cents: int | None = Field(default=None, ge=0)
    is_active: bool | None = None


class StockAdjustment(BaseModel):
    delta: int

    @field_validator("delta")
    @classmethod
    def nonzero_delta(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta must not be zero")
        return value


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    price_cents: int
    available_stock: int
    is_active: bool
    created_by: int


class OrderCreate(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    product_id: int
    product_name: str
    quantity: int
    unit_price_cents: int
    total_cents: int
    status: str
    created_at: datetime


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: int
    payment_no: str
    amount_cents: int
    paid_at: datetime


class ShippingTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: int
    status: str
    attempt_count: int
    next_retry_at: datetime
    last_error: str | None
    tracking_no: str | None
    shipped_at: datetime | None
    created_at: datetime
    updated_at: datetime
