"""Intent classification, entity extraction and frustration detection for chat messages."""
from __future__ import annotations

import json
import re
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, make_pipeline

ROOT = Path(__file__).resolve().parents[1]
_data = json.loads((ROOT / "data" / "intents.json").read_text())
_clf = make_pipeline(
    FeatureUnion([("w", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
                  ("c", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True))]),
    LogisticRegression(C=10, max_iter=3000, class_weight="balanced"),
).fit([t for v in _data.values() for t in v], [k for k, v in _data.items() for _ in v])

RULES = [  # high-precision overrides before the classifier
    ("human_handoff", r"\b(human|real person|agent|representative|someone real|talk to (a )?person|speak to (a )?person|manager)\b"),
    ("cancel_order", r"\bcancel\b"),
    ("return_request", r"\b(return|refund|exchange|send (it|this|them) back)\b(?!.*\b(policy|how long|window|free)\b)"),
    ("track_order", r"\b(where('s| is) my|track|tracking|shipped yet|arrive|delivery status|order status|haven'?t (received|got)|still not here|not here yet)\b"),
]


def intent(text: str) -> tuple[str, float]:
    t = text.lower()
    for name, pat in RULES:
        if re.search(pat, t):
            return name, 1.0
    if re.fullmatch(r"\W*(hi|hello|hey|hiya|good (morning|afternoon|evening)|yo|salam|hola)\b[\w\s,!?.]{0,25}", t):
        return "greeting", 1.0
    probs = _clf.predict_proba([text])[0]
    i = probs.argmax()
    label = str(_clf.classes_[i])
    if label == "greeting":                    # greetings are handled by the rule above only
        probs[i] = 0
        i = probs.argmax()
        label = str(_clf.classes_[i])
    return label, float(probs[i])


def order_id(text: str) -> str | None:
    m = re.search(r"\b(?:pk[-\s]?)?(10\d{3})\b", text, re.I)
    return f"PK-{m.group(1)}" if m else None


def email(text: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return m.group(0).lower() if m else None


NEGATIVE = r"\b(terrible|horrible|awful|ridiculous|unacceptable|worst|scam|furious|angry|disgusted|useless|pathetic|joke|fed up|sick of|never again|lawyer|chargeback)\b"


def frustration(text: str) -> float:
    """0..1 score from strong words, repeated punctuation and shouting."""
    score = 0.0
    if re.search(NEGATIVE, text.lower()):
        score += 0.6
    if re.search(r"[!?]{3,}", text):
        score += 0.2
    letters = [c for c in text if c.isalpha()]
    if len(letters) > 12 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        score += 0.6                         # shouting on its own is a strong signal
    return min(score, 1.0)


def yes_no(text: str) -> bool | None:
    t = text.lower().strip()
    if re.search(r"^(no|nope|nah|don'?t|cancel that|never ?mind|not now)\b", t):
        return False
    if re.search(r"^(yes|yeah|yep|y|sure|ok|okay|confirm|please do|go ahead|do it)\b|\bconfirm\b", t):
        return True
    return None
