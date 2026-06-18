"""Codex App 本地状态扫描器的单元测试。

本文件只验证对 pytest 临时 SQLite 和 rollout JSONL 的只读解析契约。测试不读取真实
`~/.codex`，不启动 Codex App，不连接 Agent Deck daemon，也不写用户配置。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_deck.adapters.codex_app_state import (
    build_codex_app_state_events,
    scan_codex_app_state,
)
from agent_deck.core.events import EventType


def _create_state_db(path: Path, rollout_path: Path) -> None:
    """创建包含一个 Codex thread 的最小 state SQLite 数据库。

    入参：`path` 是数据库路径；`rollout_path` 是 thread 指向的 JSONL 文件。
    返回：无返回值。
    错误处理：SQLite 写入失败由 sqlite3 抛出并交给 pytest。
    副作用：在 pytest 临时目录创建 SQLite 文件。
    """

    conn = sqlite3.connect(path)
    conn.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            model_provider text not null,
            cwd text not null,
            title text not null,
            sandbox_policy text not null,
            approval_mode text not null,
            archived integer not null default 0,
            preview text not null default ''
        )
        """
    )
    conn.execute(
        """
        insert into threads (
            id, rollout_path, created_at, updated_at, source, model_provider,
            cwd, title, sandbox_policy, approval_mode, archived, preview
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "thread-1",
            str(rollout_path),
            1781768400,
            1781768538,
            "vscode",
            "openai",
            "/repo",
            "请求用户选择选项",
            "{}",
            "never",
            0,
            "preview",
        ),
    )
    conn.commit()
    conn.close()


def _write_rollout(path: Path) -> None:
    """写入同时包含已完成和未完成 request_user_input 的 rollout JSONL。

    入参：`path` 是 JSONL 路径。
    返回：无返回值。
    错误处理：文件写入或 JSON 序列化失败由标准异常报告。
    副作用：在 pytest 临时目录写入一个 JSONL 文件。
    """

    rows = [
        {
            "timestamp": "2026-06-18T07:39:30.147Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "completed-call",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "id": "done",
                                "question": "已完成问题",
                                "options": [{"label": "A"}],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            },
        },
        {
            "timestamp": "2026-06-18T07:39:31.147Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "completed-call",
                "output": "{}",
            },
        },
        {
            "timestamp": "2026-06-18T07:42:18.871Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "pending-call",
                "arguments": json.dumps(
                    {
                        "questions": [
                            {
                                "header": "选项测试",
                                "id": "choice_test",
                                "question": "请选择一个测试选项",
                                "options": [
                                    {"label": "A 字符串 (Recommended)"},
                                    {"label": "B 字符串"},
                                ],
                            }
                        ],
                        "autoResolutionMs": 60000,
                    },
                    ensure_ascii=False,
                ),
            },
        },
    ]
    path.write_text(
        "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows),
        encoding="utf-8",
    )


def test_scan_codex_app_state_detects_pending_user_input(tmp_path: Path) -> None:
    """验证扫描器能发现未完成的 Codex App request_user_input。

    入参：`tmp_path` 提供 fake Codex home、state DB 和 rollout JSONL。
    返回：无返回值；断言通过代表已完成 call 被忽略，未完成 call 被映射为 waiting_user。
    错误处理：扫描器解析错误或字段不符合预期时由 pytest 断言报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout_path = tmp_path / "rollout.jsonl"
    state_db = codex_home / "state_5.sqlite"
    _write_rollout(rollout_path)
    _create_state_db(state_db, rollout_path)

    report = scan_codex_app_state(codex_home=codex_home)

    assert len(report.threads) == 1
    thread = report.threads[0]
    assert thread.thread_id == "thread-1"
    assert thread.status == "waiting_user"
    assert thread.pending_user_input is not None
    assert thread.pending_user_input.call_id == "pending-call"
    assert thread.pending_user_input.question == "请选择一个测试选项"
    assert thread.pending_user_input.option_labels == (
        "A 字符串 (Recommended)",
        "B 字符串",
    )
    assert thread.pending_user_input.auto_resolution_ms == 60000


def test_build_codex_app_state_events_maps_waiting_input(tmp_path: Path) -> None:
    """验证待用户输入的 Codex App thread 会生成 input.requested 事件。

    入参：`tmp_path` 提供 fake Codex home、state DB 和 rollout JSONL。
    返回：无返回值；断言通过代表事件可直接喂给 Agent Deck daemon。
    错误处理：事件类型、session/cwd/summary/payload 不符合预期时由 pytest 报告。
    副作用：只读扫描 pytest 临时目录中的 fake 文件。
    """

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    rollout_path = tmp_path / "rollout.jsonl"
    _write_rollout(rollout_path)
    _create_state_db(codex_home / "state_5.sqlite", rollout_path)

    events = build_codex_app_state_events(codex_home=codex_home)

    assert len(events) == 1
    event = events[0]
    assert event.normalized_type == EventType.INPUT_REQUESTED
    assert event.session_id == "thread-1"
    assert event.thread_id == "thread-1"
    assert event.cwd == "/repo"
    assert event.title == "请求用户选择选项"
    assert event.summary == "请选择一个测试选项"
    assert event.payload["call_id"] == "pending-call"
