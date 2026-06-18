"""Codex Desktop App 本地状态的只读扫描与事件转换。

本模块负责读取 Codex App 写入本机的 `state_*.sqlite` 和 thread rollout JSONL，
识别仍未返回 `function_call_output` 的 `request_user_input` 调用，并把它们转换成
Agent Deck 能消费的 `input.requested` 事件。它不启动 Codex、不操作 App UI、不写
`~/.codex`、不连接 daemon，也不修改 SQLite 或 JSONL；调用方如果要同步到 daemon，
必须显式把生成的事件发送出去。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_deck.core.events import AgentSource, EventType, NormalizedEvent

_STATE_DB_GLOB = "state_*.sqlite"
_REQUEST_USER_INPUT_TOOL = "request_user_input"
_CODEX_APP_REQUEST_SOURCE_EVENT = "codex-app.request_user_input"


class CodexUserInputRequest(BaseModel):
    """描述 Codex App 中一个仍在等待用户响应的 Plan Mode 输入请求。

    入参：字段来自 rollout JSONL 中的 `request_user_input` function call，包括 call id、
    问题 id、问题文本、选项标签、自动超时毫秒数、请求时间和 JSONL 行号。
    返回：不可变 Pydantic model，可嵌入 thread snapshot 或转换为 normalized event。
    错误处理：字段类型或 naive datetime 会由 Pydantic 校验报告。
    副作用：无；模型只保存扫描器已解析出的内存数据。
    """

    model_config = ConfigDict(frozen=True)

    call_id: str
    question: str | None
    question_id: str | None
    option_labels: tuple[str, ...] = ()
    auto_resolution_ms: int | None = None
    requested_at: datetime
    line_number: int

    @field_validator("requested_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime) -> datetime:
        """拒绝 naive datetime，避免跨时区比较时猜测本地时间。

        入参：`value` 是请求时间。
        返回：原始 timezone-aware datetime。
        错误处理：datetime 缺少 tzinfo 或 utcoffset 时抛出 ValueError。
        副作用：无；只检查内存字段。
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        return value


