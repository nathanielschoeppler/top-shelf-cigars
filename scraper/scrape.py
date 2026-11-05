"""Humidor Watch deal checker.

Visits store deal pages, keeps good handmade cigars (medium to full strength,
regular price of $20+ per cigar, real discount), and writes deals.json for the site.
Runs hourly on GitHub Actions. No Claude involved.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deals.json"
CACHE = ROOT / "scraper" / "product-cache.json"
DEBUG = ROOT / "scraper" / "debug"

MIN_MSRP_PER_CIGAR = 20.00
MIN_BOX_DISCOUNT = 0.15
MAX_SINGLE_PRICE = 40.00  # skip $50–$135 collector singles  # boxes must be at least 15% off  # regular price per cigar; Nate's rule: no cheap cigars
MAX_DEALS = 150
MAX_PRODUCT_LOOKUPS = 40   # product pages fetched per run to learn strength
STALE_HOURS = 48

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SKIP_WORDS = re.compile(
    r"\b(sweet|aromatic|vanilla|honey|infused|flavou?red|java|acid|tatiana|backwoods|"
    r"neos|filter(ed)?|cigarillos?|minis?|puritos|swisher|white owl|dutch|phillies|"
    r"djarum|hav-a-tampa|garcia y vega|antonio y cleopatra|cheyenne|nicotine|cutter|"
    r"lighter|humidor|ashtray|case|butane|grape|cherry|rum|wine|cognac|bourbon|"
    r"whiskey cask|coffee|cafe|irish cream|toasted|honey)\b", re.I)
# "Whiskey Row" is a real premium line; allow it back in.
ALLOW_WORDS = re.compile(r"whiskey row", re.I)

session = requests.Session()
session.headers.update(HEADERS)
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
TODAY = (NOW - timedelta(hours=5)).date()  # US Eastern-ish


def log(*a):
    print(*a, flush=True)


def get(url):
    try:
        r = session.get(url, timeout=25)
        if r.status_code != 200:
            log(f"  ! {r.status_code} {url}")
            return None
        if len(r.text) < 2000 or "captcha" in r.text.lower()[:5000]:
            log(f"  ! blocked/empty {url}")
            return None
        return r.text
    except requests.RequestException as e:
        log(f"  ! error {url}: {e}")
        return None


def money(s):
    m = re.search(r"\$?\s*([\d,]+\.\d{2})", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def norm_strength(s):
    if not s:
        return None
    t = s.lower().replace("–", "-").replace(" ", "")
    if "mild" in t or "mellow" in t:
        return None if "full" not in t else "Medium"
    if "medium-full" in t or "med-full" in t or "mediumtofull" in t or "medium-to-full" in t:
        return "Medium-Full"
    if t.startswith("full"):
        return "Full"
    if "medium" in t:
        return "Medium"
    if "varies" in t:
        return "Medium"
    return None


def count_from(text):
    t = text.lower()
    m = re.search(r"(\d+)\s*packs? of\s*(\d+)", t)  # "8 Packs of 5"
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"(box|pack|bundle|sampler|chest|cedar chest|tin|jar|cabinet|carton)\s+of\s+(\d+)", t)
    if m:
        return int(m.group(2))
    m = re.search(r"(\d+)\s*(pk|pack|-pack|ct|count)\b", t)
    if m:
        return int(m.group(1))
    return None


def pack_label(text):
    m = re.search(r"((?:\d+\s+)?(?:Box|Pack|Bundle|Sampler|Cedar Chest|Chest|Tin|Jar|Cabinet)s?\s+of\s+\d+)", text, re.I)
    return m.group(1).strip() if m else None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def save_debug(name, html):
    DEBUG.mkdir(parents=True, exist_ok=True)
    (DEBUG / f"{name}.html").write_text(html[:600000], encoding="utf-8")


# ── Product cache: strength / made / flavored, learned from product pages ──
cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
lookups = 0


def product_info(url):
    """Strength and type from a product page, cached forever per URL."""
    global lookups
    if url in cache:
        return cache[url]
    if lookups >= MAX_PRODUCT_LOOKUPS:
        return None
    lookups += 1
    html = get(url)
    time.sleep(1.0)
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    info = {"strength": None, "machine": False, "flavored": False}
    m = re.search(r"Strength\s*:?\s*(Mild\s*-\s*Medium|Mellow\s*-\s*Medium|Medium\s*-\s*Full|Medium to Full|Mild|Mellow|Medium|Full|Varies)", text, re.I)
    if m:
        info["strength"] = m.group(1)
    if re.search(r"Machine[\s-]Made", text, re.I) and not re.search(r"Hand[\s-]?Made", text, re.I):
        info["machine"] = True
    if re.search(r"Flavor\s*:?\s*(Coffee|Vanilla|Sweet|Infused|Bourbon|Rum|Cherry|Irish Cream|Honey)", text, re.I):
        info["flavored"] = True
    cache[url] = info
    return info


# ── Store parsers ─────────────────────────────────────────────────────────
def parse_famous(html, base):
    """Famous Smoke Shop (Magento listing pages)."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("li.product-item, div.product-item-info"):
        a = item.select_one("a.product-item-link") or item.select_one("a[href]")
        if not a:
            continue
        name = a.get_text(" ", strip=True)
        url = urljoin(base, a.get("href", ""))
        final = item.select_one('[data-price-type="finalPrice"]')
        old = item.select_one('[data-price-type="oldPrice"]')
        price = float(final["data-price-amount"]) if final and final.get("data-price-amount") else None
        msrp = float(old["data-price-amount"]) if old and old.get("data-price-amount") else None
        text = item.get_text(" ", strip=True)
        if price is None:
            m = re.search(r"Special Price\s*\$([\d,.]+)\s*\$([\d,.]+)", text)
            if m:
                price, msrp = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
        img = item.select_one("img[alt]")
        alt = img["alt"] if img else ""
        count = count_from(alt) or count_from(name)
        ends = None
        m = re.search(r"Sale ends\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text)
        if m:
            ends = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        out.append(dict(name=name, url=url, price=price, msrp=msrp, count=count,
                        pack=pack_label(alt) or (f"Pack of {count}" if count else None),
                        ends=ends, strength=None,
                        freeShipping="free shipping" in text.lower(),
                        perk=None))
    return out


def parse_cigarscom(html, base):
    """Cigars.com (Salesforce Commerce listing pages)."""
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    ends = None
    m = re.search(r"(?:expires|ends)\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", page_text, re.I)
    if m:
        y = int(m.group(3)); y = y + 2000 if y < 100 else y
        ends = f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    out = []
    tiles = soup.select(".product-tile, .product[data-pid], div.product")
    for t in tiles:
        links = [a for a in t.select("a[href*='/item/']")]
        if not links:
            continue
        url = urljoin(base, links[0]["href"])
        text = t.get_text(" ", strip=True)
        name = ""
        for a in links:
            s = a.get_text(" ", strip=True)
            if s and not s.lower().startswith("save") and len(s) > len(name):
                name = s
        name = re.split(r"\s+\d+(\.\d+)?\s*[×x]\s*\d+", name)[0].strip(" -")
        vit = re.search(r"(\d+(?:\.\d+)?\s*[×x]\s*\d+)", text)
        save = re.search(r"Save\s*\$([\d,.]+)", text)
        pm = re.search(r"((?:\d+\s+)?(?:Box|Pack|Bundle|Cedar Chest|Tin|Jar)s?\s+of\s+\d+[^$]*?)\s*-\s*\$([\d,.]+)", text, re.I)
        if not pm:
            prices = re.findall(r"\$([\d,]+\.\d{2})", text)
            if not prices:
                continue
            price = float(prices[-1].replace(",", ""))
            pack = pack_label(text)
        else:
            pack = pm.group(1).strip()
            price = float(pm.group(2).replace(",", ""))
        msrp = round(price + float(save.group(1).replace(",", "")), 2) if save else None
        st = re.search(r"STRENGTH:\s*(Mellow\s*-\s*Medium|Medium\s*-\s*Full|Mild|Mellow|Medium|Full)", text, re.I)
        promo = re.search(r"(Free [^!]*?)(?:!|\s+STRENGTH)", text)
        out.append(dict(name=name, url=url, price=price, msrp=msrp,
                        count=count_from(pack or text), pack=pack,
                        vitola=vit.group(1).replace("x", "×") if vit else None,
                        ends=ends, strength=st.group(1) if st else None,
                        freeShipping=False,
                        perk=promo.group(1).strip() if promo else None))
    return out


def parse_jsonld(html, base):
    """Fallback for any store that publishes schema.org Product data."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("itemListElement") or [data]
        for it in items:
            p = it.get("item", it) if isinstance(it, dict) else None
            if not isinstance(p, dict) or p.get("@type") != "Product":
                continue
            offers = p.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") or offers.get("lowPrice")
            if not price:
                continue
            name = p.get("name", "")
            out.append(dict(name=name, url=urljoin(base, p.get("url") or offers.get("url") or ""),
                            price=float(price), msrp=None, count=count_from(name),
                            pack=pack_label(name), ends=None, strength=None,
                            freeShipping=False, perk=None))
    return out


def parse_atlantic(html, base):
    """Atlantic Cigar (BigCommerce). Listing shows 'single - box' price ranges; we keep the single price."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("article.product-item"):
        t = card.select_one(".product-item-title")
        link = card.select_one("a.product-item-image[href], .product-item-title a[href], a[data-quick-shop][href]")
        if not t or not link:
            continue
        if card.select_one(".alert-error") and "out of stock" in card.select_one(".alert-error").get_text(" ", strip=True).lower():
            continue
        price_el = card.select_one(".price-without-tax, .price")
        prices = re.findall(r"\$([\d,]+\.\d{2})", price_el.get_text(" ", strip=True) if price_el else "")
        if not prices:
            continue
        single = float(prices[0].replace(",", ""))
        img = card.select_one("img")
        src = ""
        if img:
            srcset = img.get("data-srcset") or img.get("srcset") or ""
            m = re.search(r"(https://\S+/stencil/640w/\S+)\s", srcset) or re.search(r"(https://\S+)\s+\d+w", srcset)
            src = m.group(1) if m else (img.get("data-src") or img.get("src") or "")
        name = t.get_text(" ", strip=True)
        vit = re.search(r"\(([\d\s/.-]+x[\d\s/.-]+)\)", name)
        out.append(dict(name=re.sub(r"\s*\([^)]*\)\s*$", "", name), url=urljoin(base, link["href"]),
                        price=single, msrp=single, count=1, pack="Single cigar",
                        vitola=vit.group(1).replace("x", " × ") if vit else None,
                        ends=None, strength=None, freeShipping=False, perk=None,
                        image=src if src.startswith("http") else None))
    return out


def cigarscom_promo_pages():
    html = get("https://www.cigars.com/deals/")
    pages = ["https://www.cigars.com/deals/"]
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='trending-cigar-deals-and-discounts/']"):
            u = urljoin("https://www.cigars.com/", a["href"].split("?")[0])
            if u not in pages and "machine-made" not in u:
                pages.append(u)
    return pages[:12], html


