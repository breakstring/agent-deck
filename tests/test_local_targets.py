"""本机 URL 快捷动作测试。

这些测试只使用 fake subprocess runner；不打开浏览器、不启动 Finder、不访问真实硬件，
也不执行任意 shell。
"""

from __future__ import annotations

import subprocess

from agent_deck.actions.local_targets import open_local_url


def test_open_local_url_uses_structured_open_command() -> None:
    """URL action 应通过结构化参数调用 macOS open。

    入参：无；测试内注入 fake subprocess runner。
    返回：无返回值；断言通过代表不会拼接 shell 字符串。
    错误处理：命令参数或结果诊断错误时由 pytest 报告。
    副作用：只写测试内存列表。
    """

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """记录命令参数并返回成功。

        入参：`args` 是 executor 生成的命令参数。
        返回：成功的 `CompletedProcess`。
        错误处理：无。
        副作用：写入 `calls`。
        """

        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    result = open_local_url(url="https://example.com/path?q=1", runner=fake_runner)

    assert calls == [["open", "https://example.com/path?q=1"]]
    assert result.ok is True
    assert result.status == "succeeded"
    assert result.url == "https://example.com/path?q=1"


def test_open_local_url_rejects_unsupported_scheme() -> None:
    """URL action 只允许 http/https，拒绝高风险或未知 scheme。

    入参：无。
    返回：无返回值；断言通过代表 `javascript:` 等 URL 不会交给系统 open。
    错误处理：非法 URL 被执行时由 pytest 报告。
    副作用：无。
    """

    result = open_local_url(url="javascript:alert(1)", runner=_runner_should_not_call)

    assert result.ok is False
    assert result.status == "unsupported"
    assert "unsupported url scheme" in result.message


def _runner_should_not_call(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    """防止 fail-closed 路径误调用 subprocess。

    入参：任意参数。
    返回：不会返回；若被调用直接断言失败。
    错误处理：抛出 AssertionError。
    副作用：无。
    """

    raise AssertionError("runner should not be called")