class CodexAppThreadSnapshot(BaseModel):
    """描述 Codex App 一个 thread 的只读扫描快照。

    入参：字段来自 `threads` 表和对应 rollout JSONL；`status` 是 Agent Deck 用于展示的
    粗粒度状态，当前只在发现待用户输入时标记为 `waiting_user`。
    返回：不可变 Pydantic model，可直接序列化给 CLI。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不访问文件或数据库。
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str
    title: str | None
    cwd: str | None
    rollout_path: str
    updated_at: int | None = None
    status: str
    pending_user_input: CodexUserInputRequest | None = None


class CodexAppStateReport(BaseModel):
    """汇总一次 Codex App 本地状态扫描结果。

    入参：`codex_home` 是本次扫描使用的 Codex home；`state_db_path` 是选中的 SQLite；
    `threads` 是按 `updated_at` 倒序读取的 thread 快照。
    返回：不可变 Pydantic model，可由 CLI 作为 JSON 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型只保存扫描结果，不保留打开的文件或数据库连接。
    """

    model_config = ConfigDict(frozen=True)

    codex_home: str
    state_db_path: str
    threads: tuple[CodexAppThreadSnapshot, ...] = Field(default_factory=tuple)


def scan_codex_app_state(
    *,
    codex_home: Path | None = None,
    state_db_path: Path | None = None,
    limit: int = 20,
) -> CodexAppStateReport:
    """扫描 Codex App 本地数据库和 rollout，返回 thread 状态报告。

    入参：`codex_home` 默认是 `~/.codex`；`state_db_path` 可显式指定 SQLite 文件；
    `limit` 限制读取最近 thread 数量，必须为正数。
    返回：`CodexAppStateReport`，其中待用户响应的 thread 会带有 `pending_user_input`。
    错误处理：找不到 state DB、SQLite 查询失败、limit 非正数或文件读取失败会抛异常；
    单行 rollout JSON 解析失败会被跳过，不阻断整个扫描。
    副作用：只读访问本机文件系统和 SQLite；不写文件、不启动进程、不连接网络。
    """

    if limit <= 0:
        raise ValueError("limit must be positive")

    resolved_home = _resolve_codex_home(codex_home)
    resolved_db_path = _resolve_state_db_path(resolved_home, state_db_path)
    rows = _load_thread_rows(resolved_db_path, limit=limit)
    threads = tuple(_thread_snapshot_from_row(row) for row in rows)
    return CodexAppStateReport(
        codex_home=str(resolved_home),
        state_db_path=str(resolved_db_path),
        threads=threads,
    )


def build_codex_app_state_events(
    *,
    codex_home: Path | None = None,
    state_db_path: Path | None = None,
    limit: int = 20,
) -> tuple[NormalizedEvent, ...]:
    """扫描 Codex App 状态并生成可发送给 daemon 的 normalized events。

    入参：`codex_home`、`state_db_path` 和 `limit` 直接传给 `scan_codex_app_state`。
    返回：只包含待用户响应 thread 的 `input.requested` 事件元组。
    错误处理：扫描阶段的文件、SQLite 或参数错误会向调用方传播；事件字段非法时由
    `NormalizedEvent` 校验报告。
    副作用：只读访问 Codex 本地状态文件；不发送 HTTP、不写文件、不修改 Codex 状态。
    """

    report = scan_codex_app_state(
        codex_home=codex_home,
        state_db_path=state_db_path,
        limit=limit,
    )
    return build_codex_app_state_events_from_report(report)


def build_codex_app_state_events_from_report(
    report: CodexAppStateReport,
) -> tuple[NormalizedEvent, ...]:
    """把已有 Codex App scan report 转成 normalized events。

    入参：`report` 是同模块扫描器返回的只读状态报告。
    返回：每个 pending user input 对应一个 `EventType.INPUT_REQUESTED` 事件。
    错误处理：事件字段不满足 `NormalizedEvent` 约束时抛出 Pydantic 校验异常。
    副作用：只读取内存 report 并读取当前 UTC 作为事件接收时间；不访问文件或网络。
    """

    events: list[NormalizedEvent] = []
    for thread in report.threads:
        request = thread.pending_user_input
        if request is None:
            continue
        events.append(_event_from_thread_request(thread, request))
    return tuple(events)


def _resolve_codex_home(codex_home: Path | None) -> Path:
    """解析 Codex home 路径。

    入参：`codex_home` 是可选覆盖路径。
    返回：展开 `~` 后的绝对路径；为空时返回当前用户 `~/.codex`。
    错误处理：路径对象解析失败时按 pathlib 语义传播。
    副作用：读取当前用户 home 路径；不访问文件内容。
    """

    return (codex_home or Path.home() / ".codex").expanduser().resolve()


def _resolve_state_db_path(codex_home: Path, state_db_path: Path | None) -> Path:
    """选择要读取的 Codex App state SQLite 文件。

    入参：`codex_home` 是已解析的 Codex home；`state_db_path` 是可选显式 SQLite 路径。
    返回：显式路径或 `codex_home` 下最新修改的 `state_*.sqlite`。
    错误处理：显式路径不存在或找不到候选文件时抛 FileNotFoundError。
    副作用：只读 stat/glob 文件系统元数据，不写文件。
    """

    if state_db_path is not None:
        resolved = state_db_path.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Codex state DB not found: {resolved}")
        return resolved

    candidates = [path for path in codex_home.glob(_STATE_DB_GLOB) if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No Codex state DB found under {codex_home}")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _load_thread_rows(db_path: Path, *, limit: int) -> tuple[dict[str, Any], ...]:
    """从 Codex App SQLite 读取最近的未归档 thread 行。

    入参：`db_path` 是 SQLite 文件路径；`limit` 是最大 thread 数。
    返回：每行一个 dict，包含 id、title、cwd、rollout_path、updated_at。
    错误处理：SQLite 打开或查询失败由 sqlite3 抛出。
    副作用：以只读 URI 打开 SQLite 并执行 SELECT；不写数据库。
    """

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            select id, title, cwd, rollout_path, updated_at
            from threads
            where coalesce(archived, 0) = 0
            order by updated_at desc
            limit ?
            """,
            (limit,),
        )
        return tuple(dict(row) for row in cursor.fetchall())
    finally:
        conn.close()


