"""Bootstrap command-line entry points for Agent Deck.

This module only keeps the packaged console scripts importable during the MVP
foundation stage. It intentionally does not start network listeners, open
hardware devices, read or write user configuration, or install Codex hooks; the
real daemon, control, and hook behavior belongs to later implementation tasks.
"""

from __future__ import annotations

import typer

from agent_deck import __version__

#: Bootstrap Typer app for the future local daemon entry point. Importing or
#: asking this app for help has no network, filesystem, configuration, or
#: hardware side effects.
daemon_app = typer.Typer(
    help="Bootstrap Agent Deck daemon entry point.",
    no_args_is_help=True,
)

#: Bootstrap Typer app for future operator control commands. It currently
#: exposes only metadata commands and does not contact a daemon.
ctl_app = typer.Typer(
    help="Bootstrap Agent Deck control entry point.",
    no_args_is_help=True,
)

#: Bootstrap Typer app for the future Codex hook helper. It currently only
#: exposes help output and does not parse hook payloads or modify permissions.
codex_hook_app = typer.Typer(
    help="Bootstrap Agent Deck Codex hook entry point.",
    no_args_is_help=True,
)


@daemon_app.callback()
def daemon_callback() -> None:
    """Describe the daemon bootstrap command group.

    入参：无；当前 skeleton 不接收配置路径、端口或硬件参数。
    返回：无返回值，Typer 负责渲染 `--help` 输出。
    错误处理：本函数不主动抛出业务异常；命令行解析错误由 Typer 处理。
    副作用：无网络监听、无硬件访问、无文件或用户配置读写。
    """


@ctl_app.callback()
def ctl_callback() -> None:
    """Describe the control bootstrap command group.

    入参：无；当前 skeleton 不接收 daemon 地址或请求参数。
    返回：无返回值，Typer 负责渲染 `--help` 输出。
    错误处理：本函数不主动抛出业务异常；命令行解析错误由 Typer 处理。
    副作用：不连接本地 daemon，不读写文件，也不修改全局状态。
    """


@ctl_app.command()
def version() -> None:
    """Print the Agent Deck package version.

    入参：无；版本号来自 `agent_deck.__version__`，不读取环境或配置。
    返回：无返回值；版本文本通过标准输出交给 Typer/Click 处理。
    错误处理：本函数不主动抛出业务异常；标准输出失败等底层错误由运行时传播。
    副作用：仅向标准输出写入一行版本号，不访问网络、硬件或文件系统。
    """

    typer.echo(__version__)


@codex_hook_app.callback()
def codex_hook_callback() -> None:
    """Describe the Codex hook bootstrap command group.

    入参：无；当前 skeleton 不接收或解析 Codex hook JSON payload。
    返回：无返回值，Typer 负责渲染 `--help` 输出。
    错误处理：本函数不主动抛出业务异常；命令行解析错误由 Typer 处理。
    副作用：不读取 stdin，不写权限响应，不连接 daemon，也不修改配置。
    """
