import hashlib
import hmac
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wa_agent import guards, whatsapp  # noqa: E402


def test_webhook_verification(monkeypatch):
    monkeypatch.setenv("WA_VERIFY_TOKEN", "secret-token")
    ok = {"hub.mode": "subscribe", "hub.verify_token": "secret-token", "hub.challenge": "12345"}
    assert whatsapp.verify_subscription(ok) == "12345"
    assert whatsapp.verify_subscription({**ok, "hub.verify_token": "wrong"}) is None


def test_signature_check(monkeypatch):
    monkeypatch.setenv("WA_APP_SECRET", "app-secret")
    body = b'{"object":"whatsapp_business_account"}'
    good = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert whatsapp.valid_signature(body, good)
    assert not whatsapp.valid_signature(body, "sha256=forged")
    assert not whatsapp.valid_signature(body, None)


def test_parse_text_and_button_events():
    t = whatsapp.parse_messages(whatsapp.sample_text_event("15550100101", "Sarah", "where is my order"))
    b = whatsapp.parse_messages(whatsapp.sample_button_event("15550100101", "Sarah", "btn_track", "Track my order"))
    assert t[0]["text"] == "where is my order" and t[0]["name"] == "Sarah"
    assert b[0]["button_id"] == "btn_track"


def test_status_updates_are_ignored():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.x", "status": "delivered"}]}}]}]}
    assert whatsapp.parse_messages(payload) == []


def test_button_payload_respects_whatsapp_limits():
    p = whatsapp.build_payload("1555", "Pick one", [("a", "A very long button title here"), ("b", "B"), ("c", "C"), ("d", "D")])
    buttons = p["interactive"]["action"]["buttons"]
    assert len(buttons) == 3 and all(len(x["reply"]["title"]) <= 20 for x in buttons)


def test_dry_run_writes_outbox(monkeypatch, tmp_path):
    monkeypatch.delenv("WA_TOKEN", raising=False)
    monkeypatch.setattr(whatsapp, "OUTBOX", tmp_path / "outbox.jsonl")
    assert whatsapp.send("1555", "hello")["dry_run"]
    assert json.loads((tmp_path / "outbox.jsonl").read_text())["payload"]["text"]["body"] == "hello"


def test_pii_masking():
    masked = guards.mask_pii("card 4111 1111 1111 1111 email sarah.m@example.com")
    assert "4111 1111" not in masked and "[card ****1111]" in masked and "s***@example.com" in masked


def test_non_card_numbers_are_not_masked():
    assert guards.mask_pii("tracking 1Z84A7W90312349902 order PK-10342") == "tracking 1Z84A7W90312349902 order PK-10342"


def test_ownership():
    order = {"phone": "15550100101"}
    assert guards.owns(order, "15550100101", None, None)
    assert not guards.owns(order, "15550100109", None, "sarah.m@example.com")
    assert guards.owns(order, "15550100109", "sarah.m@example.com", "sarah.m@example.com")


def test_dev_conversations_all_pass(tmp_path):
    sys.path.insert(0, str(ROOT / "eval"))
    import run_eval
    convs = json.loads((ROOT / "eval/dev_conversations.json").read_text())["conversations"]
    assert all(not run_eval.run_one(c, tmp_path)[0] for c in convs)
