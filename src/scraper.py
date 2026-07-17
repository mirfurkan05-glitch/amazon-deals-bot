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
#
# We also tag each line with whether the text node's nearest ancestor is
# struck-through or visually hidden. Amazon marks the "was" price two
# different ways at once — a strikethrough span for sighted users, and a
# separate visually-hidden ("a-offscreen"-style) span carrying the same
# number again for screen readers — and both of those land in the card's
# text alongside the plain current price. Positional guessing ("2nd price
# found = original") can't tell those apart from a same-valued duplicate of
# the CURRENT price, which is what produced "was 1,899.00, now 1,899 (44%
# off)": the offscreen echo of the current price got mistaken for the
# original. Tagging strikethrough/hidden lets us identify the original
# price by how it's marked up, not by where it happens to sit in the text.
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
        // Parallel array instead of nested objects: keeps the Python side
        // simple (zip text.split('\\n') with this) and avoids relying on
        // JSON key order across the Playwright JS<->Python boundary.
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
    # decimals optional — Indian rupee prices routinely omit them
    return re.compile(rf'{symbol}\s?[\d,]+(?:\.\d{{1,2}})?')


def def _extract_deals(page, domain: str, max_deals: int, price_re=None) -> list[Deal]:
    url = f"https://www.{domain}/deals"
    price_re = _price_regex(domain)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
            page = context.new_page()
            # "networkidle" (waiting for zero in-flight requests for 500ms)
            # is a bad fit for a page like Amazon's /deals: it keeps
            # background requests going almost continuously -- analytics
            # beacons, ad pixels, recommendation widgets refreshing -- so on
            # a slower connection (shared CI runners in particular) it can
            # go the full timeout without ever going idle, even though the
            # actual deal content loaded within a few seconds. "domcontentloaded"
            # only waits for the initial HTML, which is faster and more
            # reliable, and then we wait for something concrete -- an actual
            # product link -- to confirm the page really did render deals,
            # rather than waiting for a network condition that may never occur.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"Amazon blocked the page load (Download Error): {e}")
                return []  # Gracefully return 0 deals and let the bot try again next time
            try:
                page.wait_for_selector('a[href*="/dp/"], a[href*="/gp/product/"]', timeout=20000)
            except Exception:
                # Page loaded but no product links showed up in time -- don't
                # crash the whole run over this. Fall through and let
                # _extract_deals return an empty list; main.py already
                # handles "0 deals found" as a normal (non-fatal) outcome,
                # and this run will just retry on the next schedule.
                pass
            page.wait_for_timeout(2500)  # let lazy-loaded tiles settle
            page.mouse.wheel(0, 3000)  # trigger scroll-triggered lazy loads
            page.wait_for_timeout(1500)
            return _extract_deals(page, domain, max_deals, price_re)
        finally:
            browser.close()


