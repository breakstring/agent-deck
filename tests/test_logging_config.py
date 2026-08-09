"""Agent Deck Uvicorn 日志配置与轮转文件 handler 的定向测试。

测试只在 pytest 临时目录创建小型日志文件，不启动 daemon、不访问 HTTP 或真实硬件，也不修改
进程全局 logging 配置；用于证明默认配置映射和标准按大小轮转行为。
"""

from __future__ import annotations

import logging

from agent_deck.config import LoggingConfig, LogLevel
from agent_deck.logging_config import (
    EnsuredRotatingFileHandler,
    build_uvicorn_log_config,
)


def test_build_uvicorn_log_config_maps_quiet_defaults(tmp_path) -> None:
    """Uvicorn 配置应使用 warning 过滤并复用单个轮转文件 handler。

    入参：pytest ``tmp_path`` 提供不会实际打开的日志路径。
    返回：无；断言通过代表 error/access/root 使用相同 handler 和轮转上限。
    错误处理：映射字段缺失或级别错误时由 pytest 报告。
    副作用：只组装字典，不创建日志目录或文件。
    """

    config = LoggingConfig(file_path=tmp_path / "nested" / "agent-deckd.log")

    result = build_uvicorn_log_config(config)

    handler = result["handlers"]["rotating_file"]
    assert handler["maxBytes"] == 5 * 1024 * 1024
    assert handler["backupCount"] == 2
    assert result["loggers"]["uvicorn.error"]["level"] == "WARNING"
    assert result["loggers"]["uvicorn.access"]["handlers"] == [
        "console",
        "rotating_file",
    ]
    assert not (tmp_path / "nested").exists()


def test_build_uvicorn_log_config_can_disable_file_output() -> None:
    """关闭文件日志时配置中不应残留文件 handler。

    入参：无；使用内存中的 ``LoggingConfig``。
    返回：无；断言通过代表 console-only 模式不创建文件输出合同。
    错误处理：若文件 handler 仍存在则由 pytest 报告。
    副作用：无；不创建文件、不修改全局 logger。
    """

    result = build_uvicorn_log_config(
        LoggingConfig(level=LogLevel.ERROR, file_enabled=False)
    )

    assert set(result["handlers"]) == {"console"}
    assert result["root"]["level"] == "ERROR"


def test_build_uvicorn_log_config_maps_trace_to_numeric_level() -> None:
    """Uvicorn 的 trace 字符串应映射为 Python logging 可识别的数值级别。

    入参：无；使用关闭文件输出的 trace 配置。
    返回：无；断言通过代表标准 ``dictConfig`` 不会因未知 ``TRACE`` 字符串失败。
    错误处理：级别不是 Uvicorn 约定的数值 5 时由 pytest 报告。
    副作用：只组装配置字典，不修改进程全局 logger。
    """

    result = build_uvicorn_log_config(
        LoggingConfig(level=LogLevel.TRACE, file_enabled=False)
    )

    assert result["handlers"]["console"]["level"] == 5
    assert result["loggers"]["uvicorn.error"]["level"] == 5


def test_ensured_rotating_file_handler_bounds_log_files(tmp_path) -> None:
    """轮转 handler 应创建父目录并把文件数量限制为当前文件加备份。

    入参：pytest ``tmp_path`` 提供隔离日志目录。
    返回：无；断言通过代表写入超过阈值后只保留一个当前文件和两个备份。
    错误处理：文件未创建、未轮转或数量超限时由 pytest 报告。
    副作用：在 pytest 临时目录写入少量测试日志并在结束前关闭 handler。
    """

    log_path = tmp_path / "nested" / "agent-deckd.log"
    handler = EnsuredRotatingFileHandler(
        str(log_path),
        maxBytes=128,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for index in range(20):
            record = logging.LogRecord(
                "agent-deck-test",
                logging.WARNING,
                __file__,
                1,
                f"warning-{index}-" + ("x" * 40),
                (),
                None,
            )
            handler.emit(record)
    finally:
        handler.close()

    files = sorted(log_path.parent.glob("agent-deckd.log*"))
    assert log_path.exists()
    assert [path.name for path in files] == [
        "agent-deckd.log",
        "agent-deckd.log.1",
        "agent-deckd.log.2",
    ]
