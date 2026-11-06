import pytest

from app.scraper import extract, parse_price, detect_currency, site_from_url, normalize_url


@pytest.mark.parametrize("raw, expected", [
    ("$1,299.99", 1299.99),
    ("1.299,99 €", 1299.99),
    ("₹1,29,999", 129999.0),
    ("£19", 19.0),
    ("19,95 €", 19.95),
    ("USD 49.00", 49.0),
    ("1.299 kr", 1299.0),
    ("24.5", 24.5),
    (59.99, 59.99),
    ("Free", None),
    ("0.00", None),
    (None, None),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_detect_currency():
    assert detect_currency("$10") == "USD"
    assert detect_currency("10 €") == "EUR"
    assert detect_currency("CAD 10") == "CAD"
    assert detect_currency("C$10") == "CAD"
    assert detect_currency("10") is None


def test_site_and_url_helpers():
    assert site_from_url("https://www.amazon.com/dp/B0") == "amazon.com"
    assert normalize_url("walmart.com/ip/123") == "https://walmart.com/ip/123"


def test_jsonld_product():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[{"@type":"WebPage"},
     {"@type":"Product","name":"Sony WH-1000XM5",
      "offers":{"@type":"Offer","price":"329.99","priceCurrency":"USD",
                "availability":"https://schema.org/InStock",
                "shippingDetails":{"shippingRate":{"value":"4.99","currency":"USD"}}}}]}
    </script></head><body></body></html>"""
    r = extract(html, "https://www.example.com/p/1")
    assert (r.price, r.currency, r.in_stock, r.shipping, r.title) == (329.99, "USD", True, 4.99, "Sony WH-1000XM5")
    assert r.method == "structured data" and r.error is None


def test_jsonld_aggregate_offer_out_of_stock():
    html = """<script type="application/ld+json">[{"@type":"Product","name":"X",
      "offers":{"@type":"AggregateOffer","lowPrice":89,"priceCurrency":"EUR",
                "availability":"OutOfStock"}}]</script>"""
    r = extract(html, "https://shop.de/x")
    assert (r.price, r.currency, r.in_stock) == (89.0, "EUR", False)


def test_meta_tags():
    html = """<head><meta property="og:title" content="Blue Mug">
      <meta property="product:price:amount" content="12.50">
      <meta property="product:price:currency" content="GBP"></head>"""
    r = extract(html, "https://mugs.co.uk/blue")
    assert (r.price, r.currency, r.title, r.method) == (12.5, "GBP", "Blue Mug", "meta tags")


def test_amazon_style_markup():
    html = """<span id="productTitle"> Echo Dot (5th Gen) </span>
      <div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">$49.99</span></span></div>"""
    r = extract(html, "https://www.amazon.com/dp/B09B8V1LZ3")
    assert (r.price, r.currency, r.title, r.site) == (49.99, "USD", "Echo Dot (5th Gen)", "amazon.com")


def test_generic_price_requires_currency_symbol():
    assert extract('<div class="price">Rated 4.5</div>', "https://a.com").price is None
    assert extract('<div class="price">$15.00</div>', "https://a.com").price == 15.0


def test_blocked_page_reports_error():
    html = "<html><body>Enter the characters you see below</body></html>"
    r = extract(html, "https://www.amazon.com/dp/X")
    assert r.price is None and "blocked" in r.error


def test_no_price_found():
    r = extract("<html><title>Blog</title><p>hello</p></html>", "https://blog.com")
    assert r.price is None and "Couldn't find a price" in r.error
