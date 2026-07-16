"""
Entry point: scrape Amazon deals, filter out ones we've already posted,
post the rest to Telegram with your affiliate link, and save the updated
posted-list so future runs don't repeat them.

Run with DRY_RUN=true to see what *would* be posted without actually
hitting Telegram or updating data/posted_deals.json — always do this
before your first real run.
"""
import os
import sys
import time

from dotenv import load_dotenv

from affiliate import build_affiliate_link
from scraper import scrape_deals
from storage import load_posted, save_posted
from telegram_poster import post_deal

load_dotenv()


def main() -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    associate_tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    domain = os.environ.get("AMAZON_DOMAIN", "amazon.com")
    max_per_run = int(os.environ.get("MAX_DEALS_PER_RUN", "5"))
    min_discount = int(os.environ.get("MIN_DISCOUNT_PERCENT", "0"))
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    missing = [
        name for name, val in [
            ("TELEGRAM_BOT_TOKEN", bot_token),
            ("TELEGRAM_CHANNEL_ID", channel_id),
            ("AMAZON_ASSOCIATE_TAG", associate_tag),
        ] if not val
    ]
    if missing and not dry_run:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")

    posted = load_posted()
    print(f"Loaded {len(posted)} previously-posted ASINs.")

    print(f"Scraping https://www.{domain}/deals ...")
    deals = scrape_deals(domain=domain, max_deals=30)
    print(f"Scraped {len(deals)} deal candidates.")

    if not deals:
        print(
            "No deals found. If this keeps happening, Amazon's page structure "
            "likely doesn't match this scraper's assumptions — see the README "
            "troubleshooting section."
        )
        return

    new_deals = [
        d for d in deals
        if d.asin not in posted
        and (d.discount_percent is None or d.discount_percent >= min_discount)
    ][:max_per_run]
    print(f"{len(new_deals)} new deal(s) to post (min discount {min_discount}%).")

    posted_count = 0
    for deal in new_deals:
        affiliate_url = build_affiliate_link(deal.url, associate_tag or "placeholder-tag", domain)

        if dry_run:
            print(f"[DRY RUN] {deal.title}")
            print(f"           {deal.current_price} (was {deal.original_price}) -> {affiliate_url}")
            posted.add(deal.asin)
            posted_count += 1
            continue

        try:
            post_deal(bot_token, channel_id, deal, affiliate_url)
            print(f"Posted: {deal.title}")
            posted.add(deal.asin)
            posted_count += 1
            time.sleep(2)  # stay well under Telegram's rate limits
        except Exception as e:
            print(f"Failed to post '{deal.title}': {e}")
            # not marked as posted -> will be retried on the next run

    if not dry_run and posted_count:
        save_posted(posted)
        print(f"Saved updated posted-deals log ({len(posted)} total).")
    elif dry_run:
        print(f"[DRY RUN] Would have posted {posted_count} deal(s). Nothing was sent or saved.")


if __name__ == "__main__":
    main()
