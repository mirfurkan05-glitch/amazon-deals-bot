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

_CLIMB_TO_CARD_JS = """(el, args) => {
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
        const joined = extractLines(node).map(l => l.text).join('\n');
        if (priceRe.test(joined)) break;
    }
    const lines = extractLines(node);
    const img = node.querySelector('img');
    return {
        text: lines.map(l => l.text).join('\n'),
        struckFlags: lines.map(l => l.struck),
        hiddenFlags: lines.map(l => l.hidden),
        image: img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : ''
    };
}"""

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
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector('a[href*="/dp/"], a[href*="/gp/product/"]', timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
            return _extract_deals(page, domain, max_deals, price_re)
        finally:
            browser.close()

def _normalize_title(title: str) -> str:
    """Normalize title to help collapse color/size variants into a single deal."""
    title = re.sub(r'\s*\([^)]*\)$', '', title)
    title = re.sub(r'^(?:[🔥💥⚡]|Deal of the Day[:\-]?\s*)+', '', title, flags=re.IGNORECASE)
    return title.strip().lower()

def _extract_deals(page, domain: str, max_deals: int, price_re: re.Pattern) -> list[Deal]:
    deals: dict[str, Deal] = {}
    seen_variants: set[tuple] = set()
    
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
        struck_flags = context.get("struckFlags", [])
        hidden_flags = context.get("hiddenFlags", [])
        image_url = context.get("image", "")
        
        lines = card_text.split("\n")
        current_price, original_price = _extract_price_pair(lines, struck_flags, hidden_flags, price_re)
        
        title = _guess_title(card_text, anchor, price_re)
        discount = _guess_discount(card_text, current_price, original_price)
        
        # Variant dedup logic
        norm_title = _normalize_title(title)
        variant_key = (norm_title, current_price, discount)
        if variant_key in seen_variants:
            continue
        seen_variants.add(variant_key)
        
        full_url = href if href.startswith("http") else f"https://www.{domain}{href}"
        deals[asin] = Deal(
            asin=asin,
            title=title,
            url=full_url,
            image_url=image_url,
            current_price=current_price,
            original_price=original_price,
            discount_percent=discount,
        )
    return list(deals.values())

def _guess_title(card_text: str, anchor, price_re: re.Pattern) -> str:
    aria = anchor.get_attribute("aria-label") or anchor.get_attribute("title")
    if aria and aria.strip():
        return aria.strip()
    
    candidates = []
    for line in card_text.split("\n"):
        line = line.strip()
        if not line or price_re.fullmatch(line) or PERCENT_RE.fullmatch(line):
            continue
        
        # Hardened fallback: skip short lines that look like promo text or color swatches
        if len(line) < 15 and any(kw in line.lower() for kw in ["off", "black", "white", "blue", "red", "pink", "green"]):
            continue
            
        candidates.append(line)
        
    if candidates:
        # Prefer longer, actual product-name shaped lines over short fragments
        best_line = max(candidates, key=len)
        return best_line[:200]
        
    return "Amazon deal"

def _price_value(price_str: str) -> Optional[float]:
    digits = re.sub(r"[^\d.]", "", price_str)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None

def _extract_price_pair(lines: list[str], struck_flags: list, hidden_flags: list, price_re: re.Pattern) -> tuple[str, Optional[str]]:
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
