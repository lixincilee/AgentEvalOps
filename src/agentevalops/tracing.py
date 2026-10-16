"""Turn-level tracing: one JSONL record per user turn, optionally replayed to Langfuse.

本模块自己定四条契约，写在文件头当规格用（REQ-TRACE-1..4）：

1. **零行为改变**：没有激活的 tracer 时每个 span 都是空操作。被插桩的 agent /
   tool / retriever / planner 的输出必须逐字节不变——否则「加了可观测性」就变成了
   「换了一个被测系统」，M3 之前所有回归数字都要重跑。
2. **只记该记的**：属性有长度上限；密钥、邮箱、手机号在写盘前脱敏。trace 是要
   入库、要给人看的，把 API Key 原样写进去等于自己造一个泄露面。
3. **一条 trace 一行 JSON**：离线可跑、可 diff、可 grep，不依赖任何服务。
4. **Langfuse 是可选出口**：SDK 缺失或没配 key 时降级为「不发送 + 把本来要发的
   载荷落盘」，而不是静默丢数据，也不是让整轮评测崩掉。

时间口径：`start_ms` / `duration_ms` 都是相对 trace 起点的毫秒偏移，由
`time.perf_counter()` 得到（单调时钟，不怕系统时间被改）。`started_at` 才是墙上时间。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

#: 落盘格式的版本号。改字段语义时改它，读方按它分支。
SCHEMA = "agentevalops.trace/v1"

#: 单个属性值的字符上限。检索结果和模型输出都可能很长，整条塞进 trace 会让
#: 一份 trace 比被评测的报告还大，真正要看的那几行反而被淹掉。
DEFAULT_MAX_ATTR_CHARS = 400

#: 列表类属性最多记多少项。
DEFAULT_MAX_LIST_ITEMS = 20

MASK = "***"


class SpanKind(StrEnum):
    """span 类型。分类型不是为了好看：失败定位要先问「是检索、模型、工具还是状态」。"""

    TURN = "turn"  #: 一次 handle() 调用 = 一整轮
    NODE = "node"  #: LangGraph 节点
    LLM = "llm"  #: 模型调用
    RETRIEVER = "retriever"  #: 知识库检索
    TOOL = "tool"  #: 业务工具
    STATE = "state"  #: 会话状态读写


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


# --- 脱敏与截断 ---------------------------------------------------------------

#: 密钥类「键名」。命中即整值替换，不看值长什么样——键名叫 api_key 的时候
#: 值是不是 sk- 开头已经不重要了。
_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|token|authorization|password|passwd|credential|cookie)",
    re.IGNORECASE,
)

#: 计数类键名里带 token，但它们是**计数**不是凭据。把 ``prompt_tokens`` 脱成 ``***``，
#: 等于顺手把成本指标从 trace 里删掉——静默丢数据比不脱敏更糟。
_COUNTING_HINTS = (
    "prompt", "completion", "total", "cache", "max", "usage", "count", "budget",
    "limit", "per_",
)


def _is_sensitive_key(name: str) -> bool:
    """键名看起来是不是凭据。带计数提示词的 ``*_tokens`` 一律不算。"""
    key = name.strip().lower()
    if "token" in key and any(hint in key for hint in _COUNTING_HINTS):
        return False
    return bool(_SENSITIVE_KEY.search(key))

#: 密钥类「值形状」。有些参数（比如工具的自由文本）里会夹着用户贴进来的 key。
_SECRET_VALUE = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
#: 中国大陆手机号。前后不接数字，避免把订单号/时间戳的中间一段误伤。
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def redact_text(text: str) -> str:
    """把自由文本里的密钥 / 邮箱 / 手机号替换成 ``***``。"""
    out = text
    for pattern in _SECRET_VALUE:
        out = pattern.sub(MASK, out)
    out = _EMAIL.sub(MASK, out)
    return _PHONE.sub(MASK, out)


def clip(text: str, limit: int = DEFAULT_MAX_ATTR_CHARS) -> str:
    """截断但留下证据：告诉读的人被砍掉多少字符，而不是假装原文就这么长。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def redact_value(
    value: Any,
    *,
    limit: int = DEFAULT_MAX_ATTR_CHARS,
    max_items: int = DEFAULT_MAX_LIST_ITEMS,
    depth: int = 0,
) -> Any:
    """递归脱敏 + 截断，直到变成一个能安全落盘的普通值。"""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return clip(redact_text(value), limit)
    if depth >= 4:
        return clip(redact_text(repr(value)), limit)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            out[name] = (
                MASK
                if _is_sensitive_key(name)
                else redact_value(item, limit=limit, max_items=max_items, depth=depth + 1)
            )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        redacted = [
            redact_value(item, limit=limit, max_items=max_items, depth=depth + 1)
            for item in items[:max_items]
        ]
        if len(items) > max_items:
            redacted.append(f"…(+{len(items) - max_items} items)")
        return redacted
    return clip(redact_text(str(value)), limit)