SOURCES = [
    ("Cigars.com", "cc", parse_cigarscom, None),  # pages discovered at run time
    ("Famous Smoke Shop", "fs", parse_famous, [
        "https://www.famous-smoke.com/sale/cigar-clearance-sale/cigar-monster-deals?product_list_limit=60",
        "https://www.famous-smoke.com/sale/cigar-clearance-sale/cigar-monster-deals?p=2&product_list_limit=60",
        "https://www.famous-smoke.com/sale/top-cigar-deals/discount-cigar-deals-sales?product_list_limit=60",
    ]),
    ("JR Cigar", "jr", parse_jsonld, ["https://www.jrcigars.com/deals/best-cigar-deals/packs-and-samplers-sale/"]),
    ("Cigars International", "ci", parse_jsonld, ["https://www.cigarsinternational.com/department/deals.html"]),
    ("CigarPage", "cp", parse_jsonld, ["https://www.cigarpage.com/"]),
    ("Atlantic Cigar", "ac", parse_atlantic, [
        f"https://atlanticcigar.com/cigars/all-cigars/?sort=pricedesc&page={n}" for n in range(3, 9)
    ]),
]

BRANDS = ["Arturo Fuente", "Fuente", "Padron", "Padrón", "Oliva", "Montecristo", "Romeo y Julieta", "My Father",
          "Davidoff", "Ashton", "Diesel", "Macanudo", "CAO", "Alec Bradley", "Drew Estate", "Undercrown",
          "Liga Privada", "H. Upmann", "Rocky Patel", "Perdomo", "AJ Fernandez", "New World", "La Flor Dominicana",
          "Tatuaje", "Plasencia", "Camacho", "Punch", "Partagas", "La Gloria Cubana", "Hoyo de Monterrey",
          "Cohiba", "E.P. Carrillo", "Crowned Heads", "Foundation", "Joya de Nicaragua", "Aganorsa", "Avo",
          "Zino", "Diamond Crown", "Kristoff", "Espinosa", "Herrera Esteli", "Nub", "Cain", "Leaf by Oscar",
          "La Aroma de Cuba", "Don Pepin", "Southern Draw", "Dunbarton", "Warped", "Illusione", "Casa Fuente",
          "Gurkha", "San Lotano", "Bolivar", "Ramon Allones", "Saint Luis Rey", "Baccarat", "Kristoff"]


