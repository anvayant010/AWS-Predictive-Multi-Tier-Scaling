from fastapi.testclient import TestClient

from demo_app.catalog_service.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_products() -> None:
    response = client.get("/products")
    assert response.status_code == 200
    assert len(response.json()) == 7


def test_list_products_filtered_by_category() -> None:
    response = client.get("/products", params={"category": "electronics"})
    assert response.status_code == 200
    products = response.json()
    assert len(products) == 3
    assert all(p["category"] == "electronics" for p in products)


def test_get_product_by_id() -> None:
    response = client.get("/products/1")
    assert response.status_code == 200
    assert response.json()["name"] == "Wireless Mouse"


def test_get_product_not_found() -> None:
    response = client.get("/products/999")
    assert response.status_code == 404
