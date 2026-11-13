"""Policy knowledge base: sentence-level retrieval over the store's help articles."""
from __future__ import annotations

import re
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.pipeline import FeatureUnion

ROOT = Path(__file__).resolve().parents[1]

SYNONYMS = {r"\bdelivery\b": "shipping delivery", r"\bship(s|ping)?\b": "shipping ship", r"\brefunds?\b": "refund return",
            r"\bexchange\b": "exchange size return", r"\bklarna|installments?|pay later\b": "installments klarna",
            r"\bopen\b": "hours open", r"\bguarantee\b": "warranty", r"\bbroken|defect(ive)?|faulty\b": "defect warranty",
            r"\babroad|overseas|international(ly)?|canada|uk\b": "internationally ship", r"\bcost\b": "costs cost", r"\bhow much\b.*\bship|\bship\w*\b.*\bhow much\b": "shipping costs free orders over"}


def _expand(text: str) -> str:
    t = text.lower()
    for p, add in SYNONYMS.items():
        if re.search(p, t):
            t += " " + add
    return t


class KnowledgeBase:
    def __init__(self, folder: Path = ROOT / "data" / "kb"):
        self.sentences = []                       # (doc_title, sentence)
        for path in sorted(folder.glob("*.md")):
            text = path.read_text()
            title = re.search(r"^#\s+(.+)$", text, re.M).group(1)
            body = re.sub(r"^#.*$", "", text, flags=re.M)
            for s in re.split(r"(?<=[.!?])\s+", body.strip()):
                if len(s) > 15:
                    self.sentences.append((title, s.strip()))
        docs = [_expand(f"{t}. {s}") for t, s in self.sentences]
        self.vec = FeatureUnion([("w", TfidfVectorizer(ngram_range=(1, 2), stop_words="english", sublinear_tf=True)),
                                 ("c", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True))])
        self.matrix = self.vec.fit_transform(docs)

    def search(self, question: str, k: int = 2, min_score: float = 0.12) -> list[dict]:
        sims = cosine_similarity(self.vec.transform([_expand(question)]), self.matrix)[0]
        order = sims.argsort()[::-1]
        best = order[0]
        if sims[best] < min_score:
            return []
        hits = [order[0]]
        for i in order[1:6]:                      # add a second sentence from the same article if relevant
            if self.sentences[i][0] == self.sentences[best][0] and sims[i] >= 0.6 * sims[best]:
                hits.append(i)
                break
        return [{"source": self.sentences[i][0], "text": self.sentences[i][1], "score": round(float(sims[i]), 3)} for i in hits[:k]]