def split_brand(name):
    for b in sorted(BRANDS, key=len, reverse=True):
        if name.lower().startswith(b.lower()):
            rest = name[len(b):].strip(" -–")
            return b, rest or name
    parts = name.split()
    return (" ".join(parts[:2]), " ".join(parts[2:]) or name) if len(parts) > 2 else (name, name)


KNOWN_STRENGTH = [
    (r"padr[oó]n.*(1926|40th|80|black|pb)", "Full"), (r"padr[oó]n.*(family reserve|1964|50 years|44 years|no\\. 9[05])", "Medium"),
    (r"liga privada.*(h99|t52|no\\. ?9|flying pig|super ancho)", "Full"), (r"liga privada", "Medium-Full"),
    (r"davidoff.*(signature|grand cru|aniversario|millennium|royal|winston|late hour)", "Medium"),
    (r"davidoff.*(colorado|nicaragua|yamasa|escurio)", "Medium-Full"), (r"cohiba.*(royale|nicaragua)", "Full"),
    (r"cohiba", "Medium-Full"), (r"ashton.*(esg|vsg|symmetry)", "Medium-Full"), (r"la flor dominicana", "Full"),
    (r"(fuente.*opus|opusx)", "Full"), (r"arturo fuente.*(anejo|hemingway|don carlos)", "Medium"),
    (r"(atabey|byron|paul garmirian|diamond crown|meerapfel|alfonso)", "Medium"),
    (r"(plasencia|el septimo|tatuaje|my father|aj fernandez|oliva serie v)", "Medium-Full"),
]


