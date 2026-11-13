"""WhatsApp Cloud API webhook + support console + phone simulator.

Run:  python web/app.py  ->  http://localhost:8500
Webhook URL for Meta:  https://YOUR-DOMAIN/webhook
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wa_agent import whatsapp  # noqa: E402
from wa_agent.agent import SupportAgent  # noqa: E402
from wa_agent.store import Store  # noqa: E402

app = Flask(__name__, template_folder="templates")
store = Store()
agent = SupportAgent(store)
SIM_NOW = os.getenv("SIM_NOW")          # fixed clock for demos, e.g. 2026-11-08T11:00


def now():
    return datetime.fromisoformat(SIM_NOW) if SIM_NOW else datetime.now()


@app.get("/webhook")
def verify():
    challenge = whatsapp.verify_subscription(request.args)
    return (challenge, 200) if challenge else ("forbidden", 403)


@app.post("/webhook")
def incoming():
    raw = request.get_data()
    if not whatsapp.valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        abort(401)
    sent = []
    for m in whatsapp.parse_messages(json.loads(raw)):
        for r in agent.handle(m["from"], m["text"], now(), m["button_id"]):
            whatsapp.send(m["from"], r.text, r.buttons)
            sent.append({"text": r.text, "buttons": r.buttons})
    return jsonify({"ok": True, "replies": sent})


# ------------------------------------------------------------------ console
def conversations():
    rows = agent.log.execute("""SELECT phone, MAX(id) last_id, COUNT(*) n FROM messages GROUP BY phone ORDER BY last_id DESC""").fetchall()
    out = []
    for phone, last_id, n in rows:
        last = agent.log.execute("SELECT text, ts, direction FROM messages WHERE id=?", (last_id,)).fetchone()
        cust = store.customer(phone)
        s = agent.session(phone)
        out.append({"phone": phone, "name": cust["name"] if cust else f"+{phone}", "last": last[0], "ts": last[1][11:16],
                    "mode": "human" if s.state == "human" else "bot", "n": n})
    return out


@app.get("/")
def console_home():
    convs = conversations()
    return redirect(f"/chat/{convs[0]['phone']}") if convs else render_template("empty.html")


@app.get("/chat/<phone>")
def chat(phone):
    msgs = [dict(zip(("text", "buttons", "meta", "direction", "ts"), (r[0], json.loads(r[1]), json.loads(r[2]), r[3], r[4])))
            for r in agent.log.execute("SELECT text, buttons, meta, direction, ts FROM messages WHERE phone=? ORDER BY id", (phone,))]
    s = agent.session(phone)
    tickets = [dict(r) for r in store.conn.execute("SELECT * FROM tickets WHERE phone=? ORDER BY created DESC", (phone,))]
    returns = [dict(r) for r in store.conn.execute(
        "SELECT r.* FROM returns r JOIN orders o ON o.id=r.order_id WHERE o.phone=?", (phone,))]
    return render_template("console.html", convs=conversations(), phone=phone, msgs=msgs, cust=store.customer(phone),
                           orders=store.orders_for(phone), session=s, tickets=tickets, returns=returns)


@app.post("/chat/<phone>/reply")
def human_reply(phone):
    text = request.form["text"].strip()
    if text:
        whatsapp.send(phone, text)
        agent._log(phone, "out", text, meta={"agent": "human"}, now=now())
    return redirect(f"/chat/{phone}")


@app.post("/chat/<phone>/release")
def release(phone):
    agent.release(phone)
    agent._log(phone, "note", "Chat handed back to the assistant", now=now())
    return redirect(f"/chat/{phone}")


# ------------------------------------------------------------------ simulator (posts real webhook payloads)
@app.get("/simulator")
def simulator():
    return render_template("simulator.html", phone=request.args.get("phone", "15550100101"))


@app.post("/api/sim")
def sim():
    d = request.json
    payload = whatsapp.sample_button_event(d["phone"], d.get("name", "Test"), d["button_id"], d["text"]) if d.get("button_id") \
        else whatsapp.sample_text_event(d["phone"], d.get("name", "Test"), d["text"])
    with app.test_client() as c:
        return c.post("/webhook", data=json.dumps(payload), content_type="application/json").get_json()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8500)
