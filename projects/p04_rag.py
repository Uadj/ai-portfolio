"""#04 사내 문서 Q&A (RAG) — 청킹 전략 × 검색기 비교 + Recall@k 평가 + 출처 인용 답변.

임베딩 API 없이도 동작하도록 dense 검색 자리에 char n-gram TF-IDF 코사인을 쓴다.
(외부 임베딩 모델을 붙이려면 `CharTfidf`를 교체하면 된다.)
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel

from core import llm

router = APIRouter(prefix="/api/p04")
DOC_DIR = llm.ROOT / "data" / "handbook"
EVAL_FILE = llm.ROOT / "data" / "evals" / "rag_eval.jsonl"


@dataclass
class Chunk:
    doc: str
    title: str
    text: str


# ---------- 청킹 전략 ----------
def load_docs() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(DOC_DIR.glob("*.md"))}


def chunk_fixed(docs, size=200, overlap=0) -> list[Chunk]:
    out = []
    for name, text in docs.items():
        flat = re.sub(r"\s+", " ", text)
        step = size - overlap
        for i in range(0, len(flat), step):
            out.append(Chunk(name, name, flat[i:i + size]))
            if i + size >= len(flat):
                break
    return out


def chunk_heading(docs) -> list[Chunk]:
    """'## 섹션' 단위로 자르고, 문서 제목+섹션명을 청크 앞에 붙인다 (contextual chunk)."""
    out = []
    for name, text in docs.items():
        title = text.splitlines()[0].lstrip("# ").strip()
        for sec in re.split(r"\n(?=## )", text):
            lines = sec.strip().splitlines()
            if not lines or lines[0].startswith("# "):
                continue
            head = lines[0].lstrip("# ").strip()
            body = re.sub(r"\s+", " ", " ".join(lines[1:])).strip()
            out.append(Chunk(name, f"{title} > {head}", f"[{title} > {head}] {body}"))
    return out


CHUNKERS = {
    "fixed-200": lambda d: chunk_fixed(d, 200, 0),
    "fixed-200-overlap50": lambda d: chunk_fixed(d, 200, 50),
    "heading-contextual": chunk_heading,
}


# ---------- 검색기 ----------
def word_tokens(s: str) -> list[str]:
    toks = re.findall(r"[0-9A-Za-z가-힣]+", s.lower())
    # 한국어 조사 대응: 2글자 이상 단어는 앞 2글자 프리픽스도 토큰으로 추가
    return toks + [t[:2] for t in toks if len(t) > 2 and re.match(r"[가-힣]", t)]


def char_ngrams(s: str, ns=(2, 3)) -> list[str]:
    s = re.sub(r"\s+", "", s.lower())
    return [s[i:i + n] for n in ns for i in range(len(s) - n + 1)]


class BM25:
    def __init__(self, texts, k1=1.5, b=0.75):
        self.docs = [Counter(word_tokens(t)) for t in texts]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / len(self.lens)
        df = Counter(w for d in self.docs for w in d)
        n = len(texts)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, q):
        qt = word_tokens(q)
        out = np.zeros(len(self.docs))
        for i, d in enumerate(self.docs):
            s = 0.0
            for w in qt:
                if w in d:
                    tf = d[w]
                    s += self.idf[w] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            out[i] = s
        return out


class CharTfidf:
    def __init__(self, texts):
        grams = [Counter(char_ngrams(t)) for t in texts]
        vocab = {g: i for i, g in enumerate({g for c in grams for g in c})}
        df = Counter(g for c in grams for g in c)
        n = len(texts)
        self.vocab = vocab
        self.idf = np.zeros(len(vocab))
        for g, i in vocab.items():
            self.idf[i] = math.log((1 + n) / (1 + df[g])) + 1
        self.mat = np.stack([self._vec(c) for c in grams])

    def _vec(self, counter):
        v = np.zeros(len(self.vocab))
        for g, c in counter.items():
            if g in self.vocab:
                v[self.vocab[g]] = (1 + math.log(c)) * self.idf[self.vocab[g]]
        norm = np.linalg.norm(v)
        return v / norm if norm else v

    def scores(self, q):
        return self.mat @ self._vec(Counter(char_ngrams(q)))


def rrf(*score_lists, k=60):
    out = np.zeros(len(score_lists[0]))
    for s in score_lists:
        for rank, idx in enumerate(np.argsort(-s)):
            out[idx] += 1 / (k + rank + 1)
    return out


class Index:
    def __init__(self, chunker: str):
        self.chunks = CHUNKERS[chunker](load_docs())
        texts = [c.text for c in self.chunks]
        self.bm25 = BM25(texts)
        self.tfidf = CharTfidf(texts)

    def search(self, q: str, retriever: str, k: int):
        if retriever == "bm25":
            s = self.bm25.scores(q)
        elif retriever == "char-tfidf":
            s = self.tfidf.scores(q)
        else:
            s = rrf(self.bm25.scores(q), self.tfidf.scores(q))
        idx = np.argsort(-s)[:k]
        return [(self.chunks[i], float(s[i])) for i in idx]


@lru_cache(maxsize=None)
def get_index(chunker: str) -> Index:
    return Index(chunker)


RETRIEVERS = ["bm25", "char-tfidf", "hybrid-rrf"]


# ---------- 평가 ----------
@router.get("/eval")
def evaluate():
    """청킹 × 검색기 × k 조합별 Recall@k, MRR (LLM 호출 없음, 즉시 계산)."""
    cases = [json.loads(l) for l in EVAL_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    norm = lambda s: re.sub(r"\s+", " ", s)
    rows = []
    for ch in CHUNKERS:
        idx = get_index(ch)
        for r in RETRIEVERS:
            hits = {1: 0, 3: 0, 5: 0}
            mrr = 0.0
            for c in cases:
                res = idx.search(c["q"], r, 5)
                ranks = [i for i, (chunk, _) in enumerate(res) if c["key"] in norm(chunk.text)]
                if ranks:
                    mrr += 1 / (ranks[0] + 1)
                    for k in hits:
                        if ranks[0] < k:
                            hits[k] += 1
            n = len(cases)
            rows.append({"chunker": ch, "retriever": r, "chunks": len(idx.chunks),
                         "recall@1": round(hits[1] / n, 3), "recall@3": round(hits[3] / n, 3),
                         "recall@5": round(hits[5] / n, 3), "mrr": round(mrr / n, 3)})
    best = max(rows, key=lambda x: (x["recall@3"], x["mrr"]))
    return {"cases": len(cases), "rows": rows, "best": best}


# ---------- 질의응답 ----------
class AskReq(BaseModel):
    question: str
    chunker: str = "heading-contextual"
    retriever: str = "hybrid-rrf"
    k: int = 4


@router.get("/docs")
def docs():
    return {"docs": [{"name": n, "text": t} for n, t in load_docs().items()], "chunkers": list(CHUNKERS), "retrievers": RETRIEVERS}


@router.post("/search")
def search(req: AskReq):
    res = get_index(req.chunker).search(req.question, req.retriever, req.k)
    return {"results": [{"doc": c.doc, "title": c.title, "text": c.text, "score": round(s, 4)} for c, s in res]}


@router.post("/ask")
def ask(req: AskReq):
    res = get_index(req.chunker).search(req.question, req.retriever, req.k)
    content = [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": c.text},
         "title": c.title, "citations": {"enabled": True}}
        for c, _ in res
    ]
    content.append({"type": "text", "text": req.question})
    system = ("당신은 사내 규정 안내 봇입니다. 제공된 문서에 근거해서만 한국어로 답하세요. "
              "문서에 답이 없으면 '제공된 문서에서 찾을 수 없습니다'라고 답하세요.")
    msg = llm.create([{"role": "user", "content": content}], system=system, effort="low", max_tokens=4000)

    parts = []
    for b in msg.content:
        if b.type != "text":
            continue
        cites = [{"doc_index": c.document_index, "title": c.document_title, "text": c.cited_text}
                 for c in (getattr(b, "citations", None) or [])]
        parts.append({"text": b.text, "citations": cites})
    return {
        "answer": parts,
        "retrieved": [{"doc": c.doc, "title": c.title, "text": c.text, "score": round(s, 4)} for c, s in res],
        "usage": llm.usage_of(msg),
    }
