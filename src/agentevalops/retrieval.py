"""Local knowledge base and the deterministic retriever behind ``search_knowledge_base``.

Implements SPEC-RAG-1 / SPEC-RAG-5. The retriever is deliberately lexical and
deterministic: M3 exists to make retrieval *judgeable* (L2), and an embedding store would
add a model call, a network dependency and a second source of variance before there is
any baseline to compare it against.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .models import ErrorCode, ToolResult

#: 文档至少要覆盖查询信息量（idf 加权）的三分之一，才算「有依据」。
DEFAULT_MIN_COVERAGE = 0.34
#: 过期文档的降权系数（SPEC-RAG-5：现行文档必须排在过期文档之前）。
EXPIRED_PENALTY = 0.5
DEFAULT_TOP_K = 3
#: 一篇文档至少要真正命中查询里这么多个「语料认识的词」，才算沾边。
#: 单命中一个词就给出依据，等于把「某个词恰好出现过」当成「知识库能回答」，那是拒答失败。
DEFAULT_MIN_MATCHES = 2

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """CJK 用字符二元组，ASCII 用单词：零依赖、可复现，不需要分词器。"""
    lowered = text.casefold()
    tokens: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_ASCII_WORD.findall(lowered))
    return tokens


def topic_of(doc_id: str) -> str:
    """``DOC-REFUND-002`` -> ``REFUND``：同一主题用于「现行优先于过期」的判定。"""
    parts = doc_id.split("-")
    return parts[1] if len(parts) > 2 else doc_id


@dataclass(frozen=True)
class Hit:
    doc_id: str
    title: str
    score: float
    expired: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "score": round(self.score, 4),
            "expired": self.expired,
        }


def _current_before_expired(hits: list[Hit]) -> list[Hit]:
    """同一主题内，现行文档必须排在过期文档之前（SPEC-RAG-5）。

    只靠打分不够：过期文档的字面重合度可能更高。这里做一次显式的稳定调整，
    把条款写成代码里一眼能看懂的不变式，而不是「通常能排对」。
    """
    ordered = list(hits)
    for _ in range(len(ordered)):
        swapped = False
        for left in range(len(ordered) - 1):
            first, second = ordered[left], ordered[left + 1]
            if (
                first.expired
                and not second.expired
                and topic_of(first.doc_id) == topic_of(second.doc_id)
            ):
                ordered[left], ordered[left + 1] = second, first
                swapped = True
        if not swapped:
            break
    return ordered


class KnowledgeBase:
    """Loads the packaged corpus and scores documents by how much of the query they cover."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        if docs is None:
            data_file = files("agentevalops.data").joinpath("knowledge_base.json")
            docs = json.loads(data_file.read_text(encoding="utf-8"))
        self._docs = [dict(doc) for doc in docs]
        self._tokens = {
            doc["doc_id"]: set(tokenize(self._searchable_text(doc)))
            for doc in self._docs
        }
        # 文档频率：一个词在越多文档里出现，区分度越低。
        self._df: dict[str, int] = {}
        for token_set in self._tokens.values():
            for token in token_set:
                self._df[token] = self._df.get(token, 0) + 1
        # 语料里最不具区分度的词的权重。查询里语料**不认识**的词按这个权重计入分母：
        # 它们不该被当成最高权重的信息词，否则「问得啰嗦一点」就等于「检索不到」。
        self._min_idf = min((self._idf(token) for token in self._df), default=0.0)

    @staticmethod
    def _searchable_text(doc: dict[str, Any]) -> str:
        tags = " ".join(doc.get("tags", []))
        return " ".join([doc.get("title", ""), doc.get("text", ""), tags])

    @property
    def documents(self) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self._docs]

    def _idf(self, token: str) -> float:
        # 语料里根本没出现过的词拿到最高权重：它没被任何文档覆盖，覆盖度就该被拉低。
        return math.log(1 + len(self._docs) / (1 + self._df.get(token, 0)))

    def _coverage(self, query_tokens: list[str], doc_id: str) -> float:
        """文档覆盖了查询多少信息量：被命中的 idf 权重占查询全部权重的比例。

        两条口径都是为了修掉同一类 bad case，都写在代码里而不是靠调参碰运气：

        1. 查询里语料不认识的词，按「最不具区分度的词」的权重计入分母。
           原口径把它们按最高 idf 计入，于是「退货运费政策是怎么规定的」在语料明明有运费
           条款的情况下检索为空——「是怎」「么规」这些追问式虚词谁都不覆盖，却把分母撑爆。
        2. 至少要命中 DEFAULT_MIN_MATCHES 个语料认识的词。
           「量子计算机 超导 实验室」里的「计算」只要撞上某篇文档，按比例算也能接近满分；
           一个偶然撞上的词不构成依据。
        """
        if not query_tokens:
            return 0.0
        haystack = self._tokens[doc_id]
        covered_weight = 0.0
        total_weight = 0.0
        matched = 0
        for token in dict.fromkeys(query_tokens):
            if token not in self._df:
                total_weight += self._min_idf
                continue
            weight = self._idf(token)
            total_weight += weight
            if token in haystack:
                covered_weight += weight
                matched += 1
        if matched < DEFAULT_MIN_MATCHES or total_weight <= 0:
            return 0.0
        return covered_weight / total_weight

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_MIN_COVERAGE,
    ) -> list[Hit]:
        """Top-k documents covering at least ``threshold`` of the query, current docs first."""
        query_tokens = tokenize(query)
        candidates: list[tuple[float, Hit]] = []
        for doc in self._docs:
            raw = self._coverage(query_tokens, doc["doc_id"])
            if raw < threshold:
                continue
            expired = doc.get("status") == "expired"
            effective = raw * (EXPIRED_PENALTY if expired else 1.0)
            candidates.append(
                (
                    effective,
                    Hit(
                        doc_id=doc["doc_id"],
                        title=doc["title"],
                        score=raw,
                        expired=expired,
                    ),
                )
            )
        # 平局按 doc_id 排序：同样的输入永远同样的顺序（可复现的前提）。
        candidates.sort(key=lambda item: (-item[0], item[1].doc_id))
        ranked = [hit for _, hit in candidates[:top_k]]
        return _current_before_expired(ranked)


class KnowledgeBaseTool:
    """``search_knowledge_base``：只读，检索不到也算成功（SPEC-RAG-1）。"""

    name = "search_knowledge_base"

    def __init__(self, knowledge_base: KnowledgeBase) -> None:
        self.knowledge_base = knowledge_base

    def execute(self, *, query: str, top_k: int = DEFAULT_TOP_K) -> ToolResult:
        if not isinstance(query, str) or not query.strip():
            return ToolResult.fail(ErrorCode.INVALID_ARGUMENT, "query is required")
        hits = self.knowledge_base.search(query, top_k=top_k)
        return ToolResult.ok(
            query=query,
            top_k=top_k,
            hits=[hit.to_dict() for hit in hits],
            retrieved=len(hits),
        )
