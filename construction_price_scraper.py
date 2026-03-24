"""
Construction Material Price Scraper & Analyzer
Targets: Biiibo, IHL Canada, Home Depot CA, RONA, Yvon Building Supply
Outputs: CSV + console comparison report

Requirements:
    pip install requests beautifulsoup4 playwright pandas rich
    playwright install chromium
"""

import re
import time
import json
import csv
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd
from rich.console import Console
from rich.table import Table

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Product:
    supplier:   str
    category:   str
    name:       str
    sku:        str
    price_cad:  Optional[float]
    pro_price:  Optional[float]
    unit:       str
    url:        str
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def get_html(url: str, retries: int = 3, delay: float = 1.5):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            log.warning(f"[Attempt {attempt}/{retries}] GET {url} failed: {e}")
            time.sleep(delay * attempt)
    return None

def parse_price(text: str):
    m = re.search(r"\$?\s*([\d,]+\.?\d*)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def rate_limit(seconds: float = 1.2):
    time.sleep(seconds)

def make_stealth_browser(pw):
    """
    Launch a stealth Chromium instance that bypasses common bot-detection checks.
    Key flags:
      --disable-http2           avoids ERR_HTTP2_PROTOCOL_ERROR on Akamai/Cloudflare CDNs
      --disable-blink-features  removes the navigator.webdriver fingerprint
    Returns (browser, page) ready to use.
    """
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-http2",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-CA",
        extra_http_headers={
            "Accept-Language": "en-CA,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-ch-ua": '"Chromium";v="124","Not-A.Brand";v="8"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        },
    )
    page = ctx.new_page()
    # Mask the webdriver property so JS-based bot checks see a real browser
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, page

# ---------------------------------------------------------------------------
# SCRAPER 1 - Biiibo (biiibo.com)
# ---------------------------------------------------------------------------

BIIIBO_CATEGORIES = {
    "Dimensional Lumber":  "https://biiibo.com/building-materials/lumber/dimensional-lumber",
    "Pressure Treated":    "https://biiibo.com/building-materials/lumber/pressure-treated-lumber",
    "Plywood & OSB":       "https://biiibo.com/building-materials/lumber/plywood-osb",
    "Drywall":             "https://biiibo.com/building-materials/drywall/drywall-sheets-by-size-and-thickness",
    "Insulation":          "https://biiibo.com/building-materials/insulation",
    "Concrete & Cement":   "https://biiibo.com/building-materials/concrete-cement-masonry",
    "Metal Studs":         "https://biiibo.com/building-materials/drywall/drywall-essentials/metal-studs",
    "Steel Framing":       "https://biiibo.com/building-materials/metals-structural-steel",
}

def scrape_biiibo_page(url: str, category: str) -> list:
    products = []
    page = 1
    while True:
        paged_url = f"{url}?page={page}" if page > 1 else url
        soup = get_html(paged_url)
        if not soup:
            break
        cards = soup.select("div[class*='ProductCard'], article[class*='product']")
        if not cards:
            cards = soup.find_all(
                lambda tag: tag.name in ("div", "article")
                and tag.get_text() and "Item:" in tag.get_text()
                and "$" in tag.get_text()
                and len(tag.get_text(strip=True)) < 500
            )
        if not cards:
            text = soup.get_text(separator="\n")
            pattern = re.compile(
                r"(?P<name>[2-9\d][^\n]*(?:Lumber|Plywood|OSB|Drywall|Insulation|Mix|Board|Panel)[^\n]*)\n"
                r"Item:\s*(?P<sku>\d+)\n"
                r"(?:Save[^\n]*\n)*"
                r"\$\s*(?P<price>[\d.]+)"
                r"(?:.*?Pay \$\s*(?P<pro>[\d.]+) with PRO)?",
                re.IGNORECASE | re.DOTALL
            )
            for m in pattern.finditer(text):
                products.append(Product(
                    supplier="Biiibo", category=category,
                    name=m.group("name").strip(), sku=m.group("sku").strip(),
                    price_cad=float(m.group("price")),
                    pro_price=float(m.group("pro")) if m.group("pro") else None,
                    unit="each", url=paged_url,
                ))
            break
        found_any = False
        for card in cards:
            text = card.get_text(separator="\n", strip=True)
            name_el = card.find(["h2", "h3", "h4", "a"])
            name = name_el.get_text(strip=True) if name_el else ""
            sku_m = re.search(r"Item:\s*(\d+)", text)
            price_m = re.findall(r"\$\s*([\d.]+)", text)
            pro_m = re.search(r"Pay\s+\$\s*([\d.]+)\s+with PRO", text)
            if not name or not price_m:
                continue
            products.append(Product(
                supplier="Biiibo", category=category, name=name,
                sku=sku_m.group(1) if sku_m else "",
                price_cad=float(price_m[0]) if price_m else None,
                pro_price=float(pro_m.group(1)) if pro_m else None,
                unit="each", url=paged_url,
            ))
            found_any = True
        if not found_any:
            break
        next_btn = soup.find("a", string=re.compile(r"next|›|»", re.I))
        if not next_btn:
            break
        page += 1
        rate_limit()
    log.info(f"  Biiibo [{category}]: {len(products)} products")
    return products

def scrape_biiibo() -> list:
    all_products = []
    console.rule("[bold blue]Scraping Biiibo[/bold blue]")
    for cat, url in BIIIBO_CATEGORIES.items():
        all_products.extend(scrape_biiibo_page(url, cat))
        rate_limit()
    return all_products

# ---------------------------------------------------------------------------
# SCRAPER 2 - IHL Canada (ihlcanada.com) - Shopify store
# ---------------------------------------------------------------------------

IHL_CATEGORIES = {
    "Lumber":        "https://ihlcanada.com/collections/lumber",
    "Plywood":       "https://ihlcanada.com/collections/lumber-plywood",
    "Metal Framing": "https://ihlcanada.com/collections/building-materials-metal-framing",
    "Insulation":    "https://ihlcanada.com/collections/insulation",
    "Fasteners":     "https://ihlcanada.com/collections/fasteners",
    "Nails":         "https://ihlcanada.com/collections/nails",
    "Adhesives":     "https://ihlcanada.com/collections/selsil",
    "Safety":        "https://ihlcanada.com/collections/safety",
}

def scrape_ihl_collection(base_url: str, category: str) -> list:
    products = []
    page = 1
    while True:
        json_url = f"{base_url}/products.json?limit=250&page={page}"
        try:
            resp = requests.get(json_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  IHL JSON fetch failed ({json_url}): {e}")
            return scrape_ihl_html(base_url, category)
        items = data.get("products", [])
        if not items:
            break
        for item in items:
            for variant in item.get("variants", []):
                price_str = variant.get("price", "0")
                products.append(Product(
                    supplier="IHL Canada", category=category,
                    name=item.get("title", ""),
                    sku=str(variant.get("sku", "")),
                    price_cad=float(price_str) if price_str else None,
                    pro_price=None,
                    unit=variant.get("option1", "each"),
                    url=f"https://ihlcanada.com/products/{item.get('handle', '')}",
                ))
        if len(items) < 250:
            break
        page += 1
        rate_limit()
    log.info(f"  IHL Canada [{category}]: {len(products)} products")
    return products

def scrape_ihl_html(url: str, category: str) -> list:
    products = []
    page = 1
    while True:
        soup = get_html(f"{url}?page={page}")
        if not soup:
            break
        cards = soup.select(".product-item, .grid__item, li[class*='product']")
        if not cards:
            break
        for card in cards:
            title_el = card.select_one(".product-item__title, .h4, h3, h4")
            price_el = card.select_one(".price__current, .price, [class*='price']")
            sku_el = card.get("data-product-id", "")
            name = title_el.get_text(strip=True) if title_el else ""
            price = parse_price(price_el.get_text()) if price_el else None
            link_el = card.select_one("a")
            href = ("https://ihlcanada.com" + link_el["href"]) if link_el else url
            if name and price:
                products.append(Product(
                    supplier="IHL Canada", category=category, name=name,
                    sku=str(sku_el), price_cad=price, pro_price=None,
                    unit="each", url=href,
                ))
        next_el = soup.select_one("a[rel='next'], .pagination__next, a.next")
        if not next_el:
            break
        page += 1
        rate_limit()
    return products

def scrape_ihl() -> list:
    all_products = []
    console.rule("[bold green]Scraping IHL Canada[/bold green]")
    for cat, url in IHL_CATEGORIES.items():
        all_products.extend(scrape_ihl_collection(url, cat))
        rate_limit()
    return all_products

# ---------------------------------------------------------------------------
# SCRAPER 3 - Home Depot Canada (Playwright)
# ---------------------------------------------------------------------------

HOME_DEPOT_SEARCHES = {
    "2x4x8 Lumber": "https://www.homedepot.ca/search?q=2x4x8+spruce+framing+lumber",
    "2x6x8 Lumber": "https://www.homedepot.ca/search?q=2x6x8+spruce+framing+lumber",
    "1/2 Drywall":  "https://www.homedepot.ca/search?q=1%2F2+drywall+4x8",
    "OSB 7/16":     "https://www.homedepot.ca/search?q=7%2F16+OSB+4x8",
    "Insulation":   "https://www.homedepot.ca/search?q=r20+batt+insulation",
}

def scrape_homedepot_playwright() -> list:
    if not PLAYWRIGHT_AVAILABLE:
        log.warning("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return []
    products = []
    console.rule("[bold orange1]Scraping Home Depot Canada (Playwright)[/bold orange1]")
    with sync_playwright() as pw:
        browser, page = make_stealth_browser(pw)
        for search_name, url in HOME_DEPOT_SEARCHES.items():
            cat_count_before = len(products)
            try:
                # Use domcontentloaded — networkidle never resolves on HD's Angular app
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                # Wait for Angular to render product cards (confirmed class: acl-product-card)
                page.wait_for_selector(".acl-product-card", timeout=20000)
                page.wait_for_timeout(1500)
                cards = page.query_selector_all(".acl-product-card")
                for card in cards[:25]:
                    try:
                        # BEM child elements: acl-product-card__description / acl-product-card__price
                        name_el = card.query_selector(
                            "[class*='description'], [class*='title'], h2, h3, h4, a[class*='link']"
                        )
                        price_el = card.query_selector("[class*='price']")
                        sku_el   = card.query_selector("[class*='model'], [class*='sku'], [class*='internet']")
                        name  = name_el.inner_text().strip() if name_el else ""
                        price = parse_price(price_el.inner_text()) if price_el else None
                        sku   = sku_el.inner_text().strip() if sku_el else (
                            card.get_attribute("data-product-id") or ""
                        )
                        if name and price:
                            products.append(Product(
                                supplier="Home Depot CA", category=search_name,
                                name=name, sku=sku, price_cad=price,
                                pro_price=None, unit="each", url=url,
                            ))
                    except Exception:
                        continue
            except Exception as e:
                log.warning(f"  Home Depot CA [{search_name}] error: {e}")
            log.info(f"  Home Depot CA [{search_name}]: {len(products) - cat_count_before} products")
            time.sleep(2)
        browser.close()
    return products

# ---------------------------------------------------------------------------
# SCRAPER 4 - RONA (Playwright)
# ---------------------------------------------------------------------------

RONA_SEARCHES = {
    "2x4x8 Lumber": "https://www.rona.ca/en/search?keyword=2x4+8ft+spruce+lumber",
    "2x6x8 Lumber": "https://www.rona.ca/en/search?keyword=2x6+8ft+spruce+lumber",
    "1/2 Drywall":  "https://www.rona.ca/en/search?keyword=1%2F2+drywall+panel+4x8",
    "OSB 7/16":     "https://www.rona.ca/en/search?keyword=7%2F16+osb+4x8",
    "Insulation":   "https://www.rona.ca/en/search?keyword=r20+batt+insulation",
}

def scrape_rona_playwright() -> list:
    if not PLAYWRIGHT_AVAILABLE:
        log.warning("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return []
    products = []
    console.rule("[bold red]Scraping RONA (Playwright)[/bold red]")

    # RONA product-tile selectors (WebSphere Commerce / custom theme)
    RONA_CARD_SEL = (
        "[class*='product-listing-item'], "
        "[class*='product-tile'], "
        "[class*='productTile'], "
        "[id*='CatalogEntryWidget'], "
        "li.grid-item, "
        "div.col-sm-3.col-xs-6"          # fallback: Bootstrap grid cells used as product wrappers
    )

    with sync_playwright() as pw:
        browser, page = make_stealth_browser(pw)

        # Warm up with the home page first so we have a valid session & cookies
        try:
            page.goto("https://www.rona.ca/en", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception:
            pass

        for search_name, url in RONA_SEARCHES.items():
            cat_count_before = len(products)
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                # Give the JS search engine time to inject results
                page.wait_for_timeout(5000)
                # Try to wait for a product tile to confirm results loaded
                try:
                    page.wait_for_selector(RONA_CARD_SEL, timeout=15000)
                except Exception:
                    pass  # Continue anyway — try to scrape whatever rendered

                cards = page.query_selector_all(RONA_CARD_SEL)
                for card in cards[:25]:
                    try:
                        name_el  = card.query_selector("[class*='title'], [class*='name'], h2, h3, h4, a")
                        price_el = card.query_selector("[class*='price'], [class*='Prix']")
                        name  = name_el.inner_text().strip() if name_el else ""
                        price = parse_price(price_el.inner_text()) if price_el else None
                        link_el = card.query_selector("a[href]")
                        href  = link_el.get_attribute("href") if link_el else url
                        if href and not href.startswith("http"):
                            href = "https://www.rona.ca" + href
                        if name and price:
                            products.append(Product(
                                supplier="RONA", category=search_name,
                                name=name, sku="", price_cad=price,
                                pro_price=None, unit="each", url=href or url,
                            ))
                    except Exception:
                        continue
            except Exception as e:
                log.warning(f"  RONA [{search_name}] error: {e}")
            log.info(f"  RONA [{search_name}]: {len(products) - cat_count_before} products")
            time.sleep(2)
        browser.close()
    return products

# ---------------------------------------------------------------------------
# SCRAPER 5 - Yvon Building Supply
# ---------------------------------------------------------------------------

def scrape_yvon() -> list:
    products = []
    console.rule("[bold purple]Scraping Yvon Building Supply[/bold purple]")
    urls = {
        "Lumber":  "https://www.yvonbuildingsupply.ca/lumber",
        "Drywall": "https://www.yvonbuildingsupply.ca/drywall",
    }
    for category, url in urls.items():
        soup = get_html(url)
        if not soup:
            continue
        for card in soup.select(".product, li.product"):
            name_el = card.select_one("h2, h3, .woocommerce-loop-product__title")
            price_el = card.select_one(".price, .woocommerce-Price-amount")
            link_el = card.select_one("a")
            name = name_el.get_text(strip=True) if name_el else ""
            price = parse_price(price_el.get_text()) if price_el else None
            href = link_el["href"] if link_el else url
            if name and price:
                products.append(Product(
                    supplier="Yvon Building Supply", category=category,
                    name=name, sku="", price_cad=price,
                    pro_price=None, unit="each", url=href,
                ))
        rate_limit()
    log.info(f"  Yvon Building Supply: {len(products)} products")
    return products

# ---------------------------------------------------------------------------
# ANALYSIS ENGINE
# ---------------------------------------------------------------------------

BENCHMARK_KEYWORDS = {
    "2x4x8 Spruce":           ["2 in. x 4 in. x 8 ft. Spruce", "2x4x8 SPF", "2x4x8"],
    "2x4x10 Spruce":          ["2 in. x 4 in. x 10 ft. Spruce", "2x4x10 SPF", "2x4x10"],
    "2x6x8 Spruce":           ["2 in. x 6 in. x 8 ft. Spruce", "2x6x8 SPF", "2x6x8"],
    "2x6x10 Spruce":          ["2 in. x 6 in. x 10 ft. Spruce", "2x6x10 SPF", "2x6x10"],
    "1/2 Drywall 4x8":        ["1/2 in. x 4 ft. x 8 ft", "1/2in Drywall"],
    "7/16 OSB 4x8":           ["7/16 in. x 4 ft. x 8 ft. OSB", "7/16 OSB"],
    "1/2 Spruce Plywood":     ["1/2 in. x 4 ft. x 8 ft. Standard Spruce Plywood"],
    "3/4 Spruce Plywood":     ["3/4 in. x 4 ft. x 8 ft. Standard Spruce Plywood"],
    "2x4x8 Pressure Treated": ["2 in. x 4 in. x 8 ft. Brown Pressure Treated", "2x4 Treated"],
}

def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())

def match_benchmark(product_name: str, keywords: list) -> bool:
    n = normalize_name(product_name)
    return any(normalize_name(kw) in n or n in normalize_name(kw) for kw in keywords)

def build_comparison_table(all_products: list) -> pd.DataFrame:
    rows = []
    for benchmark, keywords in BENCHMARK_KEYWORDS.items():
        matches = [p for p in all_products if match_benchmark(p.name, keywords)]
        for p in matches:
            rows.append({
                "Benchmark":   benchmark,
                "Supplier":    p.supplier,
                "Product":     p.name,
                "SKU":         p.sku,
                "Price (CAD)": p.price_cad,
                "PRO Price":   p.pro_price,
                "URL":         p.url,
                "Scraped At":  p.scraped_at,
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values(["Benchmark", "Price (CAD)"])

def print_comparison_report(df: pd.DataFrame):
    if df.empty:
        console.print("[yellow]No matching products found.[/yellow]")
        return
    console.rule("[bold white]LIVE PRICE COMPARISON REPORT[/bold white]")
    for benchmark in df["Benchmark"].unique():
        sub = df[df["Benchmark"] == benchmark].dropna(subset=["Price (CAD)"])
        if sub.empty:
            continue
        table = Table(title=f"[bold cyan]{benchmark}[/bold cyan]", show_lines=True)
        table.add_column("Supplier",    style="green",   min_width=20)
        table.add_column("Product",     style="white",   min_width=30)
        table.add_column("Price (CAD)", style="yellow",  justify="right")
        table.add_column("PRO Price",   style="magenta", justify="right")
        table.add_column("vs Biiibo",   style="red",     justify="right")
        biiibo_rows = sub[sub["Supplier"] == "Biiibo"]
        biiibo_price = biiibo_rows["Price (CAD)"].min() if not biiibo_rows.empty else None
        cheapest = sub["Price (CAD)"].min()
        for _, row in sub.iterrows():
            price = row["Price (CAD)"]
            pro = f"${row['PRO Price']:.2f}" if pd.notna(row["PRO Price"]) else "—"
            if biiibo_price and row["Supplier"] != "Biiibo":
                diff = price - biiibo_price
                vs = f"{chr(43) if diff >= 0 else chr(45)}{abs((diff / biiibo_price) * 100):.1f}%"
            else:
                vs = "—"
            price_str = f"[bold green]${price:.2f} BEST[/bold green]" if price == cheapest else f"${price:.2f}"
            table.add_row(row["Supplier"], row["Product"][:45], price_str, pro, vs)
        console.print(table)
    console.rule("[bold white]SUMMARY[/bold white]")
    rows = []
    for supplier in df["Supplier"].unique():
        s = df[df["Supplier"] == supplier]
        rows.append({"Supplier": supplier, "Products": len(s),
            "Avg $": f"${s['Price (CAD)'].mean():.2f}",
            "Min $": f"${s['Price (CAD)'].min():.2f}",
            "Max $": f"${s['Price (CAD)'].max():.2f}"})
    console.print(pd.DataFrame(rows).to_string(index=False))

def save_outputs(all_products: list, df: pd.DataFrame):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── timestamped output folder (artifact / local runs) ──────────────────
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    raw_path = out_dir / f"raw_products_{ts}.csv"
    with open(raw_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=asdict(all_products[0]).keys())
        writer.writeheader()
        writer.writerows([asdict(p) for p in all_products])
    log.info(f"Saved: {raw_path}")
    if not df.empty:
        comp_path = out_dir / f"price_comparison_{ts}.csv"
        df.to_csv(comp_path, index=False)
        log.info(f"Saved: {comp_path}")
    json_path = out_dir / f"products_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in all_products], f, indent=2, default=str)
    log.info(f"Saved: {json_path}")

    # ── docs/latest.json — served by GitHub Pages to the dashboard ─────────
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    latest_payload = {
        "scraped_at": datetime.now().isoformat(),
        "total_products": len(all_products),
        "products": [asdict(p) for p in all_products],
    }
    latest_path = docs_dir / "latest.json"
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest_payload, f, indent=2, default=str)
    log.info(f"Saved: {latest_path}  ← GitHub Pages dashboard feed")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    console.print(
        "[bold]Construction Material Price Scraper[/bold]\n"
        f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "Suppliers: Biiibo · IHL Canada · Home Depot CA · RONA\n"
    )
    all_products = []
    all_products.extend(scrape_biiibo())
    all_products.extend(scrape_ihl())
    all_products.extend(scrape_homedepot_playwright())
    all_products.extend(scrape_rona_playwright())

    console.print(f"\n[bold]Total products scraped:[/bold] {len(all_products)}")

    # Per-supplier summary
    suppliers = {}
    for p in all_products:
        suppliers[p.supplier] = suppliers.get(p.supplier, 0) + 1
    for supplier, count in suppliers.items():
        status = "[green]✓[/green]" if count > 0 else "[red]✗[/red]"
        console.print(f"  {status} {supplier}: {count} products")

    if not all_products:
        console.print("[yellow]No products scraped this run — check logs above for errors.[/yellow]")
        console.print("[yellow]Exiting without writing output (no data to save).[/yellow]")
        # Exit 0 so the workflow doesn't fail the commit step on first run
        return

    df = build_comparison_table(all_products)
    print_comparison_report(df)
    save_outputs(all_products, df)
    console.print("\n[bold green]Done! Check the /output folder for reports.[/bold green]")

if __name__ == "__main__":
    main()
