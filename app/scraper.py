"""Generic product-price extraction that works across most online stores.

Strategy, most reliable first:
  1. JSON-LD structured data (schema.org Product / Offer) - used by most big retailers
  2. Price meta tags (Open Graph / product:price, itemprop=price)
  3. Known selectors for popular stores (Amazon, eBay, Best Buy, Target, Walmart, ...)
  4. Generic "price"-looking elements that contain a currency symbol
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CURRENCY_SYMBOLS = [
    ("C$", "CAD"), ("A$", "AUD"), ("US$", "USD"), ("R$", "BRL"),
    ("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("¥", "JPY"),
    ("₩", "KRW"), ("₽", "RUB"), ("₺", "TRY"), ("zł", "PLN"), ("Rs", "INR"),
]
CURRENCY_CODES = {"USD", "EUR", "GBP", "INR", "JPY", "CAD", "AUD", "CNY", "KRW",
                  "CHF", "SEK", "NOK", "DKK", "PLN", "BRL", "MXN", "TRY", "AED", "SGD"}

# (css selector, label) - checked in order after structured data fails.
STORE_SELECTORS = [
    # Amazon
    ("#corePrice_feature_div .a-offscreen", "amazon"),
    ("#corePriceDisplay_desktop_feature_div .a-offscreen", "amazon"),
    ("#priceblock_dealprice", "amazon"),
    ("#priceblock_ourprice", "amazon"),
    ("#apex_desktop .a-offscreen", "amazon"),
    (".a-price .a-offscreen", "amazon"),
    # eBay
    (".x-price-primary", "ebay"),
    ("#prcIsum", "ebay"),
    # Best Buy
    ("[data-testid='customer-price'] span", "bestbuy"),
    (".priceView-customer-price span", "bestbuy"),
    # Target
    ("[data-test='product-price']", "target"),
    # Walmart
    ("[itemprop='price']", "microdata"),
    ("[data-seo-id='hero-price']", "walmart"),
    ("span[data-automation-id='product-price']", "walmart"),
    # Shopify / WooCommerce / common themes
    (".price-item--sale", "shopify"),
    (".price-item--regular", "shopify"),
    (".woocommerce-Price-amount", "woocommerce"),
    (".product-price", "generic"),
    (".sale-price", "generic"),
    (".price", "generic"),
]

BLOCK_MARKERS = [
    "enter the characters you see below",   # Amazon
    "/errors/validatecaptcha",              # Amazon
    "robot or human",                       # Walmart
    "px-captcha",                           # PerimeterX
    "are you a robot",
    "verify you are a human",
    "access denied",
    "cf-challenge",                         # Cloudflare
    "attention required! | cloudflare",
]


@dataclass
class ScrapeResult:
    site: str
    price: float | None = None
    currency: str | None = None
    title: str | None = None
    in_stock: bool | None = None
    shipping: float | None = None
    method: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers

def site_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host or "unknown"


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def parse_price(value) -> float | None:
    """Turn '$1,299.99', '1.299,99 €', '₹1,29,999', 19.5 ... into a float."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):  # plain machine-readable number
        v = float(s)
        return v if v > 0 else None
    m = re.search(r"\d[\d.,\s  ']*", s)
    if not m:
        return None
    num = re.sub(r"[\s  ']", "", m.group(0)).rstrip(".,")
    dot, comma = num.rfind("."), num.rfind(",")
    if dot >= 0 and comma >= 0:
        # whichever separator comes last is the decimal point
        num = num.replace(",", "") if dot > comma else num.replace(".", "").replace(",", ".")
    elif comma >= 0:
        after = num[comma + 1:]
        num = num.replace(",", ".") if num.count(",") == 1 and len(after) in (1, 2) else num.replace(",", "")
    elif dot >= 0:
        if num.count(".") > 1 or len(num[dot + 1:]) == 3:
            num = num.replace(".", "")  # 1.299 / 1.299.000 -> thousands separators
    try:
        v = float(num)
    except ValueError:
        return None
    return v if v > 0 else None


def detect_currency(text: str | None) -> str | None:
    if not text:
        return None
    for code in re.findall(r"\b[A-Z]{3}\b", text):
        if code in CURRENCY_CODES:
            return code
    for sym, code in CURRENCY_SYMBOLS:
        if sym in text:
            return code
    return None


def _has_currency_mark(text: str) -> bool:
    return detect_currency(text) is not None


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return [str(x) for x in (t if isinstance(t, list) else [t])]


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _walk(v)


