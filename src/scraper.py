"""
Scrapes Amazon's deals page for products currently on discount.

IMPORTANT — read this before your first real run:
Amazon changes its page markup often, and this file was written without
being able to load the live site (network-sandboxed while building it).
So instead of hardcoding specific CSS classes / data-testid values —
which I can't verify and would likely be wrong by the time you read this —
the scraper locks onto the one thing that's stable: product links always
contain "/dp/<ASIN>" or "/gp/product/<ASIN>". It finds those links, then
climbs up the surrounding DOM until it sees a price, and reads the
title/price/discount/image out of that container.

This is more resilient than exact selectors, but it's still a heuristic.
If a run turns up 0 deals or the fields look wrong, see the README
troubleshooting section — you'll want to open the deals page in a real
browser, inspect a card, and adjust PRICE_RE / the climb logic below.
"""
import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright

ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
PRICE_RE = re.compile(r'\$[\d,]+\.\d{2}')
PERCENT_RE = re.compile(r'(\d{1,3})%')

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# JS run inside the page to walk up from a product link to its card
# container, stopping as soon as a price appears in the accumulated text
# (rather than a fixed number of levels, which is more likely to either
# undershoot on deep DOMs or bleed into neighboring cards on flat ones).
_CLIMB_TO_CARD_JS = """
(el, maxLevels) => {
    let node = el;
    for (let i = 0; i < maxLevels; i++) {
        if (!node.parentElement) break;
        node = node.parentElement;
        if (/\\$[\\d,]+\\.\\d{2}/.test(node.innerText || '')) break;
    }
    const img = node.querySelector('img');
    return {
        text: node.innerText || '',
        image: img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : ''
    };
}
"""


@dataclass
class Deal:
    asin: str
    title: str
    url: str
    image_url: str
    current_price: str
    original_price: Optional[str]
    discount_percent: Optional[int]


def scrape_deals(domain: str = "amazon.com", max_deals: int = 20, headless: bool = True) -> list[Deal]:
    url = f"https://www.{domain}/deals"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)  # let lazy-loaded tiles settle
            page.mouse.wheel(0, 3000)  # trigger scroll-triggered lazy loads
            page.wait_for_timeout(1500)
            return _extract_deals(page, domain, max_deals)
        finally:
            browser.close()


def _extract_deals(page, domain: str, max_deals: int) -> list[Deal]:
    deals: dict[str, Deal] = {}
    seen_content: set[str] = set() # Track unique visual layout
    
    cards = page.query_selector_all('[data-testid="deal-card"]')
    if not cards:
        cards = page.query_selector_all('div[class*="DealGridItem"]')

    for card in cards:
        if len(deals) >= max_deals:
            break

        anchor = card.query_selector('a[href*="/dp/"], a[href*="/gp/product/"]')
        if not anchor:
            continue

        href = anchor.get_attribute("href") or ""
        match = ASIN_RE.search(href)
        if not match:
            continue
            
        asin = match.group(1)
        if asin in deals:
            continue

        card_text = card.inner_text()
        img_element = card.query_selector('img')
        image_url = img_element.get_attribute("src") if img_element else ""

        # SAFETY FILTER: If we've already seen this exact image or discount layout in this run, skip it
        discount = _guess_discount(card_text)
        content_fingerprint = f"{image_url}_{discount}"
        if content_fingerprint in seen_content:
            continue

        full_url = href if href.startswith("http") else f"https://www.{domain}{href}"
        
        deals[asin] = Deal(
            asin=asin,
            title=_guess_title(card_text, anchor),
            url=full_url,
            image_url=image_url,
            current_price=_guess_current_price(card_text),
            original_price=_guess_original_price(card_text),
            discount_percent=discount,
        )
        seen_content.add(content_fingerprint)

    # Safety net fallback
    if not deals:
        anchors = page.query_selector_all('a[href*="/dp/"], a[href*="/gp/product/"]')
        for anchor in anchors:
            if len(deals) >= max_deals:
                break
            href = anchor.get_attribute("href") or ""
            match = ASIN_RE.search(href)
            if not match:
                continue
            asin = match.group(1)
            if asin in deals:
                continue

            context = anchor.evaluate(_CLIMB_TO_CARD_JS, 4)
            card_text = context.get("text", "")
            image_url = context.get("image", "")

            discount = _guess_discount(card_text)
            content_fingerprint = f"{image_url}_{discount}"
            if content_fingerprint in seen_content:
                continue

            full_url = href if href.startswith("http") else f"https://www.{domain}{href}"
            deals[asin] = Deal(
                asin=asin,
                title=_guess_title(card_text, anchor),
                url=full_url,
                image_url=image_url,
                current_price=_guess_current_price(card_text),
                original_price=_guess_original_price(card_text),
                discount_percent=discount,
            )
            seen_content.add(content_fingerprint)

    return list(deals.values())

def _guess_title(card_text: str, anchor) -> str:
    aria = anchor.get_attribute("aria-label") or anchor.get_attribute("title")
    if aria and aria.strip():
        return aria.strip()
    for line in card_text.split("\n"):
        line = line.strip()
        # skip empty lines, pure prices, and pure percent-off badges to find
        # something that looks like an actual product title
        if line and not PRICE_RE.fullmatch(line) and not PERCENT_RE.fullmatch(line):
            return line[:200]
    return "Amazon deal"


def _guess_current_price(text: str) -> str:
    prices = PRICE_RE.findall(text)
    return prices[0] if prices else ""


def _guess_original_price(text: str) -> Optional[str]:
    prices = PRICE_RE.findall(text)
    return prices[1] if len(prices) > 1 else None


def _guess_discount(text: str) -> Optional[int]:
    match = PERCENT_RE.search(text)
    return int(match.group(1)) if match else None
