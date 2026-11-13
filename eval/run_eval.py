"""Scripted WhatsApp conversations, each checked against expected outcomes.

Each conversation runs through the real /webhook endpoint (Cloud API payloads) on a fresh store.
Usage: python eval/run_eval.py [test|dev]   ->  eval/results_<set>.json
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wa_agent import whatsapp  # noqa: E402
from wa_agent.agent import SupportAgent  # noqa: E402
from wa_agent.store import Store  # noqa: E402

CHECKS = {
    "tracked": lambda ctx, v: any(f"tracked {v}" in e for e in ctx["events"]) and v in ctx["all_replies"],
    "return_created_for": lambda ctx, v: ctx["store"].conn.execute("SELECT 1 FROM returns WHERE order_id=?", (v,)).fetchone() is not None,
    "no_return": lambda ctx, v: ctx["store"].conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0] == 0,
    "cancelled": lambda ctx, v: ctx["store"].order(v)["status"] == "cancelled",
    "not_cancelled": lambda ctx, v: ctx["store"].order(v)["status"] != "cancelled",
    "handoff": lambda ctx, v: (ctx["session"].state == "human") == v,
    "reply_contains": lambda ctx, v: all(x.lower() in ctx["all_replies"].lower() for x in ([v] if isinstance(v, str) else v)),
    "reply_not_contains": lambda ctx, v: all(x.lower() not in ctx["all_replies"].lower() for x in ([v] if isinstance(v, str) else v)),
    "card_masked": lambda ctx, v: not re.search(r"\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}", ctx["logged_in"]),
    "event": lambda ctx, v: any(v in e for e in ctx["events"]),
}


def run_one(conv, tmp):
    os.environ.pop("WA_TOKEN", None)
    store = Store(Path(tmp) / f"s{time.perf_counter_ns()}.db", reset=True)
    agent = SupportAgent(store, Path(tmp) / f"c{time.perf_counter_ns()}.db")
    now = datetime.fromisoformat(conv.get("now", "2026-11-08T11:00"))
    replies, lat = [], []
    for turn in conv["turns"]:
        payload = whatsapp.sample_button_event(conv["phone"], "Test", turn["button"], turn.get("title", "")) \
            if isinstance(turn, dict) else whatsapp.sample_text_event(conv["phone"], "Test", turn)
        for m in whatsapp.parse_messages(payload):
            t0 = time.perf_counter()
            out = agent.handle(m["from"], m["text"], now, m["button_id"])
            lat.append((time.perf_counter() - t0) * 1000)
            replies += [r.text for r in out]
    s = agent.session(conv["phone"])
    logged_in = " ".join(r[0] for r in agent.log.execute("SELECT text FROM messages WHERE direction='in'"))
    ctx = {"store": store, "session": s, "events": s.events, "all_replies": "\n".join(replies), "logged_in": logged_in}
    failed = [k for k, v in conv["expect"].items() if not CHECKS[k](ctx, v)]
    return failed, replies, lat


def main():
    split = sys.argv[1] if len(sys.argv) > 1 else "test"
    convs = json.loads((ROOT / f"eval/{split}_conversations.json").read_text())["conversations"]
    ok, lat_all, failures, by_cat = 0, [], [], {}
    with tempfile.TemporaryDirectory() as tmp:
        for c in convs:
            failed, replies, lat = run_one(c, tmp)
            lat_all += lat
            cat = c.get("category", "other")
            by_cat.setdefault(cat, [0, 0])
            by_cat[cat][1] += 1
            if not failed:
                ok += 1
                by_cat[cat][0] += 1
            else:
                failures.append({"name": c["name"], "failed_checks": failed, "replies": replies})
    res = {"split": split, "conversations": len(convs), "passed": ok, "pass_rate": round(ok / len(convs), 3),
           "by_category": {k: f"{v[0]}/{v[1]}" for k, v in by_cat.items()},
           "avg_ms_per_message": round(sum(lat_all) / len(lat_all), 2), "failures": failures}
    (ROOT / f"eval/results_{split}.json").write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if k != "failures"}, indent=2))
    for f in failures:
        print("\nFAIL", f["name"], f["failed_checks"])
        for r in f["replies"]:
            print("   <", r[:140].replace("\n", " "))


if __name__ == "__main__":
    main()
