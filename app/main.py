import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password
from app.db import SessionLocal, get_db
from app.models import Order, Payment, Product, ShippingTask, User
from app.shipping import run_worker
from app.schemas import (
    OrderCreate,
    OrderOut,
    PaymentOut,
    ProductCreate,
    ProductOut,
    ProductUpdate,
    ShippingTaskOut,
    StockAdjustment,
    UserCreate,
    UserOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = Event()  # threading.Event，跨线程的停止信号
    worker = None
    if os.getenv("SHIPPING_WORKER_ENABLED", "1") == "1":  # 开关，默认开
        poll_seconds = float(os.getenv("SHIPPING_POLL_SECONDS", "1"))
        if poll_seconds <= 0:
            raise ValueError(
                "SHIPPING_POLL_SECONDS must be positive"
            )  # fail-fast：启动就报错
        worker = Thread(
            target=run_worker,
            args=(SessionLocal, stop, poll_seconds),
            name="shipping-worker",
            daemon=True,
        )
        worker.start()
    try:
        yield
    finally:
        stop.set()  # 1. 发信号：请自行退出
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)  # 2. 最多等 5 秒
            if worker.is_alive():
                logging.getLogger(__name__).warning(
                    "发货后台尚未退出，未提交的任务将在重启后继续处理"  # 3. 不强杀
                )


app = FastAPI(
    title="电商学习平台 API", version="0.1.0", docs_url="/api-docs", lifespan=lifespan
)
WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/docs", include_in_schema=False)
def homepage() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health", tags=["系统"], summary="检查服务状态")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    tags=["用户"],
    summary="注册用户",
)
def create_user(body: UserCreate, db: Session = Depends(get_db)) -> User:
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from None
    db.refresh(user)
    return user


@app.get("/users/me", response_model=UserOut, tags=["用户"], summary="查看当前用户")
def me(user: User = Depends(get_current_user)) -> User:
    return user


