"""
Posts a formatted deal to a Telegram channel via the Bot API.

No SDK needed — this is just two plain HTTP calls (sendPhoto / sendMessage),
which keeps the dependency list short.
"""
import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def escape_html(text: str) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_caption(deal, affiliate_url: str) -> str:
    lines = [f"🔥 <b>{escape_html(deal.title)}</b>", ""]

    if deal.discount_percent and deal.current_price:
        # This line now only posts the current price and the discount percentage
        lines.append(
            f"<b>{escape_html(deal.current_price)}</b> "
            f"({deal.discount_percent}% off)"
        )
    elif deal.current_price:
        lines.append(f"<b>{escape_html(deal.current_price)}</b>")

    lines.append("")
    lines.append(f'<a href="{affiliate_url}">⚡ Claim this discount on Amazon</a>')
    return "\n".join(lines)


def post_deal(bot_token: str, channel_id: str, deal, affiliate_url: str) -> dict:
    caption = format_caption(deal, affiliate_url)

    if deal.image_url:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token, method="sendPhoto"),
            data={
                "chat_id": channel_id,
                "caption": caption,
                "parse_mode": "HTML",
                "photo": deal.image_url,
            },
            timeout=15,
        )
        # Telegram can fail to fetch some hotlinked images (referer checks,
        # expired URLs, etc). Fall back to a text message so the deal still
        # gets posted instead of silently dropping it.
        if not resp.ok or not resp.json().get("ok", False):
            resp = _send_text(bot_token, channel_id, caption)
    else:
        resp = _send_text(bot_token, channel_id, caption)

    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok", False):
        raise RuntimeError(f"Telegram API rejected the message: {result}")
    return result


def _send_text(bot_token: str, channel_id: str, caption: str) -> requests.Response:
    return requests.post(
        TELEGRAM_API.format(token=bot_token, method="sendMessage"),
        data={
            "chat_id": channel_id,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
