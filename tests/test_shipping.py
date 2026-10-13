from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.orm import sessionmaker

from app import shipping
from app.models import Order, Payment, ShippingTask
from app.shipping import MAX_ATTEMPTS, process_one, run_worker
from test_shop import product, register


def paid_task(client: TestClient) -> tuple[tuple[str, str], int, int]:
    auth = register(client, "alice")
    product_id = product(client, auth)
    order = client.post(
        "/orders", auth=auth, json={"product_id": product_id, "quantity": 1}
    ).json()
    response = client.post(f"/orders/{order['id']}/pay", auth=auth)
    assert response.status_code == 200, response.text
    task = client.get("/shipping-tasks", auth=auth).json()[0]
    return auth, order["id"], task["id"]


def ready_again(client: TestClient, task_id: int) -> None:
    with client.shipping_sessions.begin() as db:
        db.execute(update(ShippingTask).where(ShippingTask.id == task_id).values(
            next_retry_at=func.clock_timestamp() - timedelta(seconds=1)
        ))


def test_payment_and_task_are_atomic(client: TestClient) -> None:
    auth = register(client, "alice")
    product_id = product(client, auth)
    order_id = client.post(
        "/orders", auth=auth, json={"product_id": product_id, "quantity": 1}
    ).json()["id"]

    def fail_insert(*args):
        raise RuntimeError("job storage unavailable")

    event.listen(ShippingTask, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="job storage unavailable"):
            client.post(f"/orders/{order_id}/pay", auth=auth)
    finally:
        event.remove(ShippingTask, "before_insert", fail_insert)
    with client.shipping_sessions() as db:
        assert db.get(Order, order_id).status == "PENDING_PAYMENT"
        assert db.scalar(select(func.count()).select_from(Payment)) == 0
        assert db.scalar(select(func.count()).select_from(ShippingTask)) == 0
    assert client.post(f"/orders/{order_id}/pay", auth=auth).status_code == 200
    assert len(client.get("/shipping-tasks", auth=auth).json()) == 1


def test_normal_shipping_and_ownership(client: TestClient) -> None:
    auth, order_id, task_id = paid_task(client)
    bob = register(client, "bob")
    assert client.get("/shipping-tasks", auth=bob).json() == []
    assert client.get(f"/shipping-tasks/{task_id}", auth=bob).status_code == 404
    assert client.post(f"/shipping-tasks/{task_id}/retry", auth=bob).status_code == 404
    assert client.post(f"/shipping-tasks/{task_id}/retry").status_code == 401
    assert client.post(f"/orders/{order_id}/pay", auth=auth).status_code == 409
    assert client.post(f"/shipping-tasks/{task_id}/retry", auth=auth).status_code == 409

    assert process_one(client.shipping_sessions)
    task = client.get(f"/shipping-tasks/{task_id}", auth=auth).json()
    assert task["status"] == "SUCCEEDED"
    assert task["attempt_count"] == 1
    assert task["tracking_no"] == f"SIM-{order_id:010d}"
    assert task["shipped_at"] is not None
    assert not process_one(client.shipping_sessions)
    assert client.post(f"/shipping-tasks/{task_id}/retry", auth=auth).status_code == 409
    assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json() == task


def test_transient_failures_wait_then_recover(client: TestClient, monkeypatch) -> None:
    auth, _, task_id = paid_task(client)
    original = shipping.simulate_shipping
    calls = 0

    def temporarily_unavailable(order_id):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("simulated temporary failure")
        return original(order_id)

    monkeypatch.setattr(shipping, "simulate_shipping", temporarily_unavailable)
    for attempt in (1, 2):
        assert process_one(client.shipping_sessions)
        task = client.get(f"/shipping-tasks/{task_id}", auth=auth).json()
        assert (task["status"], task["attempt_count"]) == ("PENDING", attempt)
        assert "ConnectionError" in task["last_error"]
        assert task["tracking_no"] is None
        assert not process_one(client.shipping_sessions)
        ready_again(client, task_id)
    assert process_one(client.shipping_sessions)
    task = client.get(f"/shipping-tasks/{task_id}", auth=auth).json()
    assert (task["status"], task["attempt_count"], task["last_error"]) == ("SUCCEEDED", 3, None)


def test_exhausted_failures_can_be_manually_retried(client: TestClient, monkeypatch, caplog) -> None:
    auth, _, task_id = paid_task(client)
    original = shipping.simulate_shipping

    def unavailable(order_id):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(shipping, "simulate_shipping", unavailable)
    for _ in range(MAX_ATTEMPTS):
        assert process_one(client.shipping_sessions)
        ready_again(client, task_id)
    task = client.get(f"/shipping-tasks/{task_id}", auth=auth).json()
    assert (task["status"], task["attempt_count"]) == ("FAILED", MAX_ATTEMPTS)
    assert "需人工重试" in caplog.text
    assert not process_one(client.shipping_sessions)

    monkeypatch.setattr(shipping, "simulate_shipping", original)
    response = client.post(f"/shipping-tasks/{task_id}/retry", auth=auth)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == task_id
    assert response.json()["attempt_count"] == 0
    assert response.json()["last_error"] is None
    assert process_one(client.shipping_sessions)
    assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json()["status"] == "SUCCEEDED"
    assert len(client.get("/shipping-tasks", auth=auth).json()) == 1