@app.post(
    "/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    tags=["商品"],
    summary="添加商品",
)
def create_product(
    body: ProductCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Product:
    product = Product(**body.model_dump(), created_by=user.id)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.get(
    "/products", response_model=list[ProductOut], tags=["商品"], summary="查看商品列表"
)
def list_products(
    db: Session = Depends(get_db), _: User = Depends(get_current_user)
) -> list[Product]:
    return list(db.scalars(select(Product).order_by(Product.id)))


@app.get(
    "/products/{product_id}",
    response_model=ProductOut,
    tags=["商品"],
    summary="查看商品详情",
)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.patch(
    "/products/{product_id}",
    response_model=ProductOut,
    tags=["商品"],
    summary="修改商品信息或上下架",
)
def update_product(
    product_id: int,
    body: ProductUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Product:
    changes = body.model_dump(exclude_unset=True)
    if not changes or any(value is None for value in changes.values()):
        raise HTTPException(status_code=422, detail="Provide non-null fields to update")
    product = db.scalar(
        select(Product).where(Product.id == product_id).with_for_update()
    )
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    for key, value in changes.items():
        setattr(product, key, value)
    db.commit()
    db.refresh(product)
    return product


@app.post(
    "/products/{product_id}/stock-adjustments",
    response_model=ProductOut,
    tags=["商品"],
    summary="调整商品库存",
)
def adjust_stock(
    product_id: int,
    body: StockAdjustment,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Product:
    product = db.scalar(
        update(Product)
        .where(
            Product.id == product_id,
            Product.available_stock + body.delta >= 0,
        )
        .values(available_stock=Product.available_stock + body.delta)
        .returning(Product)
    )
    if product is None:
        if db.get(Product, product_id) is None:
            raise HTTPException(status_code=404, detail="Product not found")
        raise HTTPException(status_code=409, detail="Stock cannot be negative")
    db.commit()
    db.refresh(product)
    return product


def own_visible_order(db: Session, order_id: int, user_id: int) -> Order:
    order = db.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.user_id == user_id,
            Order.deleted_at.is_(None),
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.post(
    "/orders",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    tags=["订单"],
    summary="创建订单",
)
def create_order(
    body: OrderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Order:
    product = db.execute(
        update(Product)
        .where(
            Product.id == body.product_id,
            Product.is_active.is_(True),
            Product.available_stock >= body.quantity,
        )
        .values(available_stock=Product.available_stock - body.quantity)
        .returning(Product.id, Product.name, Product.price_cents)
    ).first()
    if product is None:
        if db.get(Product, body.product_id) is None:
            raise HTTPException(status_code=404, detail="Product not found")
        raise HTTPException(
            status_code=409, detail="Product unavailable or insufficient stock"
        )
    order = Order(
        user_id=user.id,
        product_id=product.id,
        product_name=product.name,
        quantity=body.quantity,
        unit_price_cents=product.price_cents,
        total_cents=product.price_cents * body.quantity,
        status="PENDING_PAYMENT",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@app.get(
    "/orders", response_model=list[OrderOut], tags=["订单"], summary="查看我的订单"
)
def list_orders(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Order]:
    return list(
        db.scalars(
            select(Order)
            .where(Order.user_id == user.id, Order.deleted_at.is_(None))
            .order_by(Order.id.desc())
        )
    )


@app.get(
    "/orders/{order_id}", response_model=OrderOut, tags=["订单"], summary="查看订单详情"
)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Order:
    return own_visible_order(db, order_id, user.id)


@app.post(
    "/orders/{order_id}/cancel",
    response_model=OrderOut,
    tags=["订单"],
    summary="取消待付款订单",
)
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Order:
    reserved = db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.user_id == user.id,
            Order.deleted_at.is_(None),
            Order.status == "PENDING_PAYMENT",
        )
        .values(status="CANCELLED")
        .returning(Order.product_id, Order.quantity)
    ).first()
    if reserved is None:
        order = own_visible_order(db, order_id, user.id)
        raise HTTPException(
            status_code=409, detail=f"Cannot cancel {order.status} order"
        )
    db.execute(
        update(Product)
        .where(Product.id == reserved.product_id)
        .values(available_stock=Product.available_stock + reserved.quantity)
    )
    db.commit()
    return own_visible_order(db, order_id, user.id)


@app.post(
    "/orders/{order_id}/pay",
    response_model=PaymentOut,
    tags=["付款"],
    summary="模拟付款",
)
def pay_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Payment:
    amount = db.scalar(
        update(Order)
        .where(
            Order.id == order_id,
            Order.user_id == user.id,
            Order.deleted_at.is_(None),
            Order.status == "PENDING_PAYMENT",
        )
        .values(status="PAID")
        .returning(Order.total_cents)
    )
    if amount is None:
        order = own_visible_order(db, order_id, user.id)
        raise HTTPException(status_code=409, detail=f"Cannot pay {order.status} order")
    payment = Payment(order_id=order_id, payment_no=uuid4().hex, amount_cents=amount)
    db.add(payment)
    db.add(ShippingTask(order_id=order_id))
    # Payment, PAID status and the durable job either all commit or all roll back.
    db.commit()
    db.refresh(payment)
    return payment


@app.get(
    "/shipping-tasks",
    response_model=list[ShippingTaskOut],
    tags=["发货"],
    summary="查看我的发货任务（包含已隐藏订单）",
)
def list_shipping_tasks(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ShippingTask]:
    return list(
        db.scalars(
            select(ShippingTask)
            .join(Order, Order.id == ShippingTask.order_id)
            .where(Order.user_id == user.id)
            .order_by((ShippingTask.status == "FAILED").desc(), ShippingTask.id.desc())
        )
    )


def own_shipping_task(
    db: Session, task_id: int, user_id: int, *, lock: bool = False
) -> ShippingTask:
    query = (
        select(ShippingTask)
        .join(Order, Order.id == ShippingTask.order_id)
        .where(ShippingTask.id == task_id, Order.user_id == user_id)
    )
    if lock:
        query = query.with_for_update(of=ShippingTask)
    task = db.scalar(query)
    if task is None:
        raise HTTPException(status_code=404, detail="Shipping task not found")
    return task


@app.get(
    "/shipping-tasks/{task_id}",
    response_model=ShippingTaskOut,
    tags=["发货"],
    summary="查看发货任务详情",
)
def get_shipping_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ShippingTask:
    return own_shipping_task(db, task_id, user.id)


@app.post(
    "/shipping-tasks/{task_id}/retry",
    response_model=ShippingTaskOut,
    tags=["发货"],
    summary="重新安排失败的发货任务",
)
def retry_shipping_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ShippingTask:
    task = own_shipping_task(db, task_id, user.id, lock=True)
    if not (
        task.status == "FAILED"
        or (task.status == "PENDING" and task.last_error is not None)
    ):
        raise HTTPException(
            status_code=409, detail="Only failed shipping tasks can be retried"
        )
    task.status = "PENDING"
    task.attempt_count = 0
    task.last_error = None
    task.next_retry_at = db.scalar(select(func.clock_timestamp()))
    task.updated_at = task.next_retry_at
    db.commit()
    db.refresh(task)
    logging.getLogger(__name__).warning(
        "人工重新安排发货 user_id=%s order_id=%s",
        user.id,
        task.order_id,
    )
    return task


@app.delete(
    "/orders/{order_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["订单"],
    summary="删除自己的订单",
)
def delete_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    now = datetime.now(timezone.utc)
    reserved = db.execute(
        update(Order)
        .where(
            Order.id == order_id,
            Order.user_id == user.id,
            Order.deleted_at.is_(None),
            Order.status == "PENDING_PAYMENT",
        )
        .values(status="CANCELLED", deleted_at=now)
        .returning(Order.product_id, Order.quantity)
    ).first()
    if reserved is not None:
        db.execute(
            update(Product)
            .where(Product.id == reserved.product_id)
            .values(available_stock=Product.available_stock + reserved.quantity)
        )
    else:
        deleted_id = db.scalar(
            update(Order)
            .where(
                Order.id == order_id,
                Order.user_id == user.id,
                Order.deleted_at.is_(None),
                Order.status.in_(["PAID", "CANCELLED"]),
            )
            .values(deleted_at=now)
            .returning(Order.id)
        )
        if deleted_id is None:
            raise HTTPException(status_code=404, detail="Order not found")
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