def _jsonld_nodes(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip().rstrip(";")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _walk(data)


def _availability(value) -> bool | None:
    if not value:
        return None
    v = str(value).lower()
    if any(k in v for k in ("outofstock", "soldout", "discontinued", "out of stock")):
        return False
    if any(k in v for k in ("instock", "in stock", "limitedavailability", "preorder", "onlineonly")):
        return True
    return None


def _offer_price(offer: dict) -> tuple[float | None, str | None]:
    for key in ("price", "lowPrice", "highPrice"):
        p = parse_price(offer.get(key))
        if p:
            return p, offer.get("priceCurrency")
    spec = offer.get("priceSpecification")
    for s in spec if isinstance(spec, list) else [spec]:
        if isinstance(s, dict):
            p = parse_price(s.get("price"))
            if p:
                return p, s.get("priceCurrency") or offer.get("priceCurrency")
    return None, None


def _offer_shipping(offer: dict) -> float | None:
    details = offer.get("shippingDetails")
    for d in details if isinstance(details, list) else [details]:
        if isinstance(d, dict) and isinstance(d.get("shippingRate"), dict):
            v = d["shippingRate"].get("value")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return None


# ---------------------------------------------------------------- extraction

def _from_jsonld(soup: BeautifulSoup, res: ScrapeResult) -> bool:
    nodes = list(_jsonld_nodes(soup))
    products = [n for n in nodes if any("Product" in t for t in _types(n))]
    candidates = []
    for prod in products:
        offers = prod.get("offers")
        for offer in _walk(offers) if offers else []:
            if any("Offer" in t for t in _types(offer)) or "price" in offer or "lowPrice" in offer:
                candidates.append((prod, offer))
    if not candidates:  # standalone Offer blocks
        candidates = [(None, n) for n in nodes if any(t in ("Offer", "AggregateOffer") for t in _types(n))]
    for prod, offer in candidates:
        price, cur = _offer_price(offer)
        if price:
            res.price, res.currency, res.method = price, cur, "structured data"
            res.in_stock = _availability(offer.get("availability"))
            res.shipping = _offer_shipping(offer)
            if prod and prod.get("name") and not res.title:
                res.title = str(prod["name"]).strip()
            return True
    return False


def _from_meta(soup: BeautifulSoup, res: ScrapeResult) -> bool:
    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) \
                or soup.find("meta", attrs={"itemprop": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    price = parse_price(meta("product:price:amount", "og:price:amount", "price"))
    if price:
        res.price, res.method = price, "meta tags"
        res.currency = meta("product:price:currency", "og:price:currency", "priceCurrency")
        res.in_stock = _availability(meta("product:availability", "og:availability", "availability"))
        return True
    return False


def _from_selectors(soup: BeautifulSoup, res: ScrapeResult) -> bool:
    for selector, label in STORE_SELECTORS:
        for el in soup.select(selector)[:5]:
            text = el.get("content") or el.get_text(" ", strip=True)
            if not text or len(text) > 40:
                continue
            # generic selectors are noisy: only trust them when a currency is visible
            if label == "generic" and not _has_currency_mark(text):
                continue
            price = parse_price(text)
            if price:
                res.price, res.method = price, f"page element ({label})"
                res.currency = detect_currency(text)
                return True
    return False


def _title(soup: BeautifulSoup) -> str | None:
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return tag["content"].strip()
    for sel in ("#productTitle", "h1"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True)
    return soup.title.get_text(strip=True) if soup.title else None


def looks_blocked(html: str) -> bool:
    low = html[:200_000].lower()
    return any(m in low for m in BLOCK_MARKERS)


def extract(html: str, url: str) -> ScrapeResult:
    res = ScrapeResult(site=site_from_url(url))
    soup = BeautifulSoup(html, "html.parser")
    found = _from_jsonld(soup, res) or _from_meta(soup, res) or _from_selectors(soup, res)
    if not res.title:
        res.title = _title(soup)
    if res.title:
        res.title = res.title[:300]
    if found:
        if res.currency:
            res.currency = str(res.currency).upper()[:3]
    else:
        if looks_blocked(html):
            res.error = "This site blocked automatic checking. Enter the price manually."
        else:
            res.error = "Couldn't find a price on this page. Enter it manually."
    return res


async def scrape(url: str, client: httpx.AsyncClient | None = None) -> ScrapeResult:
    url = normalize_url(url)
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20)
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        return ScrapeResult(site=site_from_url(url), error=f"Couldn't reach the site ({e.__class__.__name__}).")
    finally:
        if own:
            await client.aclose()
    if resp.status_code in (401, 403, 429, 503) or (resp.status_code >= 400 and looks_blocked(resp.text)):
        return ScrapeResult(site=site_from_url(url),
                            error=f"This site blocked automatic checking (HTTP {resp.status_code}). Enter the price manually.")
    if resp.status_code >= 400:
        return ScrapeResult(site=site_from_url(url), error=f"The page returned HTTP {resp.status_code}.")
    return extract(resp.text, str(resp.url))