# --- span / trace -------------------------------------------------------------


@dataclass
class Span:
    """一个已完成或正在进行的操作。"""

    name: str
    kind: SpanKind = SpanKind.NODE
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    start_ms: float = 0.0
    duration_ms: float = 0.0
    status: SpanStatus = SpanStatus.OK
    attributes: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    #: 记录时的长度上限，不进序列化。
    limit: int = DEFAULT_MAX_ATTR_CHARS

    def set(self, **attributes: Any) -> Span:
        """补记属性。允许在 span 结束后补——节点要把「跑完才知道的结果」记上来。"""
        for key, value in attributes.items():
            self.attributes[key] = redact_value(value, limit=self.limit)
        return self

    def fail(self, code: str | None, message: Any) -> Span:
        """标记为失败。错误码单独放字段，因为它要能被 `group by` 统计。"""
        self.status = SpanStatus.ERROR
        self.error_code = None if code is None else str(code)
        self.error_message = clip(redact_text(str(message)), self.limit)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": str(self.kind),
            "start_ms": round(self.start_ms, 3),
            "duration_ms": round(self.duration_ms, 3),
            "status": str(self.status),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attributes": self.attributes,
        }


class _NullSpan:
    """没有激活 tracer 时用的替身：所有方法都是空操作。

    存在的意义是让插桩点写成一段代码而不是两段——「有 tracer / 没 tracer」的分支
    只允许出现在这里，不允许出现在业务代码里。
    """

    name = ""
    kind = SpanKind.NODE
    attributes: dict[str, Any] = {}
    status = SpanStatus.OK
    error_code: str | None = None

    def set(self, **attributes: Any) -> _NullSpan:
        return self

    def fail(self, code: str | None, message: Any) -> _NullSpan:
        return self


NULL_SPAN = _NullSpan()


@dataclass
class Trace:
    """一整轮：一个根 span + 若干子 span。"""

    started_at: str
    trace_id: str = field(default_factory=lambda: f"tr_{uuid.uuid4().hex[:20]}")
    duration_ms: float = 0.0
    status: SpanStatus = SpanStatus.OK
    spans: list[Span] = field(default_factory=list)
    run_id: str | None = None
    case_id: str | None = None
    version: dict[str, Any] | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def root(self) -> Span | None:
        return next((span for span in self.spans if span.parent_id is None), None)

    def by_name(self, name: str) -> Span | None:
        return next((span for span in self.spans if span.name == name), None)

    def of_kind(self, kind: SpanKind) -> list[Span]:
        return [span for span in self.spans if str(span.kind) == str(kind)]

    def errors(self) -> list[Span]:
        return [span for span in self.spans if span.status == SpanStatus.ERROR]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 3),
            "status": str(self.status),
            "run_id": self.run_id,
            "case_id": self.case_id,
            "version": self.version,
            "attributes": self.attributes,
            "spans": [span.to_dict() for span in self.spans],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Trace:
        spans = []
        for raw in payload.get("spans", ()):
            spans.append(
                Span(
                    name=raw["name"],
                    kind=SpanKind(raw.get("kind", "node")),
                    span_id=raw.get("span_id", ""),
                    parent_id=raw.get("parent_id"),
                    start_ms=float(raw.get("start_ms", 0.0)),
                    duration_ms=float(raw.get("duration_ms", 0.0)),
                    status=SpanStatus(raw.get("status", "ok")),
                    attributes=dict(raw.get("attributes", {})),
                    error_code=raw.get("error_code"),
                    error_message=raw.get("error_message"),
                )
            )
        return cls(
            started_at=payload.get("started_at", ""),
            trace_id=payload.get("trace_id", ""),
            duration_ms=float(payload.get("duration_ms", 0.0)),
            status=SpanStatus(payload.get("status", "ok")),
            spans=spans,
            run_id=payload.get("run_id"),
            case_id=payload.get("case_id"),
            version=payload.get("version"),
            attributes=dict(payload.get("attributes", {})),
        )


