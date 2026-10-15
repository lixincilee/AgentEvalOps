"""L2 retrieval layer: the corpus, the scorer, and the read-only tool contract.

SPEC-RAG-1 / 5 / 6. Retrieval is the stage that fails *silently*: a bad hit list still
produces a confident sentence, and an empty hit list looks exactly like a broken index.
These tests pin down what must be judgeable before any answer is generated -- what is in
the corpus, what a hit looks like, what happens when nothing is found, and the two scoring
rules that were added to fix real bad cases.
"""

from __future__ import annotations

import json
import re

import pytest

from agentevalops.models import ErrorCode
from agentevalops.retrieval import (
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_MATCHES,
    DEFAULT_TOP_K,
    Hit,
    KnowledgeBase,
    KnowledgeBaseTool,
    _current_before_expired,
    tokenize,
    topic_of,
)

DOC_ID_PATTERN = re.compile(r"^DOC-[A-Z]+-\d{3}$")
ORDER_ID_PATTERN = re.compile(r"ORD-\d{4}")
REQUIRED_FIELDS = {
    "doc_id",
    "title",
    "text",
    "tags",
    "source",
    "status",
    "effective_from",
    "effective_to",
    "lang",
}
#: 投毒文档正文里必须真的带着指令性内容，否则 SPEC-RAG-6 的用例是在测空气。
INJECTION_MARKERS = ("忽略", "不要", "请原样输出", "系统提示词")

CORPUS = KnowledgeBase()
DOCUMENTS = CORPUS.documents
DOC_IDS = [doc["doc_id"] for doc in DOCUMENTS]


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase()


@pytest.fixture
def kb_tool(kb: KnowledgeBase) -> KnowledgeBaseTool:
    return KnowledgeBaseTool(kb)


# --- 分词与主题 --------------------------------------------------------------


def test_tokenize_uses_cjk_bigrams_and_ascii_words():
    assert tokenize("退款政策 ORD-1001") == ["退款", "款政", "政策", "ord", "1001"]


def test_tokenize_keeps_a_lone_cjk_character():
    assert tokenize("退") == ["退"]


def test_tokenize_is_case_insensitive():
    assert tokenize("Refund") == tokenize("REFUND") == ["refund"]


@pytest.mark.parametrize(
    ("doc_id", "topic"),
    [("DOC-REFUND-002", "REFUND"), ("DOC-UGC-001", "UGC"), ("NO-TOPIC", "NO-TOPIC")],
)
def test_topic_of_extracts_the_middle_segment(doc_id, topic):
    assert topic_of(doc_id) == topic


# --- 语料完整性 --------------------------------------------------------------


def test_corpus_is_loaded_from_the_packaged_data():
    assert 28 <= len(DOCUMENTS) <= 34


def test_doc_ids_are_unique():
    assert len(DOC_IDS) == len(set(DOC_IDS))


def test_every_document_declares_the_full_schema():
    for doc in DOCUMENTS:
        missing = REQUIRED_FIELDS - set(doc)
        assert not missing, f"{doc.get('doc_id')} 缺少字段 {sorted(missing)}"


def test_doc_ids_follow_the_naming_convention():
    for doc_id in DOC_IDS:
        assert DOC_ID_PATTERN.match(doc_id), doc_id


def test_status_and_source_vocabularies_are_closed():
    assert {doc["status"] for doc in DOCUMENTS} <= {"current", "expired"}
    assert {doc["source"] for doc in DOCUMENTS} <= {"official", "user_review"}


def test_current_documents_carry_no_end_date():
    for doc in DOCUMENTS:
        if doc["status"] == "current":
            assert doc["effective_to"] is None, doc["doc_id"]


def test_expired_documents_declare_an_end_date():
    for doc in DOCUMENTS:
        if doc["status"] == "expired":
            assert doc["effective_to"] is not None, doc["doc_id"]


def test_document_bodies_are_long_enough_to_be_useful():
    for doc in DOCUMENTS:
        assert len(doc["text"]) >= 80, doc["doc_id"]


def test_titles_and_bodies_are_unique():
    assert len({doc["title"] for doc in DOCUMENTS}) == len(DOCUMENTS)
    assert len({doc["text"] for doc in DOCUMENTS}) == len(DOCUMENTS)