def _extract_deals(page, domain: str, max_deals: int) -> list[Deal]:
    deals: dict[str, Deal] = {}
    seen_fingerprints: set[str] = set() # Our new aggressive filter

    anchors = page.query_selector_all('a[href*="/dp/"], a[href*="/gp/product/"]')
    
    for anchor in anchors:
        if len(deals) >= max_deals:
            break
            
        href = anchor.get_attribute("href") or ""
        match = ASIN_RE.search(href)
        if not match:
            continue
            
        asin = match.group(1)

        context = anchor.evaluate(_CLIMB_TO_CARD_JS, 4)
        card_text = context.get("text", "")
        image_url = context.get("image", "")

        # Extract title and price early to check them
        title = _guess_title(card_text, anchor)
        current_price = _guess_current_price(card_text)

        # THE FIX: Create a "fingerprint" using the first 30 characters of the title + the price. 
        # This completely ignores ASINs and Image URLs, stopping Amazon's trickery.
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
            original_price=_guess_original_price(card_text),
            discount_percent=_guess_discount(card_text),
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
    """
    Find the current price and, if present, the original (pre-discount) price.

    Amazon typically renders the "was" price TWICE at once: a strikethrough
    span for sighted users, and a separate visually-hidden span repeating
    the same number for screen readers (a plain strikethrough has no
    semantic meaning to assistive tech, so it needs a text equivalent).
    Both of those end up as separate lines in the card's extracted text
    alongside the plain current price -- so a card with an active discount
    commonly has price text appearing three times: current, original
    (struck), original (hidden), not two. Treating "whichever price text
    comes second" as the original -- the previous behavior -- can grab the
    hidden echo of the CURRENT price instead, producing a "was 1,899.00,
    now 1,899 (44% off)" pair that's really the same price rendered twice.

    A separate, related issue this also has to handle: a real, VISIBLE
    struck-through price that just happens to equal the current price (e.g.
    Amazon showing an MRP-strikethrough out of habit on a listing with no
    actual discount) shouldn't count as an original price either -- an
    "original" that isn't actually different from the current price isn't
    a real discount pair, whether the duplicate is hidden or visible.

    Strategy:
      1. Collect every (value, is_struck, is_hidden) price found in the
         card, deduping by numeric value+struck-state so the visible and
         hidden renderings of the SAME price collapse into one entry
         instead of being treated as two different prices.
      2. Current price = the one price that's neither struck-through nor
         hidden-only (or the smallest deduped value, when everything is
         plain and there's no discount markup at all).
      3. Original price = a struck-through value that DIFFERS from the
         current price. If nothing is marked as struck-through (some
         layouts may only expose the hidden copy, or none), fall back to
         "largest remaining distinct value", since the pre-discount price
         is always >= the current price -- position in the text is NOT
         used as a signal, since that's exactly what broke before. Either
         way, a same-valued "original" is never reported.
    """
    # (value, struck) -> best entry seen so far for that combination.
    # "Best" means visible over hidden: when a price's value+struck-state
    # collides with one we've already recorded, we only replace the kept
    # entry if the new one is visible and the kept one was hidden-only.
    # This makes the dedupe order-independent -- otherwise, if the hidden
    # a11y echo happens to appear in the DOM before the visible price, a
    # simple "first one wins" dedupe would keep the hidden one and drop the
    # visible one, which is just the same bug moved to a different spot.
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

    # Current price: prefer a plain (non-struck) entry that isn't ONLY a
    # hidden duplicate; a card always renders the current price visibly
    # somewhere, so a visible-and-plain entry should exist when there IS
    # a discount. Without one, just take the smallest plain value.
    current_candidates = [e for e in plain_entries if not e[3]] or plain_entries
    if current_candidates:
        current = min(current_candidates, key=lambda e: e[1])
    else:
        # Nothing plain at all (unusual) -- fall back to the smallest
        # overall value rather than returning nothing.
        current = min(entries, key=lambda e: e[1])

    remaining = [e for e in entries if e[1] != current[1]]
    if not remaining:
        return current[0], None

    if struck_entries:
        original_pool = [e for e in struck_entries if e[1] != current[1]]
    else:
        # No strikethrough markup detected on this layout -- fall back to
        # "largest distinct value", since a real original price can only
        # ever be >= the current price. This still avoids the old bug:
        # a same-valued hidden echo of the current price was already
        # excluded by the value-based dedupe above, so it can't win here.
        original_pool = remaining

    if not original_pool:
        return current[0], None

    original = max(original_pool, key=lambda e: e[1])
    if original[1] <= current[1]:
        # Safety net: an "original" that isn't actually higher than the
        # current price isn't a real discount pair -- don't report one.
        return current[0], None

    return current[0], original[0]


def _guess_discount(text: str, current_price: str, original_price: Optional[str]) -> Optional[int]:
    """
    Figure out the discount percentage to show, if any.

    IMPORTANT: this must stay consistent with whatever _extract_price_pair
    decided, not just search the card's raw text in isolation. Amazon cards
    frequently carry a percent-off badge ("46% off", a 🔥 ribbon, etc.) as a
    generic promotional element that can be present even when there's no
    genuine gap between current and original price on THIS listing -- e.g.
    a coupon or bank-offer badge, or an MRP-strikethrough that Amazon shows
    out of habit even though MRP happens to equal the selling price. If we
    trust that badge text unconditionally, we can end up captioning a post
    with "was ₹1,290.00, now ₹1,290 (46% off)" -- a real percent number,
    just not one that describes an actual price difference on this card.

    So: if we don't have a confirmed original_price (i.e.
    _extract_price_pair already decided there's no real discount pair --
    including cases where a struck-through price exists but equals the
    current price), we don't report ANY discount percentage, even if the
    text contains one. A percent badge with no corresponding price gap
    isn't something this bot can respond to responsibly, since we have no
    way to tell a real per-item discount apart from a bank-offer/coupon
    promo without visiting the product page ourselves.
    """
    if not original_price:
        return None

    cur = _price_value(current_price)
    orig = _price_value(original_price)
    if cur is None or not orig or orig <= 0:
        return None
    computed = round((1 - cur / orig) * 100)
    if computed <= 0:
        return None

    # If the card also has explicit "X% off" text AND it's reasonably close
    # to the price-derived number, prefer the card's own stated figure
    # (Amazon sometimes rounds slightly differently than a raw price-ratio
    # calculation would). "Reasonably close" is deliberately tight -- this
    # is a sanity check the two numbers roughly agree, not a way to let an
    # unrelated badge override a real price-derived computation.
    match = PERCENT_RE.search(text)
    if match:
        stated = int(match.group(1))
        if abs(stated - computed) <= 3:
            return stated

    return computed
