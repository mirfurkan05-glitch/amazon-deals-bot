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
browser, inspect a card, and adjust CURRENCY_SYMBOL / the climb logic below.
"""
import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import sync_playwright

ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
PERCENT_RE = re.compile(r'(\d{1,3})%')

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Amazon shows prices in the local currency symbol, and NOT always with
# decimals — amazon.in commonly shows whole-rupee amounts with no paise
# at all, e.g. "₹1,299" rather than "₹1,299.00". Add more marketplaces
# here as needed. Unlisted domains fall back to matching any common symbol.
CURRENCY_SYMBOL = {
    "amazon.com": r"\$",
    "amazon.in": r"₹",
    "amazon.co.uk": r"£",
    "amazon.ca": r"\$",
    "amazon.com.au": r"\$",
    "amazon.de": r"€",
    "amazon.fr": r"€",
    "amazon.it": r"€",
    "amazon.es": r"€",
}
FALLBACK_SYMBOL_CLASS = r"[₹$£€]"

# JS run inside the page to walk up from a product link to its card container...
_CLIMB_TO_CARD_JS = """
(el, args) => {
    const priceRe = new RegExp(args.pricePattern);

    function isStruckThrough(node) {
        let n = node;
        for (let i = 0; i < 6 && n; i++) {
            if (n.nodeType === 1) {
                const style = window.getComputedStyle(n);
                if (style.textDecorationLine && style.textDecorationLine.includes('line-through')) return true;
                const cls = n.className || '';
                if (typeof cls === 'string' && /strike|was-?price|line-?through/i.test(cls)) return true;
                const tag = n.tagName;
                if (tag === 'S' || tag === 'DEL' || tag === 'STRIKE') return true;
            }
            n = n.parentElement;
        }
        return false;
    }

    function isVisuallyHidden(node) {
        let n = node;
        for (let i = 0; i < 6 && n; i++) {
            if (n.nodeType === 1) {
                const style = window.getComputedStyle(n);
                if (style.position === 'absolute' && (style.width === '1px' || style.clip !== 'auto')) return true;
                const cls = n.className || '';
                if (typeof cls === 'string' && /offscreen|sr-only|visually-?hidden|screen-?reader/i.test(cls)) return true;
            }
            n = n.parentElement;
        }
        return false;
    }

    function extractLines(node) {
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        const lines = [];
        let t;
        while (t = walker.nextNode()) {
            const trimmed = t.textContent.trim();
            if (!trimmed) continue;
            lines.push({
                text: trimmed,
                struck: isStruckThrough(t.parentElement),
                hidden: isVisuallyHidden(t.parentElement)
            });
        }
        return lines;
    }

    let node = el;
    for (let i = 0; i < args.maxLevels; i++) {
        if (!node.parentElement) break;
        node = node.parentElement;
        const joined = extractLines(node).map(l => l.text).join('\\n');
        if (priceRe.test(joined)) break;
    }
    const lines = extractLines(node);
    const img = node.querySelector('img');
    return {
        text: lines.map(l => l.text).join('\\n'),
        struckFlags: lines.map(l => l.struck),
        hiddenFlags: lines.map(l => l.hidden),
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


def _price_regex(domain: str) -> re.Pattern:
    symbol = CURRENCY_SYMBOL.get(domain, FALLBACK_SYMBOL_CLASS)
    return re.compile(rf'{symbol}\s?[\d,]+(?:\.\d{{1,2}})?')


def scrape_deals(domain: str = "amazon.com", max_deals: int = 20, headless: bool = True) -> list[Deal]:
    url = f"https://www.{domain}/deals"
    price_re = _price_regex(domain)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
            page = context.new_page()
            
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"Amazon blocked the page load (Download Error): {e}")
                return [] 
                
            try:
                page.wait_for_selector('a[href*="/dp/"], a[href*="/gp/product/"]', timeout=20000)
            except Exception:
                pass
                
            page.wait_for_timeout(2500)  # let lazy-loaded tiles settle
            page.mouse.wheel(0, 3000)  # trigger scroll-triggered lazy loads
            page.wait_for_timeout(1500)
            
            return _extract_deals(page, domain, max_deals, price_re)
        finally:
            browser.close()


def _extract_deals(page, domain: str, max_deals: int, price_re: re.Pattern) -> list[Deal]:
    deals: dict[str, Deal] = {}
    seen_fingerprints: set[str] = set()

    anchors = page.query_selector_all('a[href*="/dp/"], a[href*="/gp/product/"]')
    
    for anchor in anchors:
        if len(deals) >= max_deals:
            break
            
        href = anchor.get_attribute("href") or ""
        match = ASIN_RE.search(href)
        if not match:
            continue
            
        asin = match.group(1)

        # Inject JavaScript to climb 4 levels up to the container
        context = anchor.evaluate(_CLIMB_TO_CARD_JS, {"maxLevels": 4, "pricePattern": price_re.pattern})
        
        card_text = context.get("text", "")
        image_url = context.get("image", "")
        struck_flags = context.get("struckFlags", [])
        hidden_flags = context.get("hiddenFlags", [])

        lines = card_text.split("\n")

        # Extract title and prices using our helper functions
        title = _guess_title(card_text, anchor, price_re)
        current_price, original_price = _extract_price_pair(lines, struck_flags, hidden_flags, price_re)

        # If we couldn't find a valid current price, skip this item
        if not current_price:
            continue
            
        discount_percent = _guess_discount(card_text, current_price, original_price)

        # THE FIX: Create a fingerprint using the first 30 characters of the title + the price. 
        # This completely ignores ASINs and Image URLs, stopping Amazon's duplicate trickery.
        fingerprint = f"{title[:30]}_{current_price}"

        if fingerprint in seen_fingerprints or asin in deals:
            continue

        full_url = href if href.startswith("http") else f"https://www.{domain}{href}"
        
        deals[asin] = Deal(
            asin=asin,
            title=title,
            url=full_url,
            image_url=image_url,
            current_price=current_price,
            original_price=original_price,
            discount_percent=discount_percent,
        )
        
        # Add the fingerprint to our tracking list
        seen_fingerprints.add(fingerprint)

    return list(deals.values())


def _guess_title(card_text: str, anchor, price_re: re.Pattern) -> str:
    aria = anchor.get_attribute("aria-label") or anchor.get_attribute("title")
    if aria and aria.strip():
        return aria.strip()
    for line in card_text.split("\n"):
        line = line.strip()
        if line and not price_re.fullmatch(line) and not PERCENT_RE.fullmatch(line):
            return line[:200]
    return "Amazon deal"


def _price_value(price_str: str) -> Optional[float]:
    """Parse '₹1,899.00' / '$19.99' -> 1899.0 / 19.99, for value comparison."""
    digits = re.sub(r"[^\d.]", "", price_str)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def _extract_price_pair(
    lines: list[str],
    struck_flags: list,
    hidden_flags: list,
    price_re: re.Pattern,
) -> tuple[str, Optional[str]]:
    best_by_key: dict[tuple, tuple] = {}
    for i, line in enumerate(lines):
        m = price_re.search(line)
        if not m:
            continue
        raw = m.group(0)
        value = _price_value(raw)
        if value is None:
            continue
        struck = bool(struck_flags[i]) if i < len(struck_flags) else False
        hidden = bool(hidden_flags[i]) if i < len(hidden_flags) else False
        key = (value, struck)
        existing = best_by_key.get(key)
        if existing is None or (existing[3] and not hidden):
            best_by_key[key] = (raw, value, struck, hidden)
    entries = list(best_by_key.values())

    if not entries:
        return "", None

    struck_entries = [e for e in entries if e[2]]
    plain_entries = [e for e in entries if not e[2]]

    current_candidates = [e for e in plain_entries if not e[3]] or plain_entries
    if current_candidates:
        current = min(current_candidates, key=lambda e: e[1])
    else:
        current = min(entries, key=lambda e: e[1])

    remaining = [e for e in entries if e[1] != current[1]]
    if not remaining:
        return current[0], None

    if struck_entries:
        original_pool = [e for e in struck_entries if e[1] != current[1]]
    else:
        original_pool = remaining

    if not original_pool:
        return current[0], None

    original = max(original_pool, key=lambda e: e[1])
    if original[1] <= current[1]:
        return current[0], None

    return current[0], original[0]


def _guess_discount(text: str, current_price: str, original_price: Optional[str]) -> Optional[int]:
    if not original_price:
        return None

    cur = _price_value(current_price)
    orig = _price_value(original_price)
    if cur is None or not orig or orig <= 0:
        return None
    computed = round((1 - cur / orig) * 100)
    if computed <= 0:
        return None

    match = PERCENT_RE.search(text)
    if match:
        stated = int(match.group(1))
        if abs(stated - computed) <= 3:
            return stated

    return computed
