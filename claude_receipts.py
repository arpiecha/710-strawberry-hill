"""Reading receipts with Claude.

SYSTEM_PROMPT and the shape of the Claude call are copied verbatim from the
original bot — they are tuned and working, so do not reword them.
"""

import base64
import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

CATEGORIES = ["Materials", "Labor", "Mortgage", "MISC"]


def _api_key() -> str:
    """Read the key when a receipt is actually analysed.

    Reading it at import time would crash-loop the whole service whenever the
    key is missing, taking the dashboard and every other route down with it.
    This way only receipt reading fails, and it says why.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set on this service")
    return key

SYSTEM_PROMPT = """You are a receipt analyzer for a home renovation project.
When given a receipt image, extract the following and respond ONLY with a JSON object (no markdown, no extra text):

{
  "store": "store name",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "type": "purchase or return",
  "category": "one of: Materials, Labor, Mortgage, MISC",
  "items": ["item1", "item2"],
  "notes": "brief note about what was bought"
}

Rules:
- type must be "purchase" or "return"
- category must match exactly one of: Materials, Labor, Mortgage, MISC
- If date not visible, use today's date
- If total not clear, use 0.00
- Items should be the main products purchased
- For returns, total should still be positive (type field indicates it's a return)

Category guide:
- Materials: any physical supplies, tools, hardware, tiles, lumber, paint, plumbing parts, electrical parts, etc.
- Labor: contractor payments, installation fees, service charges, any work done by a person
- Mortgage: mortgage payments, interest, property taxes, insurance related to the property
- MISC: anything that doesn't clearly fit above — permits, fees, cleaning, disposal, etc.
"""


def analyze_receipt(image_bytes: bytes, mime_type: str, categories: list[str] | None = None) -> dict:
    client = anthropic.Anthropic(api_key=_api_key())
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    logger.info("Sending image to Claude, size: %s bytes, mime: %s", len(image_bytes), mime_type)

    # The categories are editable now. Rather than reword the tuned prompt, the
    # current list rides along with the image when it differs from the default.
    ask = "Analyze this receipt."
    if categories and categories != CATEGORIES:
        ask += (" The categories have changed: put it in exactly one of "
                f"{', '.join(categories)} — use these instead of the ones in the "
                "instructions above, and pick the closest fit.")
    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": ask}
            ]
        }]
    )
    # Current models think before answering, so the first block is a thinking
    # block, not the answer. The old bot read content[0].text, which raises
    # against any thinking-capable model — take the first text block instead.
    raw = next((b.text for b in message.content if b.type == "text"), None)
    if raw is None:
        raise RuntimeError(f"Claude returned no text block (stop_reason={message.stop_reason})")
    logger.info("Claude response: %s", raw)
    text = raw.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)
