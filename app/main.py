"""Price Ranker web app: FastAPI backend + a single-page UI in /static."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .scraper import HEADERS, normalize_url, scrape, site_from_url

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    yield


app = FastAPI(title="Price Ranker", lifespan=lifespan)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ListingIn(BaseModel):
    url: str | None = None
    site: str | None = Field(None, max_length=100)
    price: float | None = Field(None, gt=0)
    shipping: float = Field(0, ge=0)
    currency: str | None = Field(None, max_length=3)


class ListingPatch(BaseModel):
    price: float | None = Field(None, gt=0)
    shipping: float | None = Field(None, ge=0)
    site: str | None = Field(None, max_length=100)


# ---------------------------------------------------------------- ranking

def ranked_product(pid: int) -> dict:
    product = db.get_product(pid)
    if not product:
        raise HTTPException(404, "Comparison not found")
    listings = db.listings_for(pid)
    for l in listings:
        l["total"] = None if l["price"] is None else round(l["price"] + (l["shipping"] or 0), 2)
        l["history"] = db.history(l["id"])

    # in-stock (or unknown) listings first, then by total price; unpriced at the bottom
    priced = sorted((l for l in listings if l["total"] is not None),
                    key=lambda l: (l["in_stock"] == 0, l["total"]))
    unpriced = [l for l in listings if l["total"] is None]

    best = next((l["total"] for l in priced if l["in_stock"] != 0), priced[0]["total"] if priced else None)
    for i, l in enumerate(priced, 1):
        l["rank"] = i
        l["diff"] = round(l["total"] - best, 2)
        l["diff_pct"] = round(100 * l["diff"] / best, 1) if best else 0
    for l in unpriced:
        l["rank"] = l["diff"] = l["diff_pct"] = None

    currencies = {l["currency"] for l in priced if l["currency"]}
    totals = [l["total"] for l in priced]
    product.update(
        listings=priced + unpriced,
        best_total=best,
        max_total=max(totals) if totals else None,
        mixed_currency=len(currencies) > 1,
        currency=next(iter(currencies)) if len(currencies) == 1 else None,
    )
    return product


async def refresh_listing(listing: dict, client: httpx.AsyncClient | None = None) -> dict:
    if not listing.get("url"):
        return listing
    res = await scrape(listing["url"], client)
    if res.price:
        return db.update_listing(
            listing["id"], record_price=True,
            price=res.price, currency=res.currency or listing.get("currency"),
            title=res.title or listing.get("title"), in_stock=res.in_stock,
            shipping=res.shipping if res.shipping is not None else listing.get("shipping", 0),
            method=res.method, error=None, manual=0, last_checked=db.now(),
        )
    # keep the last known price, just record the problem
    return db.update_listing(listing["id"], error=res.error, last_checked=db.now(),
                             title=listing.get("title") or res.title)


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/products")
def list_products():
    return db.list_products()


@app.post("/api/products", status_code=201)
def create_product(body: ProductIn):
    return db.create_product(body.name.strip())


@app.get("/api/products/{pid}")
def get_product(pid: int):
    return ranked_product(pid)


@app.patch("/api/products/{pid}")
def rename_product(pid: int, body: ProductIn):
    ranked_product(pid)
    db.rename_product(pid, body.name.strip())
    return ranked_product(pid)


@app.delete("/api/products/{pid}", status_code=204)
def delete_product(pid: int):
    db.delete_product(pid)


@app.post("/api/products/{pid}/listings", status_code=201)
async def add_listing(pid: int, body: ListingIn):
    ranked_product(pid)  # 404 if missing
    url = normalize_url(body.url) if body.url and body.url.strip() else None
    if not url and not (body.site and body.price):
        raise HTTPException(422, "Give a product URL, or a site name and a price.")
    site = (body.site or "").strip() or (site_from_url(url) if url else "manual")

    if body.price:  # manual entry (URL optional, used for the link and later refreshes)
        db.add_listing(pid, url=url, site=site, price=body.price, shipping=body.shipping,
                       currency=(body.currency or "").upper() or None, manual=1,
                       method="manual", last_checked=db.now())
        return ranked_product(pid)

    res = await scrape(url)
    db.add_listing(pid, url=url, site=site, title=res.title, price=res.price,
                   currency=res.currency, in_stock=res.in_stock,
                   shipping=res.shipping if res.shipping is not None else body.shipping,
                   method=res.method, error=res.error, last_checked=db.now())
    return ranked_product(pid)


@app.patch("/api/listings/{lid}")
def edit_listing(lid: int, body: ListingPatch):
    listing = db.get_listing(lid)
    if not listing:
        raise HTTPException(404, "Listing not found")
    fields = body.model_dump(exclude_none=True)
    if "price" in fields:
        fields.update(manual=1, method="manual", error=None, last_checked=db.now())
    db.update_listing(lid, record_price="price" in fields, **fields)
    return ranked_product(listing["product_id"])


@app.delete("/api/listings/{lid}")
def delete_listing(lid: int):
    listing = db.get_listing(lid)
    if not listing:
        raise HTTPException(404, "Listing not found")
    db.delete_listing(lid)
    return ranked_product(listing["product_id"])


@app.post("/api/listings/{lid}/refresh")
async def refresh_one(lid: int):
    listing = db.get_listing(lid)
    if not listing:
        raise HTTPException(404, "Listing not found")
    await refresh_listing(listing)
    return ranked_product(listing["product_id"])


@app.post("/api/products/{pid}/refresh")
async def refresh_all(pid: int):
    ranked_product(pid)
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
        async def one(l):
            async with sem:
                await refresh_listing(l, client)
        await asyncio.gather(*(one(l) for l in db.listings_for(pid)))
    return ranked_product(pid)
