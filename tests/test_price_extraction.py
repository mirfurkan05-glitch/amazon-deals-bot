"""
Regression tests for the price-pair extraction logic in scraper.py.

These test the pure-Python helpers directly (_extract_price_pair,
_guess_discount, _price_value) with hand-built (lines, struck_flags,
hidden_flags) tuples that mimic exactly what the Playwright JS climb
hands back -- no real browser needed, since none of this logic touches
the page once the text/flags exist.

Run with: python tests/test_price_extraction.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scraper import _extract_price_pair, _guess_discount, _price_value, _price_regex  # noqa: E402

INR = _price_regex("amazon.in")
USD = _price_regex("amazon.com")


def check(name, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"[{status}] {name}")
    print(f"       expected: {expected!r}")
    print(f"       got:      {got!r}")
    if status == "FAIL":
        raise AssertionError(name)


# --- Case 1: the exact bug from the screenshot -----------------------------
# "1,899 -> 1,899.00 (44% off)" was really: current price shown plainly as
# "₹1,899", the SAME price echoed again in a hidden a11y span as "₹1,899.00"
# (different string, same numeric value -- this is why naive string dedupe
# wouldn't have caught it either), and a real original price of ₹3,391
# struck through elsewhere on the card. Old code took price[0]=current,
# price[1]=whatever came second in the text, which was the hidden echo.
lines_1 = [
    "Wireless Mouse",
    "₹1,899",          # current price, plain/visible
    "₹1,899.00",       # hidden a11y echo of the SAME price, different formatting
    "₹3,391",          # real original price, struck through
    "44% off",
]
struck_1 = [False, False, False, True, False]
hidden_1 = [False, False, True, False, False]
cur, orig = _extract_price_pair(lines_1, struck_1, hidden_1, INR)
check("Case 1 current price", cur, "₹1,899")
check("Case 1 original price", orig, "₹3,391")
check("Case 1 discount", _guess_discount("44% off", cur, orig), 44)

# --- Case 2: same bug, but the hidden echo comes BEFORE the visible price --
# order shouldn't matter now, since we no longer rely on position at all.
lines_2 = [
    "Wireless Mouse",
    "₹1,899.00",       # hidden echo first this time
    "₹1,899",          # visible current price second
    "₹3,391",
    "44% off",
]
struck_2 = [False, False, False, True, False]
hidden_2 = [False, True, False, False, False]
cur, orig = _extract_price_pair(lines_2, struck_2, hidden_2, INR)
check("Case 2 current price (order-independent)", cur, "₹1,899")
check("Case 2 original price (order-independent)", orig, "₹3,391")

# --- Case 3: no strikethrough markup detected on this layout ---------------
# Falls back to "largest distinct value = original", but the value-based
# dedupe still protects it from pairing the current price with its own echo.
lines_3 = ["USB-C Cable", "$9.99", "$9.99", "$19.99"]
struck_3 = [False, False, False, False]  # nothing flagged as struck this time
hidden_3 = [False, False, True, False]
cur, orig = _extract_price_pair(lines_3, struck_3, hidden_3, USD)
check("Case 3 current price (no strike markup)", cur, "$9.99")
check("Case 3 original price (falls back to largest distinct value)", orig, "$19.99")

# --- Case 4: plain product, no discount at all ------------------------------
lines_4 = ["Notebook", "$4.99"]
struck_4 = [False, False]
hidden_4 = [False, False]
cur, orig = _extract_price_pair(lines_4, struck_4, hidden_4, USD)
check("Case 4 current price (no discount)", cur, "$4.99")
check("Case 4 original price (no discount)", orig, None)

# --- Case 5: the earlier "950%" concatenation bug is still fixed -----------
# innerText used to run "$19.99$39.99" + "50%" together into "950%". Since
# the text-node walker now isolates each element onto its own line, the
# percent regex should only ever see a clean "50%", never a corrupted one.
lines_5 = ["Desk Lamp", "$19.99", "$39.99", "50%"]
struck_5 = [False, False, True, False]
hidden_5 = [False, False, False, False]
cur, orig = _extract_price_pair(lines_5, struck_5, hidden_5, USD)
check("Case 5 current price", cur, "$19.99")
check("Case 5 original price", orig, "$39.99")
check("Case 5 discount is clean, not corrupted", _guess_discount("50%", cur, orig), 50)

# --- Case 6: two DIFFERENT products should never merge into one deal -------
# (Sanity check on _price_value / dedupe: a card with three distinct real
# prices -- e.g. a "frequently bought together" style card -- shouldn't
# collapse anything it shouldn't.)
v = _price_value("₹1,899.00")
check("Case 6 _price_value strips currency/commas correctly", v, 1899.0)
v2 = _price_value("₹1,899")
check("Case 6 _price_value handles missing decimals (India)", v2, 1899.0)
check("Case 6 both formats of the same price are numerically equal", v == v2, True)

print("\nAll price-extraction tests passed.")
