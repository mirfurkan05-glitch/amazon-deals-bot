"""
Quick regression test for the affiliate link builder.
Run with: python tests/test_affiliate.py
(plain asserts, no pytest dependency required)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from affiliate import build_affiliate_link, extract_asin  # noqa: E402

CASES = [
    ("https://www.amazon.com/Echo-Dot/dp/B09B8V1LZ3/ref=sr_1_3?keywords=echo", "B09B8V1LZ3"),
    ("https://www.amazon.com/dp/B09B8V1LZ3", "B09B8V1LZ3"),
    ("https://www.amazon.com/gp/product/B09B8V1LZ3", "B09B8V1LZ3"),
    ("/dp/B09B8V1LZ3/ref=abc", "B09B8V1LZ3"),
]

for url, expected_asin in CASES:
    got = extract_asin(url)
    assert got == expected_asin, f"{url} -> expected {expected_asin}, got {got}"

    link = build_affiliate_link(url, "mytag-20")
    assert link == f"https://www.amazon.com/dp/{expected_asin}?tag=mytag-20", link

try:
    build_affiliate_link("https://www.amazon.com/dp/B09B8V1LZ3", "")
    raise AssertionError("empty tag should have raised ValueError")
except ValueError:
    pass

print(f"All {len(CASES)} affiliate link tests passed.")