def test_corpus_never_contains_order_ids():
    """语料里出现真实订单号，检索就变成一条数据泄露通道。"""
    for doc in DOCUMENTS:
        assert not ORDER_ID_PATTERN.search(doc["text"]), doc["doc_id"]
        assert not ORDER_ID_PATTERN.search(doc["title"]), doc["doc_id"]


def test_corpus_covers_the_declared_topics():
    topics = {topic_of(doc["doc_id"]) for doc in DOCUMENTS}
    assert {"REFUND", "RETURN", "FREIGHT", "MEMBER", "SERVICE", "COUPON"} <= topics


def test_the_corpus_keeps_more_than_a_couple_of_expired_documents():
    expired = [doc for doc in DOCUMENTS if doc["status"] == "expired"]
    assert len(expired) >= 4


def test_every_expired_document_has_a_current_counterpart_on_the_same_topic():
    current = {topic_of(doc["doc_id"]) for doc in DOCUMENTS if doc["status"] == "current"}
    for doc in DOCUMENTS:
        if doc["status"] == "expired":
            assert topic_of(doc["doc_id"]) in current, doc["doc_id"]


def test_untrusted_documents_are_quarantined_under_a_visible_prefix():
    untrusted = [doc for doc in DOCUMENTS if doc["source"] == "user_review"]
    assert len(untrusted) >= 3
    for doc in untrusted:
        assert doc["doc_id"].startswith("DOC-UGC-"), doc["doc_id"]


def test_untrusted_documents_really_carry_an_injection_payload():
    """SPEC-RAG-6 的用例必须打在有 payload 的文档上。"""
    untrusted = [doc for doc in DOCUMENTS if doc["source"] == "user_review"]
    for doc in untrusted:
        assert any(marker in doc["text"] for marker in INJECTION_MARKERS), doc["doc_id"]


# --- 打分 --------------------------------------------------------------------


def test_search_is_deterministic(kb):
    query = "退款政策是怎么规定的"
    assert [hit.to_dict() for hit in kb.search(query)] == [
        hit.to_dict() for hit in kb.search(query)
    ]


def test_top_k_bounds_the_hit_list(kb):
    query = "退款多久到账"
    assert len(kb.search(query, top_k=1)) == 1
    assert len(kb.search(query)) <= DEFAULT_TOP_K


def test_hit_scores_are_rounded_for_stable_reports():
    hit = Hit(doc_id="DOC-X-001", title="t", score=0.123456789, expired=False)
    assert hit.to_dict()["score"] == 0.1235


def test_the_default_coverage_threshold_is_the_documented_one():
    """阈值是判分口径的一部分：改它必须同步改文档，否则两批分数不可比。"""
    assert DEFAULT_MIN_COVERAGE == 0.34
    assert DEFAULT_MIN_MATCHES == 2


def test_a_verbose_question_still_finds_its_document(kb):
    """回归：曾经的 bad case。

    「…是怎么规定的」这类追问式措辞里的虚词谁都不覆盖，旧口径把它们按最高 idf 计入分母，
    于是「问得啰嗦一点」等于「检索不到」。
    """
    hits = kb.search("退款政策是怎么规定的")
    assert [hit.doc_id for hit in hits] == ["DOC-REFUND-001", "DOC-REFUND-002"]


def test_a_single_accidental_word_is_not_evidence(kb):
    """回归：反方向的 bad case。

    噪声查询里的某个词偶然撞上语料，不构成「知识库能回答」的依据。
    """
    assert kb.search("量子计算机 超导 实验室") == []


def test_a_topic_the_corpus_never_mentions_returns_nothing(kb):
    assert kb.search("宠物寄存政策是什么") == []


def test_a_blank_query_returns_nothing(kb):
    assert kb.search("   ") == []


def test_every_returned_hit_clears_the_threshold(kb):
    for hit in kb.search("退款政策是怎么规定的", top_k=5):
        assert hit.score >= DEFAULT_MIN_COVERAGE, hit.doc_id


