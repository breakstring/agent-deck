"""macOS 进程表读取与 Codex CLI 父进程链推断。

本模块把真实 `ps` 输出转换为可测试的 `ProcessInfo` 记录，并提供纯函数用于从
Codex pid 上溯宿主进程。生产 reader 只做只读进程枚举；测试可使用 `StaticProcessTable`
避免访问真实系统进程。
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from agent_deck.hosts.models import (
    Confidence,
    PresentationClientContext,
    PresentationClientKind,
)

_MAX_PROCESS_CHAIN_DEPTH = 32


class ProcessInfo(BaseModel):
    """描述进程表中的一条只读进程记录。

    入参：`pid` 是进程 id；`ppid` 是父进程 id，可为空；`command` 是进程命令或可执行路径；
    `args` 是解析出的命令参数；`tty` 是 ps 暴露的 TTY；`cwd` 和 `started_at` 可为空。
    返回：不可变模型实例。
    错误处理：字段类型非法或 `started_at` 为 naive datetime 时由 Pydantic 抛出异常。
    副作用：只保存内存字段，不读取真实进程。
    """

    model_config = ConfigDict(frozen=True)

    pid: int
    ppid: int | None
    command: str
    args: tuple[str, ...] = ()
    tty: str | None = None
    cwd: str | None = None
    started_at: datetime | None = None

    @field_validator("started_at")
    @classmethod
    def _ensure_timezone_aware(cls, value: datetime | None) -> datetime | None:
        """拒绝 naive datetime，避免 PID 复用判断依赖隐式本地时区。

        入参：`value` 是可选进程启动时间。
        返回：原始 timezone-aware datetime 或 None。
        错误处理：datetime 缺少 tzinfo 或 utcoffset 时抛 ValueError。
        副作用：无；只检查内存字段。
        """

        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("process start time must be timezone-aware")
        return value


class ProcessTable(Protocol):
    """提供按 pid 读取进程记录的协议。

    入参：实现方可以来自静态测试 fixture 或真实系统进程表。
    返回：`get` 返回对应 `ProcessInfo`，缺失时返回 None。
    错误处理：实现方可在读取真实系统失败时抛运行时异常，resolver 应负责降级。
    副作用：协议本身无副作用；具体实现决定是否读取系统进程。
    """

    def get(self, pid: int) -> ProcessInfo | None:
        """按 pid 查询进程记录。

        入参：`pid` 是目标进程 id。
        返回：匹配的 `ProcessInfo`；不存在时返回 None。
        错误处理：具体实现可传播读取错误。
        副作用：静态实现无副作用，真实实现可能读取系统进程快照。
        """


class StaticProcessTable:
    """测试用内存进程表。

    入参：`processes` 是 pid 到 `ProcessInfo` 的 mapping，构造时复制。
    返回：可按 pid 查询的静态进程表。
    错误处理：构造时不主动校验链路完整性。
    副作用：只复制内存 mapping，不访问系统进程。
    """

    def __init__(self, processes: Mapping[int, ProcessInfo]) -> None:
        """创建静态进程表。

        入参：`processes` 是测试提供的进程记录 mapping。
        返回：无显式返回值。
        错误处理：mapping 迭代失败时按 Python 语义传播。
        副作用：只复制内存数据。
        """

        self._processes = dict(processes)

    def get(self, pid: int) -> ProcessInfo | None:
        """按 pid 返回静态进程记录。

        入参：`pid` 是目标进程 id。
        返回：匹配记录或 None。
        错误处理：不主动抛业务异常。
        副作用：无；只读取内存 dict。
        """

        return self._processes.get(pid)


class MacOSProcessTable:
    """从 macOS `ps` 输出构建的只读进程表。

    入参：构造时传入 `ProcessInfo` mapping；生产代码应通过 `read` 创建。
    返回：可按 pid 查询的进程表。
    错误处理：`read` 执行 ps 失败时会传播 `subprocess.SubprocessError` 或 `OSError`。
    副作用：构造无副作用；`read` 只读调用系统 `ps`。
    """

    def __init__(self, processes: Mapping[int, ProcessInfo]) -> None:
        """创建 macOS 进程表快照。

        入参：`processes` 是已经解析好的进程记录。
        返回：无显式返回值。
        错误处理：mapping 迭代失败时按 Python 语义传播。
        副作用：只复制内存数据。
        """

        self._processes = dict(processes)

    @classmethod
    def read(cls, *, timeout_seconds: float = 2.0) -> "MacOSProcessTable":
        """调用 `ps` 读取当前 macOS 进程表快照。

        入参：`timeout_seconds` 是 `ps` 命令最大等待时间。
        返回：`MacOSProcessTable` 快照。
        错误处理：`ps` 不存在、超时或返回非零时抛出对应 subprocess/OSError 异常。
        副作用：只读执行 `/bin/ps`，不修改进程或文件。
        """

        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,tty=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        processes: dict[int, ProcessInfo] = {}
        for line in result.stdout.splitlines():
            info = _parse_ps_line(line)
            if info is not None:
                processes[info.pid] = info
        return cls(processes)

    def get(self, pid: int) -> ProcessInfo | None:
        """按 pid 返回进程记录。

        入参：`pid` 是目标进程 id。
        返回：匹配记录或 None。
        错误处理：不主动抛业务异常。
        副作用：无；只读取内存快照。
        """

        return self._processes.get(pid)


def process_chain(
    table: ProcessTable, pid: int, *, max_depth: int = _MAX_PROCESS_CHAIN_DEPTH
) -> tuple[ProcessInfo, ...]:
    """从目标 pid 开始向父进程上溯。

    入参：`table` 是进程表；`pid` 是起点；`max_depth` 防止异常链路无限循环。
    返回：从子进程到祖先进程的 `ProcessInfo` 元组。
    错误处理：缺失 pid、父进程缺失、遇到环或达到深度限制时停止并返回已收集链路。
    副作用：只读取传入进程表。
    """

    chain: list[ProcessInfo] = []
    seen: set[int] = set()
    current_pid: int | None = pid
    while current_pid is not None and len(chain) < max_depth:
        if current_pid in seen:
            break
        seen.add(current_pid)
        info = table.get(current_pid)
        if info is None:
            break
        chain.append(info)
        if info.ppid in (None, 0, current_pid):
            break
        current_pid = info.ppid
    return tuple(chain)


def infer_direct_pty_host(
    chain: tuple[ProcessInfo, ...],
) -> PresentationClientContext | None:
    """从进程链中推断 direct PTY 的终端 App 展示客户端。

    入参：`chain` 是从 Codex 进程向上排列的父进程链。
    返回：命中 `.app/Contents/MacOS/` 可执行路径时返回终端 App client；未命中返回 None。
    错误处理：不主动抛业务异常；无法解析 app 名称时跳过该进程。
    副作用：只处理内存字符串。
    """

    for process in chain:
        app_name = _app_name_from_command(process.command)
        if app_name is None:
            app_name = _app_name_from_args(process.args)
        if app_name is None:
            continue
        return PresentationClientContext(
            kind=PresentationClientKind.TERMINAL_APP,
            app_name=app_name,
            app_path=_app_path_from_command(process.command),
            app_pid=process.pid,
            client_tty=process.tty,
            confidence=Confidence.MEDIUM,
        )
    return None


def _parse_ps_line(line: str) -> ProcessInfo | None:
    """解析 `ps -axo pid=,ppid=,tty=,command=` 的单行输出。

    入参：`line` 是 ps 的一行文本。
    返回：解析成功的 `ProcessInfo`；空行或字段不足时返回 None。
    错误处理：pid/ppid 不是整数时返回 None。
    副作用：无；只解析内存字符串。
    """

    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(None, 3)
    if len(parts) < 4:
        return None
    pid_raw, ppid_raw, tty_raw, command = parts
    try:
        pid = int(pid_raw)
        ppid = int(ppid_raw)
    except ValueError:
        return None
    try:
        args = tuple(shlex.split(command))
    except ValueError:
        args = (command,)
    return ProcessInfo(
        pid=pid,
        ppid=ppid,
        command=args[0] if args else command,
        args=args,
        tty=None if tty_raw == "??" else tty_raw,
    )


def _app_name_from_args(args: tuple[str, ...]) -> str | None:
    """从进程参数中解析 macOS App 名称。

    入参：`args` 是进程参数元组。
    返回：第一个 `.app/Contents/MacOS/` 路径中的 app 名称；未命中返回 None。
    错误处理：路径解析失败时返回 None。
    副作用：无；只处理内存字符串。
    """

    for arg in args:
        app_name = _app_name_from_command(arg)
        if app_name is not None:
            return app_name
    return None


def _app_name_from_command(command: str) -> str | None:
    """从可执行路径中解析 `.app` 名称。

    入参：`command` 是进程命令或路径。
    返回：去掉 `.app` 后缀的 App 名称；未命中返回 None。
    错误处理：路径缺少 app segment 时返回 None。
    副作用：无；只处理内存字符串。
    """

    app_path = _app_path_from_command(command)
    if app_path is None:
        return None
    name = Path(app_path).name
    if not name.endswith(".app"):
        return None
    return name[:-4]


def _app_path_from_command(command: str) -> str | None:
    """从命令路径中截取 macOS `.app` bundle 路径。

    入参：`command` 是进程命令或路径。
    返回：`.app` bundle 路径；未命中返回 None。
    错误处理：不主动抛业务异常。
    副作用：无；只处理内存字符串。
    """

    marker = ".app/Contents/MacOS/"
    index = command.find(marker)
    if index == -1:
        return None
    return command[: index + len(".app")]
