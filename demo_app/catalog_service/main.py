from __future__ import annotations

from fastapi import FastAPI, HTTPException

from demo_app.catalog_service.data import PRODUCTS, Product

app = FastAPI(title="catalog_service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/products", response_model=list[Product])
def list_products(category: str | None = None) -> list[Product]:
    if category is None:
        return PRODUCTS
    return [p for p in PRODUCTS if p.category == category]


@app.get("/products/{product_id}", response_model=Product)
def get_product(product_id: int) -> Product:
    for product in PRODUCTS:
        if product.id == product_id:
            return product
    raise HTTPException(status_code=404, detail=f"product {product_id} not found")