# --- 出口 ---------------------------------------------------------------------


class TraceEmitter(Protocol):
    def emit(self, trace: Trace) -> None: ...


@dataclass
class MemoryEmitter:
    """测试与自检用：把 trace 留在内存里。"""

    traces: list[Trace] = field(default_factory=list)

    def emit(self, trace: Trace) -> None:
        self.traces.append(trace)


class MultiEmitter:
    """一条 trace 交给多个出口。任一出口抛异常都不影响其它出口与主流程。

    观测层不许把被测系统带崩：Langfuse 连不上是「少了一份远端副本」，不是「这一轮评测失败」。
    """

    def __init__(self, *emitters: TraceEmitter) -> None:
        self.emitters = [emitter for emitter in emitters if emitter is not None]

    def emit(self, trace: Trace) -> None:
        for emitter in self.emitters:
            try:
                emitter.emit(trace)
            except Exception:  # pragma: no cover - 出口出问题是环境问题，不是被测系统的问题
                continue


class JsonlEmitter:
    """一条 trace 一行。显式 ``newline="\\n"``，Windows 上也不写出 CRLF。

    仓库有 .gitattributes 钉 eol=lf：默认的文本模式会用 os.linesep 写出 CRLF，
    于是同一份产物在 Windows 和 CI 上 diff 出满屏差异。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def emit(self, trace: Trace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(trace.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{line}\n")

    def read(self) -> list[Trace]:
        if not self.path.exists():
            return []
        traces = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                traces.append(Trace.from_dict(json.loads(line)))
        return traces


def _span_io(span: Span) -> tuple[Any, Any]:
    """给 Langfuse 的 input / output。不同 kind 该看的东西不一样：

    检索看 query 与命中 doc，工具看参数与结果，模型看 prompt 与响应。
    把三者混成同一个字段，等于把「定位到哪一层」这件事又还给了读日志的人。
    """
    attrs = span.attributes
    kind = str(span.kind)
    if kind == str(SpanKind.RETRIEVER):
        return (
            {"query": attrs.get("query"), "top_k": attrs.get("top_k")},
            {
                "hits": attrs.get("hits"),
                "doc_ids": attrs.get("doc_ids"),
                "grounded": attrs.get("grounded"),
            },
        )
    if kind == str(SpanKind.TOOL):
        return (
            attrs.get("arguments"),
            {
                "success": attrs.get("success"),
                "error_code": attrs.get("error_code"),
                "result": attrs.get("result"),
            },
        )
    if kind == str(SpanKind.LLM):
        return (
            {"system": attrs.get("system_prompt"), "user": attrs.get("user_message")},
            {"response": attrs.get("response"), "plan_source": attrs.get("plan_source")},
        )
    return None, None


def langfuse_payload(trace: Trace) -> list[dict[str, Any]]:
    """把一条已完成的 trace 摊成 Langfuse 的 span 列表（纯函数）。

    纯函数是有意的：Langfuse 服务本机起不来（没有 Docker），但「发给 Langfuse 的
    到底是什么」这件事必须能被测试钉住——它不能因为服务不可用就没人检查。
    """
    try:
        base = datetime.fromisoformat(trace.started_at)
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)

    by_id = {span.span_id: span for span in trace.spans}
    payload: list[dict[str, Any]] = []
    for span in trace.spans:
        parent = by_id.get(span.parent_id or "")
        level = "ERROR" if span.status == SpanStatus.ERROR else "DEFAULT"
        metadata: dict[str, Any] = {
            "schema": SCHEMA,
            "kind": str(span.kind),
            "trace_id": trace.trace_id,
        }
        if trace.run_id:
            metadata["run_id"] = trace.run_id
        if trace.case_id:
            metadata["case_id"] = trace.case_id
        if trace.version:
            metadata["version"] = trace.version
        if parent is not None:
            metadata["parent_name"] = parent.name
        if span.error_code:
            metadata["error_code"] = span.error_code
        span_input, span_output = _span_io(span)
        item: dict[str, Any] = {
            "name": span.name,
            "input": span_input,
            "output": span_output,
            "start_time": (base + timedelta(milliseconds=span.start_ms)).isoformat(),
            "end_time": (
                base + timedelta(milliseconds=span.start_ms + span.duration_ms)
            ).isoformat(),
            "parent_name": None if parent is None else parent.name,
            "level": level,
            "status_message": span.error_message,
            "metadata": {**metadata, "attributes": span.attributes},
        }
        usage = span.attributes.get("usage")
        if isinstance(usage, Mapping) and (usage.get("total_tokens") or 0) > 0:
            item["usage_details"] = {
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            }
            item["model"] = span.attributes.get("model")
        payload.append(item)
    return payload


class LangfuseEmitter:
    """把 trace 回放进 Langfuse；发不出去时降级，而不是丢数据。

    三种结果都记在 ``self.status`` 上，调用方要打印出来：

    - ``live``：真发（SDK 可用 + 配了 key，或显式注入 client）。
    - ``dry-run:<原因>``：不发送，把本来要发的载荷写到 ``dump_path``。
    - ``error:<原因>``：发送过程中出错。**不抛**——观测层不该把被测系统的运行搞崩。
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
        dump_path: str | Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._client = client
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host
        self.dump_path = Path(dump_path) if dump_path else None
        self.enabled = enabled
        self.status = "idle"
        self.sent = 0
        self.dry_runs = 0

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            from langfuse import Langfuse  # noqa: PLC0415  (可选依赖：只有真发时才 import)
        except Exception:  # pragma: no cover - 取决于环境是否装了 SDK
            self.status = "dry-run:no-sdk"
            return None
        if not (self.public_key and self.secret_key):
            self.status = "dry-run:no-keys"
            return None
        self._client = Langfuse(
            public_key=self.public_key,
            secret_key=self.secret_key,
            host=self.host,
        )
        return self._client

    def _write_dump(self, trace: Trace, reason: str) -> None:
        """把「本来要发什么」落盘。状态由调用方决定。

        这里改状态曾经是个真 bug：发送失败的 ``error:RuntimeError`` 会被随后的 dump
        改写成 ``dry-run:RuntimeError``，于是调用方再也看不到「真发失败」这件事。
        """
        if self.dump_path is None:
            return
        payload = {
            "schema": SCHEMA,
            "trace_id": trace.trace_id,
            "case_id": trace.case_id,
            "reason": reason,
            "would_send": langfuse_payload(trace),
        }
        self.dump_path.parent.mkdir(parents=True, exist_ok=True)
        with self.dump_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def emit(self, trace: Trace) -> None:
        if not self.enabled:
            self.status = "disabled"
            return
        client = self._resolve_client()
        if client is None:
            self.dry_runs += 1
            self._write_dump(trace, self.status.split(":", 1)[1])
            return
        try:
            for item in langfuse_payload(trace):
                span = client.start_span(
                    name=item["name"],
                    start_time=item["start_time"],
                    input=item.get("input"),
                    output=item.get("output"),
                    metadata=item["metadata"],
                    level=item["level"],
                )
                if item.get("status_message"):
                    span.update(status_message=item["status_message"])
                if item.get("usage_details"):
                    span.update(usage_details=item["usage_details"])
                span.end(end_time=item["end_time"])
            flush = getattr(client, "flush", None)
            if callable(flush):
                flush()
            self.status = "live"
            self.sent += 1
        except Exception as exc:  # pragma: no cover - 需要真服务才走得到
            self.status = f"error:{type(exc).__name__}"
            self._write_dump(trace, type(exc).__name__)