def _thread_snapshot_from_row(row: Mapping[str, Any]) -> CodexAppThreadSnapshot:
    """把一行 threads 表记录转换为 scan snapshot。

    入参：`row` 是 `_load_thread_rows` 返回的 mapping。
    返回：包含 pending user input 状态的 `CodexAppThreadSnapshot`。
    错误处理：缺少 thread id 或 rollout path 时抛 ValueError；rollout 文件不存在时由
    `_latest_pending_user_input` 返回 None，并把状态标记为 observed。
    副作用：只读解析该 thread 对应 rollout JSONL。
    """

    thread_id = _required_string(row, "id")
    rollout_path = _required_string(row, "rollout_path")
    pending_user_input = _latest_pending_user_input(Path(rollout_path))
    return CodexAppThreadSnapshot(
        thread_id=thread_id,
        title=_optional_string(row.get("title")),
        cwd=_optional_string(row.get("cwd")),
        rollout_path=rollout_path,
        updated_at=_optional_int(row.get("updated_at")),
        status="waiting_user" if pending_user_input is not None else "observed",
        pending_user_input=pending_user_input,
    )


def _latest_pending_user_input(rollout_path: Path) -> CodexUserInputRequest | None:
    """从 rollout JSONL 中找出最新未完成的 `request_user_input` 调用。

    入参：`rollout_path` 是 thread 对应 JSONL 文件。
    返回：最新一个没有匹配 `function_call_output` 的请求；没有或文件缺失时返回 None。
    错误处理：单行 JSON 或 arguments JSON 非法会跳过对应行；文件编码错误由运行时传播。
    副作用：只读打开 rollout 文件。
    """

    if not rollout_path.exists():
        return None

    requests: dict[str, CodexUserInputRequest] = {}
    completed_call_ids: set[str] = set()
    with rollout_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            row = _loads_json_object(line)
            if row is None:
                continue
            payload = row.get("payload")
            if not isinstance(payload, Mapping):
                continue
            payload_type = payload.get("type")
            call_id = _optional_string(payload.get("call_id"))
            if not call_id:
                continue
            if payload_type == "function_call_output":
                completed_call_ids.add(call_id)
                continue
            if (
                payload_type == "function_call"
                and payload.get("name") == _REQUEST_USER_INPUT_TOOL
            ):
                request = _user_input_request_from_payload(
                    payload,
                    row=row,
                    call_id=call_id,
                    line_number=line_number,
                )
                if request is not None:
                    requests[call_id] = request

    pending = [
        request
        for call_id, request in requests.items()
        if call_id not in completed_call_ids
    ]
    if not pending:
        return None
    return max(pending, key=lambda request: (request.requested_at, request.line_number))


def _user_input_request_from_payload(
    payload: Mapping[str, Any],
    *,
    row: Mapping[str, Any],
    call_id: str,
    line_number: int,
) -> CodexUserInputRequest | None:
    """解析 `request_user_input` function call payload。

    入参：`payload` 是 rollout row 内的 function_call payload；`row` 是外层 JSONL 行；
    `call_id` 是已校验的调用 id；`line_number` 是 JSONL 行号。
    返回：解析成功的 `CodexUserInputRequest`；arguments 不是 JSON object 时返回 None。
    错误处理：arguments JSON 非法、questions 结构异常或时间戳非法时尽量降级；无法解析
    arguments object 时跳过该 call。
    副作用：无；只处理内存数据。
    """

    arguments = _loads_json_object(_optional_string(payload.get("arguments")) or "")
    if arguments is None:
        return None
    question = _first_question(arguments)
    return CodexUserInputRequest(
        call_id=call_id,
        question=_optional_string(question.get("question")),
        question_id=_optional_string(question.get("id")),
        option_labels=tuple(_option_labels(question.get("options"))),
        auto_resolution_ms=_optional_int(arguments.get("autoResolutionMs")),
        requested_at=_parse_timestamp(row.get("timestamp")),
        line_number=line_number,
    )