def test_manual_retry_can_advance_a_failed_pending_task(client: TestClient, monkeypatch) -> None:
    auth, _, task_id = paid_task(client)
    original = shipping.simulate_shipping

    def unavailable(order_id):
        raise ConnectionError("simulated temporary failure")

    monkeypatch.setattr(shipping, "simulate_shipping", unavailable)
    assert process_one(client.shipping_sessions)
    assert not process_one(client.shipping_sessions)
    response = client.post(f"/shipping-tasks/{task_id}/retry", auth=auth)
    assert response.status_code == 200
    assert response.json()["last_error"] is None
    # Repeated clicks cannot reset a fresh task's attempt counter.
    assert client.post(f"/shipping-tasks/{task_id}/retry", auth=auth).status_code == 409
    monkeypatch.setattr(shipping, "simulate_shipping", original)
    assert process_one(client.shipping_sessions)


def test_receipt_and_success_roll_back_together_and_restart_can_resume(client: TestClient) -> None:
    auth, _, task_id = paid_task(client)

    def interrupt_commit(db):
        db.flush()
        raise RuntimeError("simulated interruption before commit")

    event.listen(client.shipping_sessions, "before_commit", interrupt_commit)
    try:
        with pytest.raises(RuntimeError, match="interruption before commit"):
            process_one(client.shipping_sessions)
    finally:
        event.remove(client.shipping_sessions, "before_commit", interrupt_commit)
    task = client.get(f"/shipping-tasks/{task_id}", auth=auth).json()
    assert (task["status"], task["attempt_count"]) == ("PENDING", 0)
    assert (task["tracking_no"], task["shipped_at"]) == (None, None)

    restarted_sessions = sessionmaker(bind=client.shipping_sessions.kw["bind"], expire_on_commit=False)
    assert process_one(restarted_sessions)
    assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json()["status"] == "SUCCEEDED"


def test_concurrent_workers_do_not_ship_a_locked_task_twice(client: TestClient, monkeypatch) -> None:
    auth, _, task_id = paid_task(client)
    original = shipping.simulate_shipping
    entered, release = Event(), Event()
    calls = []

    def paused_simulation(order_id):
        calls.append(order_id)
        entered.set()
        assert release.wait(5), "test did not release the worker"
        return original(order_id)

    monkeypatch.setattr(shipping, "simulate_shipping", paused_simulation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(process_one, client.shipping_sessions)
        try:
            assert entered.wait(3), "worker did not enter the shipping transaction"
            second = pool.submit(process_one, client.shipping_sessions)
            assert second.result(timeout=2) is False
            assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json()["status"] == "PENDING"
        finally:
            release.set()
        assert first.result(timeout=2)
    assert len(calls) == 1
    assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json()["status"] == "SUCCEEDED"


def test_hidden_paid_order_still_ships_and_remains_manageable(client: TestClient) -> None:
    auth, order_id, task_id = paid_task(client)
    assert client.delete(f"/orders/{order_id}", auth=auth).status_code == 204
    assert client.get("/orders", auth=auth).json() == []
    assert len(client.get("/shipping-tasks", auth=auth).json()) == 1
    assert process_one(client.shipping_sessions)
    assert client.get(f"/shipping-tasks/{task_id}", auth=auth).json()["status"] == "SUCCEEDED"


def test_worker_survives_database_failure_and_stops(client: TestClient, monkeypatch, caplog) -> None:
    _, _, task_id = paid_task(client)
    original = shipping.process_one
    calls = 0

    def temporarily_unavailable(sessions):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated database outage")
        return original(sessions)

    monkeypatch.setattr(shipping, "process_one", temporarily_unavailable)
    stop = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_worker, client.shipping_sessions, stop, 0.01)
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with client.shipping_sessions() as db:
                    if db.get(ShippingTask, task_id).status == "SUCCEEDED":
                        break
                sleep(0.02)
            else:
                pytest.fail("worker did not recover")
        finally:
            stop.set()
        future.result(timeout=2)
    assert calls >= 2
    assert "发货后台执行异常" in caplog.text


def test_app_lifespan_runs_shipping_worker(client: TestClient, monkeypatch) -> None:
    from app import main

    _, _, task_id = paid_task(client)
    monkeypatch.setattr(main, "SessionLocal", client.shipping_sessions)
    monkeypatch.setenv("SHIPPING_WORKER_ENABLED", "1")
    monkeypatch.setenv("SHIPPING_POLL_SECONDS", "0.01")
    with TestClient(main.app):
        deadline = monotonic() + 5
        while monotonic() < deadline:
            with client.shipping_sessions() as db:
                if db.get(ShippingTask, task_id).status == "SUCCEEDED":
                    break
            sleep(0.02)
        else:
            pytest.fail("application lifespan did not start the worker")