def known_strength(name):
    for pat, st in KNOWN_STRENGTH:
        if re.search(pat, name, re.I):
            return st
    return None


def keep(d):
    name = d["name"]
    if SKIP_WORDS.search(name) and not ALLOW_WORDS.search(name):
        return False
    if not d.get("price") or not d.get("count") or not d.get("msrp"):
        return False
    if d["count"] > 1 and d["price"] > d["msrp"] * (1 - MIN_BOX_DISCOUNT):
        return False
    if d["msrp"] / d["count"] < MIN_MSRP_PER_CIGAR:
        return False
    if d["count"] == 1 and d["price"] > MAX_SINGLE_PRICE:
        return False
    if d.get("ends") and d["ends"] < TODAY.isoformat():
        return False
    return True


def main():
    prev = json.loads(OUT.read_text()) if OUT.exists() else {"deals": []}
    prev_by_id = {d["id"]: d for d in prev.get("deals", [])}
    found, sources = {}, []

    for store, prefix, parser, pages in SOURCES:
        log(f"\n== {store}")
        first_html = None
        if pages is None:
            pages, first_html = cigarscom_promo_pages()
        ok, raw = False, []
        for i, url in enumerate(pages):
            html = first_html if (i == 0 and first_html) else get(url)
            time.sleep(1.5)
            if not html:
                continue
            ok = True
            items = parser(html, url)
            log(f"  {len(items):3d} items  {url}")
            if i == 0:
                save_debug(prefix, html)
            raw += items
        kept = 0
        for r in raw:
            if not keep(r):
                continue
            strength = norm_strength(r.get("strength")) or known_strength(r["name"])
            if not strength:
                info = product_info(r["url"])
                if not info or info["machine"] or info["flavored"]:
                    continue
                strength = norm_strength(info["strength"])
            if not strength:
                continue
            brand, line = split_brand(r["name"])
            did = f"{prefix}-{slug(r['url'].rstrip('/').split('/')[-1].replace('.html', ''))}"
            old = prev_by_id.get(did, {})
            deal = dict(id=did, format="single" if r["count"] == 1 else "box", retailer=store, brand=brand, line=line,
                        vitola=r.get("vitola"), pack=r.get("pack"), count=r["count"],
                        price=round(r["price"], 2), msrp=round(r["msrp"], 2), strength=strength,
                        url=r["url"], freeShipping=bool(r.get("freeShipping")),
                        firstSeen=old.get("firstSeen", NOW_ISO), lastSeen=NOW_ISO)
            if r.get("ends"):
                deal["ends"] = r["ends"]
            if r.get("perk"):
                deal["perk"] = r["perk"]
            if r.get("image"):
                deal["image"] = r["image"]
            deal = {k: v for k, v in deal.items() if v is not None}
            found[did] = deal
            kept += 1
        sources.append({"name": store, "ok": ok and kept > 0, "count": kept, "seen": len(raw)})
        log(f"  kept {kept}")

    # Keep recent deals from stores that couldn't be read this run.
    failed = {s["name"] for s in sources if not s["ok"]}
    for did, d in prev_by_id.items():
        if did in found or d.get("retailer") not in failed:
            continue
        if d.get("ends") and d["ends"] < TODAY.isoformat():
            continue
        seen = datetime.strptime(d.get("lastSeen", NOW_ISO), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if NOW - seen < timedelta(hours=STALE_HOURS):
            found[did] = d

    deals = sorted(found.values(), key=lambda d: d["price"] / d["msrp"])[:MAX_DEALS]
    if not deals and prev.get("deals"):
        log("\nNo deals found at all this run; leaving deals.json unchanged.")
        sys.exit(0)

    OUT.write_text(json.dumps({"lastChecked": NOW_ISO, "sources": sources, "deals": deals},
                              indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    CACHE.write_text(json.dumps(cache, indent=1) + "\n")
    log(f"\nWrote {len(deals)} deals. Sources: {sources}")


if __name__ == "__main__":
    main()
