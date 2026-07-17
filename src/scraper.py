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

# JS run inside the page to walk up from a product link to its card
# container, stopping as soon as a price appears in the accumulated text
# (rather than a fixed number of levels, which is more likely to either
# undershoot on deep DOMs or bleed into neighboring cards on flat ones).
# The regex pattern is passed in as a plain argument (not spliced into the
# source) so Playwright handles all the string escaping for us.
#
# We deliberately don't use el.innerText here: adjacent inline elements
# with no whitespace between them in the source (e.g. two <span>s back to
# back) get concatenated with no separator by innerText, which can run
# numbers together ("$19.99$39.99" + "50%" -> a stray "950%" match). Instead
# we walk individual text nodes and join them with newlines, so each
# element's text is always isolated on its own line.
_CLIMB_TO_CARD_JS = """
(el, args) => {
    const priceRe = new RegExp(args.pricePattern);

    function extractText(node) {
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        const lines = [];
        let t;
        while (t = walker.nextNode()) {
            const trimmed = t.textContent.trim();
            if (trimmed) lines.push(trimmed);
        }
        return lines.join('\\n');
    }

    let node = el;
    for (let i = 0; i < args.maxLevels; i++) {
        if (!node.parentElement) break;
        node = node.parentElement;
        if (priceRe.test(extractText(node))) break;
    }
    const img = node.querySelector('img');
    return {
        text: extractText(node),
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
    # decimals optional — Indian rupee prices routinely omit them
    return re.compile(rf'{symbol}\s?[\d,]+(?:\.\d{{1,2}})?')


def scrape_deals(domain: str = "amazon.com", max_deals: int = 20, headless: bool = True) -> list[Deal]:
    url = f"https://www.{domain}/deals"
    price_re = _price_regex(domain)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)  # let lazy-loaded tiles settle
            page.mouse.wheel(0, 3000)  # trigger scroll-triggered lazy loads
            page.wait_for_timeout(1500)
            return _extract_deals(page, domain, max_deals, price_re)
        finally:
            browser.close()


def _extract_deals(page, domain: str, max_deals: int, price_re: re.Pattern) -> list[Deal]:
    deals: dict[str, Deal] = {}
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

        context = anchor.evaluate(_CLIMB_TO_CARD_JS, {"maxLevels": 6, "pricePattern": price_re.pattern})
        card_text = context.get("text", "")
        image_url = context.get("image", "")

        full_url = href if href.startswith("http") else f"https://www.{domain}{href}"
        deals[asin] = Deal(
            asin=asin,
            title=_guess_title(card_text, anchor, price_re),
            url=full_url,
            image_url=image_url,
            current_price=_guess_current_price(card_text, price_re),
            original_price=_guess_original_price(card_text, price_re),
            discount_percent=_guess_discount(card_text),
        )

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


def _guess_current_price(text: str, price_re: re.Pattern) -> str:
    prices = price_re.findall(text)
    return prices[0] if prices else ""


def _guess_original_price(text: str, price_re: re.Pattern) -> Optional[str]:
    prices = price_re.findall(text)
    return prices[1] if len(prices) > 1 else None


def _guess_discount(text: str) -> Optional[int]:
    match = PERCENT_RE.search(text)
    return int(match.group(1)) if match else None
