
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import db, main
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")

    async def fake_scrape(url, client=None):
        from app.scraper import ScrapeResult, site_from_url
        prices = {"cheap.com": 90.0, "mid.com": 100.0, "pricey.com": 120.0}
        site = site_from_url(url)
        if site == "blocked.com":
            return ScrapeResult(site=site, error="This site blocked automatic checking.")
        return ScrapeResult(site=site, price=prices[site], currency="USD", title=f"Item at {site}",
                            in_stock=site != "pricey.com", method="test")

    monkeypatch.setattr(main, "scrape", fake_scrape)
    with TestClient(main.app) as c:
        yield c


def test_ranking_flow(client):
    pid = client.post("/api/products", json={"name": "Headphones"}).json()["id"]
    for url in ("mid.com/x", "pricey.com/x", "blocked.com/x"):
        client.post(f"/api/products/{pid}/listings", json={"url": url})
    # cheap.com price + shipping makes its total 105, so mid.com (100) should win
    client.post(f"/api/products/{pid}/listings", json={"url": "cheap.com/x", "shipping": 15})
    # manual entry
    p = client.post(f"/api/products/{pid}/listings", json={"site": "Local store", "price": 110}).json()

    order = [(l["site"], l["total"], l["rank"]) for l in p["listings"]]
    assert order == [
        ("mid.com", 100.0, 1),
        ("cheap.com", 105.0, 2),
        ("Local store", 110.0, 3),
        ("pricey.com", 120.0, 4),   # out of stock goes after in-stock listings
        ("blocked.com", None, None),
    ]
    assert p["best_total"] == 100.0 and p["listings"][1]["diff"] == 5.0
    assert "blocked" in p["listings"][-1]["error"]

    # manual fix for the blocked site makes it the new winner and records history
    blocked_id = p["listings"][-1]["id"]
    p = client.patch(f"/api/listings/{blocked_id}", json={"price": 80}).json()
    assert p["listings"][0]["site"] == "blocked.com" and p["listings"][0]["manual"] == 1
    assert len(p["listings"][0]["history"]) == 1

    # refresh keeps the manual price for the site that still blocks scraping
    p = client.post(f"/api/products/{pid}/refresh").json()
    top = p["listings"][0]
    assert top["site"] == "blocked.com" and top["price"] == 80 and top["error"]

    assert client.get("/api/products").json()[0]["best_total"] == 80
    assert client.delete(f"/api/products/{pid}").status_code == 204
    assert client.get(f"/api/products/{pid}").status_code == 404


def test_validation(client):
    pid = client.post("/api/products", json={"name": "X"}).json()["id"]
    assert client.post(f"/api/products/{pid}/listings", json={}).status_code == 422
    assert client.post(f"/api/products/{pid}/listings", json={"site": "a", "price": -1}).status_code == 422
