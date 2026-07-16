"""
Builds Amazon Associates affiliate links from plain product URLs.

Amazon affiliate links just need your tracking tag appended as a `tag=`
query param. We normalize to a clean /dp/ASIN/ URL first so links stay
short and don't leak Amazon's internal ref= tracking junk.
"""
import re
from urllib.parse import urlparse

ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')


def extract_asin(url: str) -> str | None:
    """Pull the 10-character ASIN out of an Amazon product URL, if present."""
    match = ASIN_RE.search(url)
    return match.group(1) if match else None


def build_affiliate_link(product_url: str, associate_tag: str, domain: str = "amazon.com") -> str:
    """
    Convert any Amazon product URL into a clean affiliate link carrying
    your Associates tag.

    Falls back to appending `tag=` onto the original URL if we can't
    confidently find an ASIN (better to have a working-but-messy link
    than to drop the deal).
    """
    if not associate_tag:
        raise ValueError("associate_tag is required to build an affiliate link")

    asin = extract_asin(product_url)
    if asin:
        return f"https://www.{domain}/dp/{asin}?tag={associate_tag}"

    parsed = urlparse(product_url)
    if not parsed.scheme:
        # not a real URL at all — nothing sensible to build
        return product_url

    separator = '&' if parsed.query else '?'
    return f"{product_url}{separator}tag={associate_tag}"