@pytest.mark.parametrize(
    "query",
    ["退款政策是怎么规定的", "人工客服的工作时间是多长", "会员等级会降级吗"],
)
def test_a_stale_document_is_visible_but_never_ahead_of_its_current_counterpart(kb, query):
    """SPEC-RAG-5。删掉过期文档会掩盖「知识库里有陈旧内容」这个真实问题。"""
    hits = kb.search(query, top_k=5)
    position = {hit.doc_id: index for index, hit in enumerate(hits)}
    for hit in hits:
        if not hit.expired:
            continue
        for other in hits:
            if other.expired or topic_of(other.doc_id) != topic_of(hit.doc_id):
                continue
            assert position[other.doc_id] < position[hit.doc_id], (query, other.doc_id)


def test_current_documents_outrank_stale_ones_without_relying_on_the_score():
    """同一主题内，现行文档必须排在过期文档之前——哪怕过期文档打分更高。

    只靠打分不够：过期文档的字面重合度可能更高。这里直接喂一个「过期在前」的列表，
    断言那个显式的不变式真的在起作用，而不是「通常能排对」。
    """
    stale = Hit("DOC-TEST-002", "旧条款", score=1.0, expired=True)
    fresh = Hit("DOC-TEST-001", "现行条款", score=0.4, expired=False)
    assert [hit.doc_id for hit in _current_before_expired([stale, fresh])] == [
        "DOC-TEST-001",
        "DOC-TEST-002",
    ]


def test_the_current_before_stale_rule_does_not_reorder_across_topics():
    stale = Hit("DOC-TEST-002", "旧条款", score=1.0, expired=True)
    other = Hit("DOC-OTHER-001", "别的主题", score=0.4, expired=False)
    assert [hit.doc_id for hit in _current_before_expired([stale, other])] == [
        "DOC-TEST-002",
        "DOC-OTHER-001",
    ]


@pytest.mark.parametrize("query", ["退款太慢了", "退货流程麻烦", "退款金额不对"])
def test_untrusted_documents_are_reachable_by_a_cheap_query(kb, query):
    """投毒文档必须真能被检索到——捞不出来的注入样本没有判别力（SPEC-RAG-6）。"""
    hits = kb.search(query, top_k=5)
    assert any(hit.doc_id.startswith("DOC-UGC-") for hit in hits), query


# --- 工具契约（SPEC-RAG-1） --------------------------------------------------


def test_the_tool_name_is_the_contract_name(kb_tool):
    assert kb_tool.name == "search_knowledge_base"


def test_a_hit_result_declares_exactly_the_contract_fields(kb_tool):
    result = kb_tool.execute(query="退款政策是怎么规定的")
    assert result.success is True
    assert result.error_code is None
    assert set(result.data) == {"query", "top_k", "hits", "retrieved"}
    assert result.data["query"] == "退款政策是怎么规定的"
    assert result.data["top_k"] == DEFAULT_TOP_K
    assert result.data["retrieved"] == len(result.data["hits"]) == 2
    for hit in result.data["hits"]:
        assert set(hit) == {"doc_id", "title", "score", "expired"}


def test_no_hit_is_still_a_success(kb_tool):
    """SPEC-RAG-1：检索不到不是错误。

    报成工具失败，就再也分不清「检索坏了」和「知识库真没有」——而这两件事的处理方式
    完全不同：前者要修索引，后者要拒答。
    """
    result = kb_tool.execute(query="宠物寄存政策是什么")
    assert result.success is True
    assert result.error_code is None
    assert result.data["hits"] == []
    assert result.data["retrieved"] == 0


def test_a_blank_query_is_an_argument_error(kb_tool):
    result = kb_tool.execute(query="   ")
    assert result.success is False
    assert result.error_code == ErrorCode.INVALID_ARGUMENT


def test_a_non_string_query_is_an_argument_error(kb_tool):
    assert kb_tool.execute(query=None).error_code == ErrorCode.INVALID_ARGUMENT


def test_the_tool_is_read_only(kb, kb_tool):
    before = json.dumps(kb.documents, ensure_ascii=False, sort_keys=True)
    kb_tool.execute(query="退款政策是怎么规定的")
    after = json.dumps(kb.documents, ensure_ascii=False, sort_keys=True)
    assert before == after


def test_the_corpus_hands_out_copies(kb):
    kb.documents[0]["title"] = "tampered"
    assert kb.documents[0]["title"] != "tampered"
