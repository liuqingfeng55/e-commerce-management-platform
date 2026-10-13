"""Durable tasks for local simulated shipping, completed in one transaction."""

import logging
from datetime import timedelta
from threading import Event

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Order, ShippingTask

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
RETRY_SECONDS = 10


def simulate_shipping(order_id: int) -> str:
    """Generate a local receipt; this function must not call an external warehouse."""
    return f"SIM-{order_id:010d}"


def process_one(sessions: sessionmaker[Session]) -> bool:
    error_text = None
    with sessions.begin() as db:
        now = db.scalar(select(func.clock_timestamp()))
        task = db.scalar(
            select(ShippingTask)
            .join(Order, Order.id == ShippingTask.order_id)
            .where(
                Order.status == "PAID",
                ShippingTask.status == "PENDING",
                ShippingTask.next_retry_at <= now,
            )
            .order_by(ShippingTask.next_retry_at, ShippingTask.id)
            .with_for_update(of=ShippingTask, skip_locked=True)
            .limit(1)
        )
        if task is None:
            return False

        task.attempt_count += 1
        task.updated_at = now
        try:
            tracking_no = simulate_shipping(task.order_id)
        except Exception as error:
            failed = task.attempt_count >= MAX_ATTEMPTS
            task.status = "FAILED" if failed else "PENDING"
            task.next_retry_at = now + timedelta(seconds=RETRY_SECONDS)
            task.last_error = error_text = f"{type(error).__name__}: {error}"[:2000]
            order_id, attempt_count = task.order_id, task.attempt_count
        else:
            task.status = "SUCCEEDED"
            task.tracking_no = tracking_no
            task.shipped_at = now
            task.last_error = None
        # Hold the row lock until the receipt and final status commit together.
        # A crash or database error rolls back, leaving the task pending.

    if error_text is not None:
        logger.log(
            logging.ERROR if failed else logging.WARNING,
            "发货%s order_id=%s attempt=%s error=%s",
            "失败，需人工重试" if failed else "失败，等待自动重试",
            order_id,
            attempt_count,
            error_text,
        )
    return True


def run_worker(sessions: sessionmaker[Session], stop: Event, poll_seconds: float = 1) -> None:
    while not stop.is_set():
        try:
            for _ in range(20):
                if stop.is_set() or not process_one(sessions):
                    break
        except Exception:
            logger.exception("发货后台执行异常，未提交的任务稍后继续处理")
        stop.wait(poll_seconds)