def _event_from_thread_request(
    thread: CodexAppThreadSnapshot,
    request: CodexUserInputRequest,
) -> NormalizedEvent:
    """把一个待用户响应请求映射为 Agent Deck normalized event。

    入参：`thread` 是 Codex App thread 快照；`request` 是该 thread 的 pending 输入请求。
    返回：`EventType.INPUT_REQUESTED` 的 `NormalizedEvent`。
    错误处理：事件字段非法时由 `NormalizedEvent.build` 抛出校验异常。
    副作用：读取当前 UTC 作为 received_at；不访问文件或网络。
    """

    return NormalizedEvent.build(
        source=AgentSource.CODEX,
        source_event_type=_CODEX_APP_REQUEST_SOURCE_EVENT,
        normalized_type=EventType.INPUT_REQUESTED,
        session_id=thread.thread_id,
        thread_id=thread.thread_id,
        cwd=thread.cwd,
        title=thread.title,
        summary=request.question,
        payload={
            "call_id": request.call_id,
            "question_id": request.question_id,
            "question": request.question,
            "option_labels": list(request.option_labels),
            "auto_resolution_ms": request.auto_resolution_ms,
            "rollout_path": thread.rollout_path,
            "line_number": request.line_number,
            "detected_by": "codex_app_state_scan",
        },
        occurred_at=request.requested_at,
    )


def _loads_json_object(raw: str) -> dict[str, Any] | None:
    """把 JSON 字符串解析为 object，非法或非 object 时返回 None。

    入参：`raw` 是 JSON 文本。
    返回：dict 或 None。
    错误处理：JSONDecodeError 被吞掉并返回 None，避免单行坏数据阻断扫描。
    副作用：无；只处理内存字符串。
    """

    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _first_question(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """从 request_user_input arguments 中取第一个问题对象。

    入参：`arguments` 是已解析的 JSON object。
    返回：第一个 question object；缺失或结构不匹配时返回空 mapping。
    错误处理：本函数不抛业务异常；非标准结构会被降级为空对象。
    副作用：无；只读取内存 mapping。
    """

    questions = arguments.get("questions")
    if not isinstance(questions, list) or not questions:
        return {}
    first = questions[0]
    return first if isinstance(first, Mapping) else {}


def _option_labels(options: Any) -> Iterable[str]:
    """提取 request_user_input 选项标签。

    入参：`options` 通常是 list[object]，每项可能包含 `label`。
    返回：字符串标签迭代器，忽略空值和非字符串。
    错误处理：本函数不抛业务异常；非 list 输入返回空迭代。
    副作用：无；只读取内存数据。
    """

    if not isinstance(options, list):
        return ()
    labels: list[str] = []
    for option in options:
        if not isinstance(option, Mapping):
            continue
        label = _optional_string(option.get("label"))
        if label is not None:
            labels.append(label)
    return tuple(labels)


def _parse_timestamp(value: Any) -> datetime:
    """解析 rollout JSONL timestamp 为 timezone-aware datetime。

    入参：`value` 应是 ISO 8601 字符串，Codex 常用 `Z` 表示 UTC。
    返回：解析出的 datetime；缺失或非法时返回当前 UTC。
    错误处理：解析异常被吞掉并降级到 `datetime.now(UTC)`，避免坏时间戳阻断扫描。
    副作用：降级路径会读取当前系统时间；不访问外部 I/O。
    """

    if not isinstance(value, str) or not value.strip():
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _required_string(row: Mapping[str, Any], key: str) -> str:
    """读取必须存在的非空字符串字段。

    入参：`row` 是 SQLite 行 mapping；`key` 是字段名。
    返回：非空字符串。
    错误处理：缺失、非字符串或空字符串时抛 ValueError。
    副作用：无；只读取内存 mapping。
    """

    value = _optional_string(row.get(key))
    if value is None:
        raise ValueError(f"Codex state row missing {key}")
    return value


def _optional_string(value: Any) -> str | None:
    """把非空字符串值规范化为 str 或 None。

    入参：`value` 是任意 SQLite/JSON 字段值。
    返回：去掉首尾空白后的字符串；非字符串或空字符串返回 None。
    错误处理：本函数不主动抛异常。
    副作用：无；只处理内存值。
    """

    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(value: Any) -> int | None:
    """把可安全表达为整数的值转成 int。

    入参：`value` 是 SQLite/JSON 字段值。
    返回：int 或 None；bool 被视为非整数以避免误把开关当计数。
    错误处理：转换失败时返回 None。
    副作用：无；只处理内存值。
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None
