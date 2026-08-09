"""Agent Deck daemon 的 Uvicorn 日志配置与安全轮转 handler。

本模块把已校验的 ``LoggingConfig`` 转换为 Uvicorn ``dictConfig``，并在首次打开日志文件前
创建父目录。它只负责进程日志输出，不读取业务配置、不记录事件 payload，也不改变 HTTP 行为。
文件 handler 使用标准按大小轮转，避免个人设备上的常驻 daemon 日志无限增长。
"""

from __future__ import annotations

from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from agent_deck.config import LoggingConfig, LogLevel

_TRACE_LOG_LEVEL = 5


class EnsuredRotatingFileHandler(RotatingFileHandler):
    """创建父目录后打开按大小轮转的 UTF-8 日志文件。

    入参：参数语义与 ``logging.handlers.RotatingFileHandler`` 相同；``filename`` 支持 ``~``。
    返回：可由 ``logging.config.dictConfig`` 构造的 handler 实例。
    错误处理：目录或文件不可创建时保留原始 ``OSError``，阻止 daemon 静默丢失日志。
    副作用：创建缺失的父目录，并按标准 handler 语义打开、写入和轮转日志文件。
    """

    def __init__(
        self,
        filename: str,
        mode: str = "a",
        maxBytes: int = 0,
        backupCount: int = 0,
        encoding: str | None = None,
        delay: bool = False,
        errors: str | None = None,
    ) -> None:
        """初始化会自动创建父目录的轮转日志 handler。

        入参：与父类同名参数保持兼容，供标准 ``dictConfig`` 直接注入。
        返回：无显式返回值；构造成功后实例可接收 logging record。
        错误处理：路径创建或文件打开错误由父类/``Path.mkdir`` 原样抛出。
        副作用：展开用户目录、创建父目录，并可能立即打开日志文件。
        """

        resolved = Path(filename).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(
            resolved,
            mode=mode,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
            errors=errors,
        )


def build_uvicorn_log_config(config: LoggingConfig) -> dict[str, Any]:
    """生成同时覆盖 Uvicorn error/access 与其他 warning 的日志配置。

    入参：``config`` 是已经通过 Pydantic 校验的日志策略。
    返回：可直接传给 ``uvicorn.run(log_config=...)`` 的 ``dictConfig`` 字典；控制台始终保留，
    文件 handler 仅在 ``file_enabled`` 时加入，access logger 是否产生日志由 Uvicorn 的
    ``access_log`` 参数独立控制。
    错误处理：本函数只组装普通字典，不打开文件；handler 构造错误发生在 Uvicorn 应用配置时。
    副作用：无；不修改全局 logging 状态、不创建目录或文件。
    """

    level: str | int = (
        _TRACE_LOG_LEVEL
        if config.level == LogLevel.TRACE
        else config.level.value.upper()
    )
    handler_names = ["console"]
    handlers: dict[str, dict[str, Any]] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "level": level,
            "stream": "ext://sys.stderr",
        }
    }
    if config.file_enabled:
        handlers["rotating_file"] = {
            "()": "agent_deck.logging_config.EnsuredRotatingFileHandler",
            "filename": str(config.file_path),
            "maxBytes": config.max_bytes,
            "backupCount": config.backup_count,
            "encoding": "utf-8",
            "formatter": "default",
            "level": level,
        }
        handler_names.append("rotating_file")

    logger = {
        "handlers": handler_names,
        "level": level,
        "propagate": False,
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(asctime)s %(name)s: %(message)s",
                "use_colors": None,
            }
        },
        "handlers": handlers,
        "loggers": {
            "uvicorn": dict(logger),
            "uvicorn.error": dict(logger),
            "uvicorn.access": dict(logger),
        },
        "root": {
            "handlers": handler_names,
            "level": level,
        },
    }
