from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient


def register(client: TestClient, username: str) -> tuple[str, str]:
    auth = (username, "password123")
    response = client.post("/users", json={"username": username, "password": auth[1]})
    assert response.status_code == 201, response.text
    return auth


def product(client: TestClient, auth: tuple[str, str], stock: int = 3) -> int:
    response = client.post(
        "/products",
        auth=auth,
        json={"name": "Book", "price_cents": 1999, "available_stock": stock},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_order_lifecycle_and_ownership(client: TestClient) -> None:
    alice = register(client, "alice")
    bob = register(client, "bob")
    product_id = product(client, alice)

    order_response = client.post(
        "/orders", auth=alice, json={"product_id": product_id, "quantity": 2}
    )
    assert order_response.status_code == 201, order_response.text
    order = order_response.json()
    assert (order["status"], order["unit_price_cents"], order["total_cents"]) == (
        "PENDING_PAYMENT", 1999, 3998
    )
    assert client.get(f"/products/{product_id}", auth=alice).json()["available_stock"] == 1
    assert client.get(f"/orders/{order['id']}", auth=bob).status_code == 404
    assert client.post(f"/orders/{order['id']}/pay", auth=bob).status_code == 404
    assert client.post(f"/orders/{order['id']}/cancel", auth=bob).status_code == 404
    assert client.delete(f"/orders/{order['id']}", auth=bob).status_code == 404

    payment = client.post(f"/orders/{order['id']}/pay", auth=alice)
    assert payment.status_code == 200, payment.text
    assert payment.json()["amount_cents"] == 3998
    assert client.post(f"/orders/{order['id']}/pay", auth=alice).status_code == 409
    assert client.post(f"/orders/{order['id']}/cancel", auth=alice).status_code == 409
    assert client.delete(f"/orders/{order['id']}", auth=alice).status_code == 204
    assert client.get(f"/orders/{order['id']}", auth=alice).status_code == 404
    assert client.get(f"/products/{product_id}", auth=alice).json()["available_stock"] == 1


def test_cancel_and_delete_pending_return_stock_once(client: TestClient) -> None:
    alice = register(client, "alice")
    product_id = product(client, alice, stock=2)
    order_id = client.post(
        "/orders", auth=alice, json={"product_id": product_id, "quantity": 1}
    ).json()["id"]
    assert client.post(f"/orders/{order_id}/cancel", auth=alice).status_code == 200
    assert client.post(f"/orders/{order_id}/cancel", auth=alice).status_code == 409
    assert client.get(f"/products/{product_id}", auth=alice).json()["available_stock"] == 2

    order_id = client.post(
        "/orders", auth=alice, json={"product_id": product_id, "quantity": 2}
    ).json()["id"]
    assert client.delete(f"/orders/{order_id}", auth=alice).status_code == 204
    assert client.delete(f"/orders/{order_id}", auth=alice).status_code == 404
    assert client.get(f"/products/{product_id}", auth=alice).json()["available_stock"] == 2


def test_last_item_is_not_oversold(client: TestClient) -> None:
    alice = register(client, "alice")
    product_id = product(client, alice, stock=1)
    barrier = Barrier(2)

    def order() -> int:
        barrier.wait()
        return client.post(
            "/orders", auth=alice, json={"product_id": product_id, "quantity": 1}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: order(), range(2)))
    assert sorted(statuses) == [201, 409]
    assert client.get(f"/products/{product_id}", auth=alice).json()["available_stock"] == 0
    assert len(client.get("/orders", auth=alice).json()) == 1


def test_pay_and_cancel_cannot_both_succeed(client: TestClient) -> None:
    alice = register(client, "alice")
    product_id = product(client, alice, stock=1)
    order_id = client.post(
        "/orders", auth=alice, json={"product_id": product_id, "quantity": 1}
    ).json()["id"]
    barrier = Barrier(2)

    def act(action: str) -> int:
        barrier.wait()
        return client.post(f"/orders/{order_id}/{action}", auth=alice).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(act, ["pay", "cancel"]))
    assert sorted(results) == [200, 409]
    state = client.get(f"/orders/{order_id}", auth=alice).json()["status"]
    stock = client.get(f"/products/{product_id}", auth=alice).json()["available_stock"]
    assert (state, stock) in [("PAID", 0), ("CANCELLED", 1)]
