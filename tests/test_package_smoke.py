"""Smoke tests for the Agent Deck package skeleton.

These tests only verify that the Task 1 package metadata and bootstrap CLI
entry points are importable. They do not touch network listeners, hardware
devices, Codex hook payloads, user configuration, or persistent project state.
"""

from typer.testing import CliRunner

import agent_deck
from agent_deck.cli import codex_hook_app, ctl_app, daemon_app


def test_package_exports_version_and_cli_apps() -> None:
    """Verify Task 1 package metadata and bootstrap CLI imports.

    入参：无；测试直接导入包和三个 Typer app 对象。
    返回：无返回值；断言通过代表包版本和 console script 目标可导入。
    错误处理：导入失败或版本不匹配会由 pytest 以失败断言/异常报告。
    副作用：仅执行 Python 导入，不启动网络、不打开硬件、不读写用户配置。
    """

    assert agent_deck.__version__ == "0.1.0"
    assert daemon_app is not None
    assert ctl_app is not None
    assert codex_hook_app is not None


def test_ctl_version_command_prints_package_version() -> None:
    """Verify the bootstrap control CLI can print package version.

    入参：无；测试通过 Typer `CliRunner` 调用 `ctl_app version`。
    返回：无返回值；断言通过代表 CLI 命令退出码和输出符合包骨架预期。
    错误处理：命令异常、非零退出码或输出不匹配会由 pytest 报告失败。
    副作用：仅在隔离 runner 中写标准输出，不连接 daemon、不访问网络或硬件。
    """

    result = CliRunner().invoke(ctl_app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == "0.1.0"
