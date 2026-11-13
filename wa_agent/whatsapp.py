"""WhatsApp Cloud API glue: webhook verification, payload parsing and outbound messages.

Environment:
  WA_VERIFY_TOKEN      token you set in the Meta app dashboard for webhook verification
  WA_APP_SECRET        app secret, used to check the X-Hub-Signature-256 header
  WA_TOKEN             access token for sending messages
  WA_PHONE_NUMBER_ID   the business phone number id
Without WA_TOKEN the sender runs in dry-run mode and writes payloads to data/outbox.jsonl.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUTBOX = ROOT / "data" / "outbox.jsonl"
GRAPH = "https://graph.facebook.com/v21.0"


def verify_subscription(args: dict) -> str | None:
    if args.get("hub.mode") == "subscribe" and args.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN", "dev-verify-token"):
        return args.get("hub.challenge")
    return None


def valid_signature(raw_body: bytes, header: str | None) -> bool:
    secret = os.getenv("WA_APP_SECRET")
    if not secret:
        return True                       # local development
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return bool(header) and hmac.compare_digest(expected, header)


def parse_messages(payload: dict) -> list[dict]:
    """Flatten incoming messages: [{'from', 'name', 'id', 'text', 'button_id'}]. Status updates are skipped."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                item = {"from": m["from"], "name": names.get(m["from"]), "id": m.get("id"), "text": "", "button_id": None}
                if m.get("type") == "text":
                    item["text"] = m["text"]["body"]
                elif m.get("type") == "interactive":
                    reply = m["interactive"].get("button_reply") or m["interactive"].get("list_reply") or {}
                    item["text"], item["button_id"] = reply.get("title", ""), reply.get("id")
                else:
                    item["text"] = f"[{m.get('type')} message]"
                out.append(item)
    return out


def build_payload(to: str, text: str, buttons: list[tuple[str, str]] | None = None) -> dict:
    if not buttons:
        return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text, "preview_url": True}}
    return {"messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {"type": "button", "body": {"text": text[:1024]},
                            "action": {"buttons": [{"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                                                   for bid, title in buttons[:3]]}}}


def send(to: str, text: str, buttons=None) -> dict:
    payload = build_payload(to, text, buttons)
    token, number_id = os.getenv("WA_TOKEN"), os.getenv("WA_PHONE_NUMBER_ID")
    if token and number_id:
        r = requests.post(f"{GRAPH}/{number_id}/messages", headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=15)
        return {"sent": r.ok, "status": r.status_code}
    OUTBOX.parent.mkdir(exist_ok=True)
    with OUTBOX.open("a") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "payload": payload}) + "\n")
    return {"sent": False, "dry_run": True}


def sample_text_event(phone: str, name: str, text: str, msg_id: str = "wamid.TEST") -> dict:
    """Builds a webhook payload in the Cloud API shape, for tests and the simulator."""
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": "PHONE_ID"},
        "contacts": [{"profile": {"name": name}, "wa_id": phone}],
        "messages": [{"from": phone, "id": msg_id, "timestamp": "1762600000", "type": "text", "text": {"body": text}}]}}]}]}


def sample_button_event(phone: str, name: str, button_id: str, title: str) -> dict:
    p = sample_text_event(phone, name, "")
    p["entry"][0]["changes"][0]["value"]["messages"][0] = {
        "from": phone, "id": "wamid.BTN", "timestamp": "1762600000", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}}}
    return p