# --- tracer -------------------------------------------------------------------


class Tracer:
    """收集一条 trace 的 span，并在 turn 结束时交给 emitter。"""

    def __init__(
        self,
        emitter: TraceEmitter | None = None,
        *,
        run_id: str | None = None,
        case_id: str | None = None,
        version: Mapping[str, Any] | None = None,
        max_attr_chars: int = DEFAULT_MAX_ATTR_CHARS,
        clock: Any = time.perf_counter,
    ) -> None:
        self.emitter = emitter
        self.run_id = run_id
        self.case_id = case_id
        self.version = None if version is None else dict(version)
        self.limit = max_attr_chars
        self.clock = clock
        self.traces: list[Trace] = []
        self.orphan_spans = 0
        self._stack: list[Span] = []
        self._trace: Trace | None = None
        self._origin = 0.0

    @property
    def active(self) -> bool:
        return self._trace is not None

    @contextmanager
    def turn(self, name: str = "agent.turn", **attributes: Any) -> Iterator[Span]:
        """开一条 trace。必须成对使用——它同时是 emitter 的触发点。"""
        trace = Trace(
            started_at=datetime.now(timezone.utc).isoformat(),
            run_id=self.run_id,
            case_id=self.case_id,
            version=self.version,
        )
        self.traces.append(trace)
        previous_trace, previous_stack = self._trace, self._stack
        self._trace, self._stack = trace, []
        self._origin = self.clock()
        root = Span(
            name=name,
            kind=SpanKind.TURN,
            start_ms=0.0,
            limit=self.limit,
        )
        root.set(**attributes)
        trace.spans.append(root)
        self._stack.append(root)
        try:
            yield root
        except BaseException as exc:  # 异常也要落盘：出错的 trace 才是要找的 trace
            if root.status != SpanStatus.ERROR:
                # 已经显式记过错误码的就别盖：``BUDGET_EXCEEDED`` 比 ``BudgetExceededError``
                # 更接近「到底出了什么事」。
                root.fail(type(exc).__name__, exc)
            root.duration_ms = (self.clock() - self._origin) * 1000
            self._stack.pop()
            self._finish(trace, root)
            self._trace, self._stack = previous_trace, previous_stack
            raise
        root.duration_ms = (self.clock() - self._origin) * 1000
        self._stack.pop()
        self._finish(trace, root)
        self._trace, self._stack = previous_trace, previous_stack

    def _finish(self, trace: Trace, root: Span) -> None:
        trace.duration_ms = root.duration_ms
        trace.status = (
            SpanStatus.ERROR
            if any(span.status == SpanStatus.ERROR for span in trace.spans)
            else SpanStatus.OK
        )
        if self.emitter is not None:
            self.emitter.emit(trace)

    @contextmanager
    def span(
        self, name: str, kind: SpanKind = SpanKind.NODE, **attributes: Any
    ) -> Iterator[Span]:
        """开一个子 span。没有正在进行的 turn 时记一次 orphan 并降级为空操作。"""
        if self._trace is None:
            self.orphan_spans += 1
            yield NULL_SPAN
            return
        parent = self._stack[-1] if self._stack else None
        span = Span(
            name=name,
            kind=kind,
            parent_id=None if parent is None else parent.span_id,
            start_ms=max(0.0, (self.clock() - self._origin) * 1000),
            limit=self.limit,
        )
        span.set(**attributes)
        self._trace.spans.append(span)
        self._stack.append(span)
        started = self.clock()
        try:
            yield span
        except BaseException as exc:
            if span.status != SpanStatus.ERROR:
                span.fail(type(exc).__name__, exc)
            raise
        finally:
            span.duration_ms = (self.clock() - started) * 1000
            self._stack.pop()
            if parent is not None:
                parent.duration_ms = max(
                    parent.duration_ms,
                    span.start_ms + span.duration_ms - parent.start_ms,
                )
            else:
                trace = self._trace
                if trace is not None:
                    trace.duration_ms = max(trace.duration_ms, span.start_ms + span.duration_ms)


