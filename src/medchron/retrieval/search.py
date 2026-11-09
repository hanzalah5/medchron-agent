"""Hybrid retrieval over spans: lexical and vector results fused with RRF.

Two implementations share one interface:
- ``PostgresSearch``: Postgres full-text search (``ts_rank`` on a generated
  ``tsvector``) plus pgvector cosine distance, fused in SQL. Production path.
- ``MemorySearch``: BM25 plus the same embedder over an in-memory
  ``IngestedCase``. Used for offline runs, tests and the oracle demo.

Both are scoped to one matter. Every hit carries the span ID and page, so
agents can cite what they retrieve.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from medchron.ingestion.models import IngestedCase
from medchron.retrieval.embeddings import Embedder

RRF_K = 60


class SearchHit(BaseModel):
    span_id: str
    page_no: int
    text: str
    score: float


class SpanSearch(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        page_from: int | None = None,
        page_to: int | None = None,
    ) -> list[SearchHit]: ...


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def rrf(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


class MemorySearch:
    def __init__(self, case: IngestedCase, embedder: Embedder) -> None:
        self.case, self.embedder = case, embedder
        self.spans = case.spans
        self._tf = [Counter(_tokens(s.text)) for s in self.spans]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 1.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.spans)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._vecs = embedder.embed([s.text for s in self.spans]) if self.spans else []

    def _bm25(self, i: int, q: list[str], k1: float = 1.4, b: float = 0.75) -> float:
        tf, length = self._tf[i], self._len[i]
        score = 0.0
        for t in q:
            if t in tf:
                f = tf[t]
                score += self._idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self._avg))
        return score

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        page_from: int | None = None,
        page_to: int | None = None,
    ) -> list[SearchHit]:
        idx = [
            i
            for i, s in enumerate(self.spans)
            if (page_from is None or s.page_no >= page_from)
            and (page_to is None or s.page_no <= page_to)
        ]
        q = _tokens(query)
        lexical = sorted((i for i in idx if self._bm25(i, q) > 0), key=lambda i: -self._bm25(i, q))
        qv = self.embedder.embed([query])[0]
        vector = sorted(
            idx, key=lambda i: -sum(a * b for a, b in zip(qv, self._vecs[i], strict=True))
        )
        fused = rrf([[str(i) for i in lexical[:50]], [str(i) for i in vector[:50]]])
        best = sorted(fused.items(), key=lambda kv: -kv[1])[:limit]
        return [
            SearchHit(
                span_id=self.spans[int(i)].span_id,
                page_no=self.spans[int(i)].page_no,
                text=self.spans[int(i)].text,
                score=round(score, 5),
            )
            for i, score in best
        ]


_HYBRID_SQL = text(
    """
    WITH lexical AS (
        SELECT span_id, row_number() OVER (
            ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC) AS r
        FROM spans
        WHERE matter_id = :m AND tsv @@ websearch_to_tsquery('english', :q)
          AND page_no BETWEEN :p0 AND :p1
        ORDER BY r LIMIT 50
    ),
    vector AS (
        SELECT span_id, row_number() OVER (ORDER BY embedding <=> CAST(:v AS vector)) AS r
        FROM spans
        WHERE matter_id = :m AND page_no BETWEEN :p0 AND :p1
        ORDER BY r LIMIT 50
    ),
    fused AS (
        SELECT span_id, sum(1.0 / (:k + r)) AS score
        FROM (SELECT * FROM lexical UNION ALL SELECT * FROM vector) u
        GROUP BY span_id
    )
    SELECT s.span_id, s.page_no, s.text, f.score
    FROM fused f JOIN spans s USING (span_id)
    ORDER BY f.score DESC, s.span_id
    LIMIT :limit
    """
)


class PostgresSearch:
    def __init__(self, session: Session, matter_id: str, embedder: Embedder) -> None:
        self.session, self.matter_id, self.embedder = session, matter_id, embedder

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        page_from: int | None = None,
        page_to: int | None = None,
    ) -> list[SearchHit]:
        vec = self.embedder.embed([query])[0]
        rows = self.session.execute(
            _HYBRID_SQL,
            {
                "q": query,
                "m": self.matter_id,
                "v": str(vec),
                "k": RRF_K,
                "p0": page_from or 0,
                "p1": page_to or 1_000_000,
                "limit": limit,
            },
        ).all()
        return [SearchHit(span_id=r[0], page_no=r[1], text=r[2], score=float(r[3])) for r in rows]
