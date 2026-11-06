# Price Ranker

Compare the price of one product across any websites and rank them, cheapest first.

## Run

```bash
uv sync
uv run python -m app        # open http://127.0.0.1:8000
```

## How it works

1. Create a **comparison** (e.g. "AirPods Pro 2").
2. Paste the product's link from each store. The price, title, stock status and
   shipping (when the page lists it) are read automatically from the page's structured data,
   price meta tags, or well-known store markup.
3. Listings are ranked by **total = price + shipping**. In-stock items come first, and
   each row shows how much more it costs than the best price.
4. Click **Refresh all prices** any time. Every check is saved, so each row gets a
   price-history sparkline.

If a site blocks automated checking (Amazon, Walmart, eBay, Best Buy and Newegg often do),
the row says so. Click ✎ and type the price you see. You can also add a store with
**Enter a price manually**.

Data lives in `data/prices.db` (SQLite). Set `PRICE_DB` to use another file and `PORT` to change the port.

## Tests

```bash
uv run pytest
```
