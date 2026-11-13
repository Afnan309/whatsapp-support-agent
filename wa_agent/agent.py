"""Conversation manager for the WhatsApp support assistant.

Each WhatsApp number has a session with a small state machine (waiting for an order number, a return
reason, a confirmation...). Replies can carry quick-reply buttons, which map to WhatsApp interactive messages.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import guards, nlu
from .kb import KnowledgeBase
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
BRAND = "Peakline Outdoor"
MAIN_MENU = [("btn_track", "Track my order"), ("btn_return", "Start a return"), ("btn_human", "Talk to a person")]
REASONS = [("reason_size", "Wrong size"), ("reason_defect", "Damaged / defective"), ("reason_mind", "Changed my mind")]


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Session:
    phone: str
    state: str = "idle"          # idle | need_order | need_email | need_reason | confirm_return | confirm_cancel | human
    action: str | None = None    # track | return | cancel
    order_id: str | None = None
    reason: str | None = None
    verified: set = field(default_factory=set)
    offered_human: bool = False
    events: list = field(default_factory=list)


def _d(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%a, %b ") + str(date.fromisoformat(iso).day) if iso else "soon"


def support_open(now: datetime) -> bool:
    if now.weekday() < 5:
        return 9 <= now.hour < 18
    return now.weekday() == 5 and 10 <= now.hour < 16


class SupportAgent:
    def __init__(self, store: Store, log_path: Path = ROOT / "data" / "chats.db"):
        self.store, self.kb = store, KnowledgeBase()
        self.sessions: dict[str, Session] = {}
        self.log = sqlite3.connect(str(log_path), check_same_thread=False)
        self.log.execute("""CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, phone TEXT, direction TEXT,
                            text TEXT, buttons TEXT, meta TEXT, ts TEXT)""")
        self.log.execute("CREATE TABLE IF NOT EXISTS sessions(phone TEXT PRIMARY KEY, data TEXT)")
        self.log.commit()

    # ------------------------------------------------------------------ logging
    def _log(self, phone, direction, text, buttons=None, meta=None, now=None):
        self.log.execute("INSERT INTO messages(phone,direction,text,buttons,meta,ts) VALUES (?,?,?,?,?,?)",
                         (phone, direction, guards.mask_pii(text), json.dumps(buttons or []), json.dumps(meta or {}),
                          (now or datetime.now()).isoformat(timespec="seconds")))
        self.log.commit()

    def session(self, phone: str) -> Session:
        """Sessions are persisted, so a handoff or a half-finished return survives a restart."""
        if phone not in self.sessions:
            row = self.log.execute("SELECT data FROM sessions WHERE phone=?", (phone,)).fetchone()
            if row:
                d = json.loads(row[0])
                d["verified"] = set(d["verified"])
                self.sessions[phone] = Session(**d)
            else:
                self.sessions[phone] = Session(phone)
        return self.sessions[phone]

    def _save(self, s: Session) -> None:
        d = {**s.__dict__, "verified": sorted(s.verified)}
        self.log.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (s.phone, json.dumps(d)))
        self.log.commit()

    # ------------------------------------------------------------------ entry point
    def handle(self, phone: str, text: str, now: datetime | None = None, button_id: str | None = None) -> list[Reply]:
        now = now or datetime.now()
        s = self.session(phone)
        label, conf = nlu.intent(text) if not button_id else ("button", 1.0)
        self._log(phone, "in", text, meta={"intent": label, "confidence": round(conf, 2), "button": button_id}, now=now)
        replies = self._respond(s, phone, text, button_id, label, now)
        s.offered_human = any(b == "btn_human" for r in replies for b, _ in r.buttons) and s.state != "human"
        for r in replies:
            self._log(phone, "out", r.text, [t for _, t in r.buttons], {"state": s.state}, now=now)
        self._save(s)
        return replies

    # ------------------------------------------------------------------ brain
    def _respond(self, s: Session, phone, text, button_id, label, now) -> list[Reply]:
        cust = self.store.customer(phone)
        first = cust["name"].split()[0] if cust else "there"

        if s.state == "human":
            return [] if s.events and s.events[-1] == "waiting for agent" else self._waiting(s)
        if guards.find_card(text):
            s.events.append("card number detected and masked")
            return [Reply("For your security, please don't share card numbers here. I've hidden it from our records. "
                          "We never ask for payment details on WhatsApp.")]
        if guards.is_injection(text):
            s.events.append("prompt injection blocked")
            return [Reply("I can only help with your own orders, returns and our store policies.", MAIN_MENU)]
        if nlu.frustration(text) >= 0.6 and s.state != "human":
            return self._handoff(s, phone, "customer frustrated", text, now,
                                 lead="I'm really sorry about this. I don't want to make it worse with a bot. ")

        # buttons
        if button_id:
            if button_id == "btn_track":
                label = "track_order"
            elif button_id == "btn_return":
                label = "return_request"
            elif button_id == "btn_human":
                label = "human_handoff"
            elif button_id.startswith("order:"):
                s.order_id = button_id.split(":", 1)[1]
                return self._continue(s, phone, now)
            elif button_id.startswith("reason_") and s.state == "need_reason":
                s.reason = dict(REASONS)[button_id]
                return self._confirm_return(s)
            elif button_id in ("yes", "no"):
                text = button_id

        if s.offered_human and nlu.yes_no(text) is True:
            return self._handoff(s, phone, "customer accepted offer of a person", text, now)
        s.offered_human = False

        # pending conversation states
        if s.state in ("need_order", "need_email"):
            oid, em = nlu.order_id(text), nlu.email(text)
            if oid:
                s.order_id = oid
            if em and s.order_id:
                o = self.store.order(s.order_id)
                owner = self.store.customer(o["phone"]) if o else None
                if owner and owner["email"] == em:
                    s.verified.add(s.order_id)
                    s.events.append(f"verified {s.order_id} by email")
            if em and s.state == "need_email" and s.order_id not in s.verified:
                s.events.append(f"email did not match {s.order_id}")
                self._reset(s)
                return [Reply("That email doesn't match our records for this order, so I can't share its details. "
                              "If you think this is a mistake, our team can help.", [("btn_human", "Talk to a person")])]
            if oid or em:
                return self._continue(s, phone, now)
            if label not in ("human_handoff", "policy_question", "other"):
                return [Reply("Could you send the order number? It looks like PK-10342 and is in your confirmation email.")]
        if s.state == "need_reason":
            s.reason = text.strip()[:120]
            return self._confirm_return(s)
        if s.state in ("confirm_return", "confirm_cancel"):
            yn = nlu.yes_no(text)
            if yn is True:
                return self._do_return(s) if s.state == "confirm_return" else self._do_cancel(s)
            if yn is False:
                self._reset(s)
                return [Reply("No problem, I've left the order as it is. Anything else I can help with?", MAIN_MENU)]

        # intents (a bare order number means "tell me about this order")
        if label in ("policy_question", "other", "greeting", "thanks") and nlu.order_id(text):
            label = "track_order"
        if label in ("track_order", "return_request", "cancel_order"):
            s.action = {"track_order": "track", "return_request": "return", "cancel_order": "cancel"}[label]
            s.order_id = nlu.order_id(text)
            if not s.order_id and cust:
                orders = [o for o in self.store.orders_for(phone) if o["status"] != "cancelled"]
                if s.action == "return":
                    orders = [o for o in orders if o["status"] == "delivered"]
                elif s.action == "cancel":
                    orders = [o for o in orders if o["status"] == "processing"] or orders
                else:
                    orders = [o for o in orders if o["status"] != "delivered"] or orders
                if len(orders) == 1:
                    s.order_id = orders[0]["id"]
                elif orders:
                    s.state = "need_order"
                    return [Reply(f"Sure, {first}. Which order is it about?",
                                  [(f"order:{o['id']}", f"{o['id']} {o['items'].split(',')[0]}"[:20]) for o in orders[:3]])]
            if not s.order_id:
                s.state = "need_order"
                return [Reply("Happy to help. What's your order number? It looks like PK-10342.")]
            return self._continue(s, phone, now)
        if label == "policy_question":
            return self._policy(text)
        if label == "human_handoff":
            return self._handoff(s, phone, "customer asked for a person", text, now)
        if label == "greeting":
            return [Reply(f"Hi {first}! I'm the {BRAND} assistant. I can track orders, start returns and answer questions "
                          "about shipping, sizing and warranties. What can I do for you?", MAIN_MENU)]
        if label == "thanks":
            self._reset(s)
            return [Reply("You're welcome! Have a great day outside. 🏕️")]
        hits = self.kb.search(text)
        if hits and hits[0]["score"] > 0.25:
            return self._policy(text)
        return [Reply("I'm the store assistant, so I can help with orders, returns, shipping, sizing and warranties. "
                      "What would you like to do?", MAIN_MENU)]

    # ------------------------------------------------------------------ actions
    def _continue(self, s: Session, phone, now) -> list[Reply]:
        o = self.store.order(s.order_id) if s.order_id else None
        if not o:
            s.state = "need_order"
            return [Reply(f"I couldn't find order {s.order_id}. Could you double-check the number in your confirmation email?")]
        owner = self.store.customer(o["phone"])
        if not guards.owns(o, phone, None, None) and s.order_id not in s.verified:
            s.state = "need_email"
            s.events.append(f"ownership check for {o['id']}")
            return [Reply("To protect your privacy, I can only share order details with the number that placed the order. "
                          "Please reply with the email address used for this order.")]
        if s.action == "track":
            return self._track(s, o)
        if s.action == "return":
            ok, why = self.store.return_eligibility(o)
            if not ok:
                self._reset(s)
                return [Reply(f"I can't start a return for {o['id']} because {why}.", [("btn_human", "Talk to a person")])]
            s.state = "need_reason"
            return [Reply(f"Order {o['id']} ({o['items']}) can be returned, {why}. What's the reason for the return?", REASONS)]
        if s.action == "cancel":
            if o["status"] != "processing":
                self._reset(s)
                extra = " You can return it free of charge after it arrives." if o["status"] == "shipped" else ""
                return [Reply(f"Order {o['id']} is already {o['status']}, so it can't be cancelled.{extra}")]
            s.state = "confirm_cancel"
            return [Reply(f"Order {o['id']} ({o['items']}, ${o['total']:.2f}) hasn't shipped yet. Do you want me to cancel it?",
                          [("yes", "Yes, cancel it"), ("no", "No, keep it")])]
        return []

    def _track(self, s: Session, o: dict) -> list[Reply]:
        self._reset(s)
        s.events.append(f"tracked {o['id']} ({o['status']})")
        if o["status"] == "processing":
            return [Reply(f"Order {o['id']} ({o['items']}) is being packed and will ship within 1 business day. "
                          f"Estimated delivery: {_d(o['eta'])}.")]
        if o["status"] == "shipped":
            return [Reply(f"Good news: order {o['id']} ({o['items']}) shipped on {_d(o['shipped'])} with {o['carrier']} and is "
                          f"expected {_d(o['eta'])}.\nTrack it here: {self.store.tracking_url(o)}")]
        if o["status"] == "delivered":
            return [Reply(f"Order {o['id']} ({o['items']}) was delivered on {_d(o['delivered'])}. Is everything OK with it?",
                          [("btn_return", "Start a return"), ("btn_human", "Talk to a person")])]
        return [Reply(f"Order {o['id']} was cancelled, and the refund went back to your original payment method.")]

    def _confirm_return(self, s: Session) -> list[Reply]:
        s.state = "confirm_return"
        o = self.store.order(s.order_id)
        return [Reply(f"Just to confirm: return {o['items']} from order {o['id']} (reason: {s.reason}). "
                      "We'll email a free prepaid label. Shall I start it?", [("yes", "Yes, start return"), ("no", "No")])]

    def _do_return(self, s: Session) -> list[Reply]:
        o = self.store.order(s.order_id)
        rma = self.store.create_return(o["id"], s.reason or "not given")
        s.events.append(f"return {rma} created for {o['id']}")
        self._reset(s)
        return [Reply(f"Done! Your return {rma} is open. A prepaid label is on its way to your email. Once we receive the item, "
                      f"your refund of ${o['total']:.2f} goes back to your original payment method within 5 to 7 business days.")]

    def _do_cancel(self, s: Session) -> list[Reply]:
        o = self.store.order(s.order_id)
        self.store.conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (o["id"],))
        self.store.conn.commit()
        s.events.append(f"cancelled {o['id']}")
        self._reset(s)
        return [Reply(f"Order {o['id']} is cancelled. Your ${o['total']:.2f} refund will show on your statement in 3 to 5 business days.")]

    def _policy(self, text: str) -> list[Reply]:
        hits = self.kb.search(text)
        if not hits:
            return [Reply("I'm not sure about that one, and I don't want to guess. Want me to connect you with our team?",
                          [("btn_human", "Talk to a person")])]
        answer = " ".join(h["text"] for h in hits)
        return [Reply(f"{answer}\n\n(From our {hits[0]['source']} policy)")]

    def _handoff(self, s: Session, phone, reason, text, now, lead="") -> list[Reply]:
        tid = self.store.create_ticket(phone, reason, guards.mask_pii(text))
        s.state = "human"
        s.events.append(f"handed off to a person ({reason}), ticket {tid}")
        when = "A teammate will reply right here in a few minutes." if support_open(now) else \
            "Our team is offline right now and will reply here first thing next business morning (9 AM ET)."
        return [Reply(f"{lead}I've passed this to our support team (ticket {tid}) with the full chat history. {when}")]

    def _waiting(self, s: Session) -> list[Reply]:
        s.events.append("waiting for agent")
        return [Reply("Thanks, I've added that to your ticket. A teammate will reply here.")]

    def release(self, phone: str) -> None:
        """Called from the console when a human agent hands the chat back to the bot."""
        s = self.session(phone)
        self._reset(s)
        s.events.append("handed back to the assistant")
        self._save(s)

    @staticmethod
    def _reset(s: Session):
        s.state, s.action, s.order_id, s.reason = "idle", None, None, None
