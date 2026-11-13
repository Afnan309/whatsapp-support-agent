"""Safety and privacy guardrails for customer chats."""
from __future__ import annotations

import re

INJECTION = [r"ignore (all |any |the )?(previous|prior|above) (instructions|rules|prompts?)", r"system prompt",
             r"you are now", r"developer mode", r"act as (an? )?(admin|unrestricted|different)", r"jailbreak",
             r"(show|list|give) (me )?(all|every|other) (customers|orders|users)", r"\bdan\b"]


def is_injection(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in INJECTION)


def _luhn(num: str) -> bool:
    digits = [int(d) for d in num][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def find_card(text: str) -> bool:
    for m in re.finditer(r"\b(?:\d[ -]?){13,19}\b", text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def mask_pii(text: str) -> str:
    """Mask card numbers and emails before anything is logged."""
    def card(m):
        d = re.sub(r"\D", "", m.group(0))
        return "[card ****" + d[-4:] + "]" if 13 <= len(d) <= 19 and _luhn(d) else m.group(0)
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", card, text)
    return re.sub(r"([\w.+-])[\w.+-]*@([\w-]+\.[\w.]+)", r"\1***@\2", text)


def owns(order: dict, phone: str, claimed_email: str | None, customer_email: str | None) -> bool:
    """Order details are only shared with the WhatsApp number that placed the order,
    or someone who gives the order's email address."""
    return order["phone"] == phone or (claimed_email is not None and claimed_email == customer_email)
