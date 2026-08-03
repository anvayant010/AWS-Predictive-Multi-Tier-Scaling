from __future__ import annotations

from pydantic import BaseModel


class Product(BaseModel):
    id: int
    name: str
    category: str
    price_usd: float
    in_stock: bool


PRODUCTS: list[Product] = [
    Product(id=1, name="Wireless Mouse", category="electronics", price_usd=19.99, in_stock=True),
    Product(
        id=2, name="Mechanical Keyboard", category="electronics", price_usd=89.50, in_stock=True
    ),
    Product(id=3, name="USB-C Hub", category="electronics", price_usd=34.00, in_stock=False),
    Product(id=4, name="Ceramic Mug", category="home", price_usd=12.25, in_stock=True),
    Product(id=5, name="Desk Lamp", category="home", price_usd=27.75, in_stock=True),
    Product(id=6, name="Notebook", category="office", price_usd=4.50, in_stock=True),
    Product(id=7, name="Standing Desk", category="office", price_usd=349.00, in_stock=False),
]
