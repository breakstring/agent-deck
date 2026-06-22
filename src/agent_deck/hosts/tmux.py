"""tmux 只读快照读取与 pane/client 匹配。

本模块只通过 `tmux list-panes` 和 `tmux list-clients` 读取当前 tmux 状态，并把输出
转换为可测试的内存模型。读取失败、tmux 不存在或超时时返回空快照，不阻断 Codex
host resolver 的其他检测路径。
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from pydantic import BaseModel, ConfigDict

_PANE_FORMAT = (
    "#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{session_name}\t"
    "#{window_id}\t#{window_index}\t#{pane_index}\t#{pane_current_path}"
)
_CLIENT_FORMAT = "#{client_tty}\t#{client_pid}\t#{session_name}\t#{client_activity}"


class TmuxPane(BaseModel):
    """描述一个 tmux pane 的只读快照。

    入参：字段来自 `tmux list-panes`，包括 pane id、TTY、PID、session/window/pane 坐标和
    当前路径。
    返回：不可变模型实例。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不访问 tmux。
    """

    model_config = ConfigDict(frozen=True)

    pane_id: str
    pane_tty: str | None = None
    pane_pid: int | None = None
    session_name: str
    window_id: str | None = None
    window_index: int | None = None
    pane_index: int | None = None
    current_path: str | None = None


class TmuxClient(BaseModel):
    """描述一个 tmux attached client 的只读快照。

    入参：字段来自 `tmux list-clients`，包括 client TTY、client PID、session 名称和
    最近活动时间戳。
    返回：不可变模型实例。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不访问 tmux。
    """

    model_config = ConfigDict(frozen=True)

    client_tty: str | None = None
    client_pid: int | None = None
    session_name: str
    client_activity: int | None = None


class TmuxSnapshot(BaseModel):
    """汇总一次 tmux 只读快照。

    入参：`panes` 是当前所有 pane；`clients` 是当前 attached clients。
    返回：不可变模型实例。
    错误处理：字段类型非法时由 Pydantic 抛出 ValidationError。
    副作用：只保存内存字段，不访问 tmux。
    """

    model_config = ConfigDict(frozen=True)

    panes: tuple[TmuxPane, ...] = ()
    clients: tuple[TmuxClient, ...] = ()


class TmuxReader(Protocol):
    """读取 tmux 快照的协议。

    入参：实现方可以是真实 subprocess reader 或测试静态 reader。
    返回：`read` 返回当前 `TmuxSnapshot`。
    错误处理：生产实现应吞掉 tmux 缺失、超时和非零退出并返回空快照。
    副作用：协议本身无副作用；具体实现决定是否调用 tmux。
    """

    def read(self) -> TmuxSnapshot:
        """读取 tmux 快照。

        入参：无。
        返回：`TmuxSnapshot`。
        错误处理：具体实现可选择降级为空快照。
        副作用：静态实现无副作用，生产实现只读调用 tmux。
        """


class StaticTmuxReader:
    """测试用静态 tmux reader。

    入参：`snapshot` 是可选固定快照；为空时返回空快照。
    返回：提供 `read` 的 reader。
    错误处理：不主动抛业务异常。
    副作用：只保存内存快照。
    """

    def __init__(self, snapshot: TmuxSnapshot | None = None) -> None:
        """创建静态 tmux reader。

        入参：`snapshot` 是固定返回值。
        返回：无显式返回值。
        错误处理：不主动抛业务异常。
        副作用：只保存内存引用。
        """

        self._snapshot = snapshot or TmuxSnapshot()

    def read(self) -> TmuxSnapshot:
        """返回固定 tmux 快照。

        入参：无。
        返回：构造时提供的 `TmuxSnapshot`。
        错误处理：不主动抛业务异常。
        副作用：无；只读取内存字段。
        """

        return self._snapshot


class SubprocessTmuxReader:
    """通过 tmux CLI 读取只读快照。

    入参：`timeout_seconds` 是每个 tmux 命令的最大等待时间。
    返回：提供 `read` 的生产 reader。
    错误处理：tmux 不存在、命令失败或超时时返回空快照。
    副作用：`read` 只读调用 `tmux list-panes` 和 `tmux list-clients`。
    """

    def __init__(self, timeout_seconds: float = 1.0) -> None:
        """创建 subprocess tmux reader。

        入参：`timeout_seconds` 必须是正数；调用方负责传入合理值。
        返回：无显式返回值。
        错误处理：构造时不主动校验，命令超时由 `read` 处理。
        副作用：无；不立即调用 tmux。
        """

        self._timeout_seconds = timeout_seconds

    def read(self) -> TmuxSnapshot:
        """读取当前 tmux panes 和 clients。

        入参：无。
        返回：解析成功的 `TmuxSnapshot`；tmux 不可用或失败时返回空快照。
        错误处理：吞掉 `OSError`、`subprocess.SubprocessError` 和 `ValueError` 并降级为空。
        副作用：只读调用 tmux CLI，不修改 session。
        """

        try:
            panes_output = _run_tmux(
                ["tmux", "list-panes", "-a", "-F", _PANE_FORMAT],
                timeout_seconds=self._timeout_seconds,
            )
            clients_output = _run_tmux(
                ["tmux", "list-clients", "-F", _CLIENT_FORMAT],
                timeout_seconds=self._timeout_seconds,
            )
            return TmuxSnapshot(
                panes=tuple(
                    _parse_pane_line(line)
                    for line in panes_output.splitlines()
                    if line
                ),
                clients=tuple(
                    _parse_client_line(line)
                    for line in clients_output.splitlines()
                    if line
                ),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return TmuxSnapshot()


def find_pane_for_tty(snapshot: TmuxSnapshot, tty: str | None) -> TmuxPane | None:
    """按 TTY 在 tmux snapshot 中查找 pane。

    入参：`snapshot` 是 tmux 快照；`tty` 是 Codex 进程的 TTY，可带或不带 `/dev/` 前缀。
    返回：匹配的 `TmuxPane`；未命中返回 None。
    错误处理：空 TTY 直接返回 None。
    副作用：无；只读取内存快照。
    """

    normalized_tty = _normalize_tty(tty)
    if normalized_tty is None:
        return None
    for pane in snapshot.panes:
        if _normalize_tty(pane.pane_tty) == normalized_tty:
            return pane
    return None


def clients_for_session(
    snapshot: TmuxSnapshot, session_name: str
) -> tuple[TmuxClient, ...]:
    """返回某个 tmux session 的 attached clients。

    入参：`snapshot` 是 tmux 快照；`session_name` 是目标 session 名称。
    返回：按原始快照顺序排列的 client 元组。
    错误处理：不主动抛业务异常。
    副作用：无；只读取内存快照。
    """

    return tuple(
        client for client in snapshot.clients if client.session_name == session_name
    )


def _run_tmux(command: list[str], *, timeout_seconds: float) -> str:
    """执行一个只读 tmux 命令并返回 stdout。

    入参：`command` 是 tmux argv；`timeout_seconds` 是最大等待时间。
    返回：标准输出文本。
    错误处理：非零退出、超时或执行失败由 subprocess/OSError 异常表示。
    副作用：只读调用 tmux CLI。
    """

    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return result.stdout


def _parse_pane_line(line: str) -> TmuxPane:
    """解析一行 `tmux list-panes` 输出。

    入参：`line` 是 tab 分隔的 pane 字段。
    返回：`TmuxPane`。
    错误处理：字段不足或整数字段非法时抛 ValueError。
    副作用：无；只解析内存字符串。
    """

    fields = line.split("\t")
    if len(fields) != 8:
        raise ValueError("unexpected tmux pane field count")
    pane_id, pane_tty, pane_pid, session_name, window_id, window_index, pane_index, current_path = fields
    return TmuxPane(
        pane_id=pane_id,
        pane_tty=_none_if_empty(pane_tty),
        pane_pid=_optional_int(pane_pid),
        session_name=session_name,
        window_id=_none_if_empty(window_id),
        window_index=_optional_int(window_index),
        pane_index=_optional_int(pane_index),
        current_path=_none_if_empty(current_path),
    )


def _parse_client_line(line: str) -> TmuxClient:
    """解析一行 `tmux list-clients` 输出。

    入参：`line` 是 tab 分隔的 client 字段。
    返回：`TmuxClient`。
    错误处理：字段不足或整数字段非法时抛 ValueError。
    副作用：无；只解析内存字符串。
    """

    fields = line.split("\t")
    if len(fields) != 4:
        raise ValueError("unexpected tmux client field count")
    client_tty, client_pid, session_name, client_activity = fields
    return TmuxClient(
        client_tty=_none_if_empty(client_tty),
        client_pid=_optional_int(client_pid),
        session_name=session_name,
        client_activity=_optional_int(client_activity),
    )


def _normalize_tty(value: str | None) -> str | None:
    """规范化 TTY 字符串，方便 `/dev/ttys006` 与 `ttys006` 匹配。

    入参：`value` 是可选 TTY。
    返回：去掉 `/dev/` 前缀的 TTY；空值返回 None。
    错误处理：不主动抛业务异常。
    副作用：无；只处理内存字符串。
    """

    if value is None or not value.strip():
        return None
    stripped = value.strip()
    if stripped.startswith("/dev/"):
        return stripped.removeprefix("/dev/")
    return stripped


def _none_if_empty(value: str) -> str | None:
    """把空字符串规范为 None。

    入参：`value` 是 tmux 输出字段。
    返回：非空字符串或 None。
    错误处理：不主动抛业务异常。
    副作用：无；只处理内存字符串。
    """

    return value if value else None


def _optional_int(value: str) -> int | None:
    """把 tmux 整数字段解析为 int。

    入参：`value` 是 tmux 输出字段。
    返回：整数或 None。
    错误处理：非空且非法的整数字段抛 ValueError。
    副作用：无；只处理内存字符串。
    """

    if not value:
        return None
    return int(value)