#: 当前激活的 tracer。用 ContextVar 而不是全局变量：压测服务在线程池里跑，
#: 全局变量会让并发请求的 trace 互相污染。
_ACTIVE: ContextVar[Tracer | None] = ContextVar("agentevalops_tracer", default=None)


def current_tracer() -> Tracer | None:
    return _ACTIVE.get()


@contextmanager
def tracing(tracer: Tracer | None) -> Iterator[Tracer | None]:
    """在这个上下文里激活 tracer。``None`` 表示明确关掉。"""
    token = _ACTIVE.set(tracer)
    try:
        yield tracer
    finally:
        _ACTIVE.reset(token)


@contextmanager
def span(
    name: str, kind: SpanKind = SpanKind.NODE, **attributes: Any
) -> Iterator[Span | _NullSpan]:
    """插桩点用的统一入口：有没有 tracer、有没有 turn，都只有这一种写法。"""
    tracer = _ACTIVE.get()
    if tracer is None or not tracer.active:
        yield NULL_SPAN
        return
    with tracer.span(name, kind, **attributes) as active_span:
        yield active_span


def read_traces(path: str | Path) -> list[Trace]:
    """读回一份 JSONL trace 文件。坏行直接跳过——报告不该因为一行截断就整体失败。"""
    traces: list[Trace] = []
    target = Path(path)
    if not target.exists():
        return traces
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            traces.append(Trace.from_dict(json.loads(line)))
        except (ValueError, KeyError, TypeError):
            continue
    return traces


def iter_lines(traces: Sequence[Trace]) -> Iterator[str]:
    """给报告/视图脚本复用同一套序列化口径。"""
    for trace in traces:
        yield json.dumps(trace.to_dict(), ensure_ascii=False)