"""Codex 本机安装检测、集成引导与保守安装模型。

本模块负责 Codex 安装路径、用户配置路径、Agent Deck 集成片段、merge dry-run 和
显式 apply 安装。默认检测与 dry-run 不启动 Codex、不连接 app-server、不读取 prompt
或 rollout 内容、不写入 `~/.codex`，也不连接 Agent Deck daemon。只有调用方显式使用
`install_codex_integration(apply=True)` 时才会写用户级 Codex 配置，并且会先拒绝需要
人工合并的冲突、再备份已有文件。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

DEFAULT_CODEX_APP_PATH: Final[Path] = Path("/Applications/Codex.app")
DEFAULT_AGENT_DECK_DAEMON_URL: Final[str] = "http://127.0.0.1:8765"
DEFAULT_PERMISSION_TIMEOUT_SECONDS: Final[int] = 25
DEFAULT_CODEX_SYSTEM_REQUIREMENTS_PATH: Final[Path] = Path(
    "/etc/codex/requirements.toml"
)
DEFAULT_CODEX_MANAGED_HOOKS_DIR: Final[Path] = Path(
    "/usr/local/lib/agent-deck/codex-hooks"
)
_LIFECYCLE_HOOK_EVENTS: Final[tuple[str, ...]] = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
)
_AGENT_DECK_NOTIFY_DIRNAME: Final[str] = "agent-deck"
_AGENT_DECK_NOTIFY_FANOUT_NAME: Final[str] = "notify-fanout.py"
_AGENT_DECK_HOOK_MARKER: Final[str] = "_agent_deck"
_AGENT_DECK_MANAGED_BLOCK_BEGIN: Final[str] = "# BEGIN_AGENT_DECK_MANAGED_HOOKS"
_AGENT_DECK_MANAGED_BLOCK_END: Final[str] = "# END_AGENT_DECK_MANAGED_HOOKS"


class CodexInstallSurface(BaseModel):
    """描述一个 Codex 安装面的只读检测结果。

    入参：`detected` 表示该安装面是否被当前探针发现；`path` 是发现或检查的路径；
    `source` 描述路径来源，例如 PATH 或 macOS app bundle。
    返回：不可变 Pydantic model，可直接序列化给 CLI/API。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型只保存检测函数传入的内存数据。
    """

    model_config = ConfigDict(frozen=True)

    detected: bool
    path: str | None
    source: str


class CodexInstallationReport(BaseModel):
    """汇总 Codex CLI 与 Codex App 的安装检测结果。

    入参：`cli` 是 shell PATH 上的 Codex CLI 检测；`app` 是 macOS Codex.app 检测。
    返回：不可变 Pydantic model，供 `CodexDetectionReport` 嵌套使用。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；只保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    cli: CodexInstallSurface
    app: CodexInstallSurface


class CodexConfigurationReport(BaseModel):
    """描述 Codex 用户级配置文件位置与存在性。

    入参：`codex_home` 是解析后的 CODEX_HOME；`user_config_path`/`user_hooks_path`
    是官方用户级配置和 hooks 文件位置；`*_exists` 表示当前只读存在性检查结果。
    返回：不可变 Pydantic model，可用于集成向导提示用户编辑哪个文件。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不读写文件。
    """

    model_config = ConfigDict(frozen=True)

    codex_home: str
    user_config_path: str
    user_config_exists: bool
    user_hooks_path: str
    user_hooks_exists: bool
    system_requirements_path: str
    system_requirements_exists: bool
    managed_hooks_dir: str
    managed_hooks_dir_exists: bool


class CodexIntegrationGuide(BaseModel):
    """给出 Agent Deck 接入 Codex hooks/notify 的手动引导。

    入参：`writes_files` 明确表示本向导是否会自动写文件；`daemon_command` 是建议先启动的
    daemon 命令；`notify_toml` 是用户级 `config.toml` 片段；`hooks_json` 是可合并进
    `hooks.json` 的 lifecycle hook 片段；`merge_dry_run` 是对现有用户配置的只读合并建议；
    `trust_steps` 是 Codex 内部 review/trust 步骤；`verification_commands` 是接入后可执行的
    smoke 命令；`warnings` 是安全边界提示。
    返回：不可变 Pydantic model，可直接输出 JSON。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；只保存生成出的配置片段，不创建文件。
    """

    model_config = ConfigDict(frozen=True)

    mode: str
    writes_files: bool
    daemon_command: str
    notify_toml: str
    hooks_json: dict[str, Any]
    managed_requirements_toml: str | None = None
    managed_wrapper_path: str | None = None
    merge_dry_run: "CodexIntegrationMergeDryRun"
    trust_steps: tuple[str, ...]
    verification_commands: tuple[str, ...]
    warnings: tuple[str, ...]


class CodexIntegrationMergeTarget(BaseModel):
    """描述单个 Codex 配置文件的 dry-run 合并动作。

    入参：`path` 是目标文件路径；`exists` 表示文件当前是否存在；`action` 是稳定动作代码；
    `reason` 是不包含原始配置内容的说明。
    返回：不可变 Pydantic model，供 CLI JSON 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；只保存检测结果。
    """

    model_config = ConfigDict(frozen=True)

    path: str
    exists: bool
    action: str
    reason: str


class CodexIntegrationMergeDryRun(BaseModel):
    """汇总 Codex integration 的只读合并建议。

    入参：`writes_files` 固定为 False；`notify` 描述用户级 `config.toml` 动作；`hooks`
    描述用户级 `hooks.json` 动作；`recommended_edits` 是可执行的人工编辑摘要；`warnings`
    是解析失败或人工确认提示。
    返回：不可变 Pydantic model，可嵌入 `CodexIntegrationGuide`。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不读取或写入文件。
    """

    model_config = ConfigDict(frozen=True)

    writes_files: bool
    notify: CodexIntegrationMergeTarget
    hooks: CodexIntegrationMergeTarget
    recommended_edits: tuple[str, ...]
    warnings: tuple[str, ...]


class CodexDetectionReport(BaseModel):
    """Codex 本机检测与可选集成引导的总报告。

    入参：`product` 固定为 `codex`；`installation` 汇总安装面；`configuration`
    描述用户级配置文件；`integration` 在用户请求 `--enable-integration` 时包含手动引导。
    返回：不可变 Pydantic model，可由 CLI 以 JSON 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不访问外部资源。
    """

    model_config = ConfigDict(frozen=True)

    product: str
    installation: CodexInstallationReport
    configuration: CodexConfigurationReport
    integration: CodexIntegrationGuide | None = None


class CodexIntegrationInstallResult(BaseModel):
    """Codex integration 安装命令的执行结果。

    入参：`applied` 表示是否执行写入；`writes_files` 表示本次调用是否允许并实际写文件；
    `detection_report` 是同一次检测/引导报告；`backup_paths` 是写入前创建的备份文件；
    `written_paths` 是本次写入或创建的目标文件；`skipped_paths` 是已配置所以跳过的文件。
    返回：不可变 Pydantic model，可由 CLI 以 JSON 输出。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不访问文件。
    """

    model_config = ConfigDict(frozen=True)

    mode: str
    applied: bool
    writes_files: bool
    detection_report: CodexDetectionReport
    backup_paths: tuple[str, ...]
    written_paths: tuple[str, ...]
    skipped_paths: tuple[str, ...]


class CodexManagedSystemValidationCheck(BaseModel):
    """描述 managed-system 安装前只读验证的单项结果。

    入参：`name` 是稳定检查项名称；`ok` 表示该项是否通过；`severity` 是展示和聚合用级别；
    `message` 是不包含敏感配置原文的中文说明；`path` 是该检查关联的可选文件路径。
    返回：不可变 Pydantic model，可直接序列化给 CLI。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型只保存验证函数传入的数据。
    """

    model_config = ConfigDict(frozen=True)

    name: str
    ok: bool
    severity: str
    message: str
    path: str | None = None


class CodexManagedSystemValidationResult(BaseModel):
    """汇总 Codex managed-system 安装前的只读验证结果。

    入参：`mode` 固定为 `managed-system`；`ok` 表示是否没有阻断级错误；`checks` 是全部
    检查项；`detection_report` 是同次生成的 managed-system 安装计划。
    返回：不可变 Pydantic model，可由 CLI 输出 JSON。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：无；模型自身不访问文件系统。
    """

    model_config = ConfigDict(frozen=True)

    mode: str
    ok: bool
    checks: tuple[CodexManagedSystemValidationCheck, ...]
    detection_report: CodexDetectionReport


def validate_codex_managed_system_integration(
    *,
    daemon_url: str = DEFAULT_AGENT_DECK_DAEMON_URL,
    codex_home: Path | None = None,
    app_path: Path | None = None,
    system_requirements_path: Path | None = None,
    managed_hooks_dir: Path | None = None,
) -> CodexManagedSystemValidationResult:
    """只读验证 Codex managed-system 集成是否可安全安装或继续使用。

    入参：`daemon_url` 用于生成待验证的 hook 命令；`codex_home`、`app_path`、
    `system_requirements_path`、`managed_hooks_dir` 允许测试或自定义路径覆盖。
    返回：`CodexManagedSystemValidationResult`，其中 `ok=False` 表示存在需要人工处理的
    阻断项。
    错误处理：本函数尽量把可预期的配置问题收敛为 check；无法构建基础检测报告的异常向上传播。
    副作用：只读检查路径存在性、读取现有 requirements/wrapper/hooks 文件；不创建、不修改文件。
    """

    report = build_codex_detection_report(
        enable_integration=True,
        daemon_url=daemon_url,
        codex_home=codex_home,
        app_path=app_path,
        integration_mode="managed-system",
        system_requirements_path=system_requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )
    guide = report.integration
    if guide is None:
        raise ValueError("managed-system integration guide missing")

    checks: list[CodexManagedSystemValidationCheck] = []
    _validate_generated_managed_requirements(guide=guide, checks=checks)
    _validate_current_system_requirements(guide=guide, checks=checks)
    _validate_current_managed_wrapper(guide=guide, checks=checks)
    _validate_user_level_hook_overlap(report=report, checks=checks)
    ok = not any(check.severity == "error" for check in checks)
    return CodexManagedSystemValidationResult(
        mode="managed-system",
        ok=ok,
        checks=tuple(checks),
        detection_report=report,
    )


def _validation_check(
    *,
    name: str,
    ok: bool,
    severity: str,
    message: str,
    path: str | None = None,
) -> CodexManagedSystemValidationCheck:
    """构造单个 managed-system validate check。

    入参：`name` 是稳定检查项名称；`ok` 和 `severity` 描述检查结果；`message`
    是面向操作者的说明；`path` 是相关文件路径。
    返回：`CodexManagedSystemValidationCheck`。
    错误处理：非法字段由 Pydantic 报告。
    副作用：无；只构造内存模型。
    """

    return CodexManagedSystemValidationCheck(
        name=name,
        ok=ok,
        severity=severity,
        message=message,
        path=path,
    )


def _validate_generated_managed_requirements(
    *,
    guide: CodexIntegrationGuide,
    checks: list[CodexManagedSystemValidationCheck],
) -> None:
    """验证当前版本生成的 managed requirements 片段自身可被 Codex 解析。

    入参：`guide` 是 managed-system guide；`checks` 是调用方维护的检查结果列表。
    返回：无返回值；检查结果追加到 `checks`。
    错误处理：缺少或解析失败时追加 error，不向外抛出。
    副作用：无；只解析内存 TOML 字符串。
    """

    if guide.managed_requirements_toml is None:
        checks.append(
            _validation_check(
                name="generated_requirements",
                ok=False,
                severity="error",
                message="当前版本未生成 managed requirements TOML。",
            )
        )
        return
    try:
        parsed = tomllib.loads(guide.managed_requirements_toml)
    except tomllib.TOMLDecodeError as exc:
        checks.append(
            _validation_check(
                name="generated_requirements",
                ok=False,
                severity="error",
                message=f"生成的 managed requirements TOML 不可解析：{exc}",
            )
        )
        return
    hooks = parsed.get("hooks")
    managed_dir = hooks.get("managed_dir") if isinstance(hooks, dict) else None
    if not isinstance(managed_dir, str) or not managed_dir:
        checks.append(
            _validation_check(
                name="generated_requirements",
                ok=False,
                severity="error",
                message="生成的 managed requirements 缺少 [hooks].managed_dir。",
            )
        )
        return
    if not Path(managed_dir).expanduser().is_absolute():
        checks.append(
            _validation_check(
                name="generated_requirements",
                ok=False,
                severity="error",
                message="[hooks].managed_dir 必须是绝对路径。",
                path=managed_dir,
            )
        )
        return
    checks.append(
        _validation_check(
            name="generated_requirements",
            ok=True,
            severity="ok",
            message="生成的 managed requirements TOML 可解析，并包含绝对 [hooks].managed_dir。",
            path=managed_dir,
        )
    )


def _validate_current_system_requirements(
    *,
    guide: CodexIntegrationGuide,
    checks: list[CodexManagedSystemValidationCheck],
) -> None:
    """验证当前系统 requirements 文件是否能安全合并 Agent Deck managed block。

    入参：`guide` 是 managed-system guide；`checks` 是调用方维护的检查结果列表。
    返回：无返回值；检查结果追加到 `checks`。
    错误处理：merge dry-run 中的 manual action 被转成 error；不读取原文到输出。
    副作用：只读取 dry-run 已检查过的文件状态，不写文件。
    """

    target = guide.merge_dry_run.hooks
    if target.action == "manual_merge_required":
        checks.append(
            _validation_check(
                name="system_requirements",
                ok=False,
                severity="error",
                message=f"{target.reason} 需要人工合并后再 apply。",
                path=target.path,
            )
        )
        return
    if target.action == "already_configured":
        checks.append(
            _validation_check(
                name="system_requirements",
                ok=True,
                severity="ok",
                message="系统 requirements 已包含当前 Agent Deck managed hooks。",
                path=target.path,
            )
        )
        return
    if target.action == "create_system_requirements":
        checks.append(
            _validation_check(
                name="system_requirements",
                ok=True,
                severity="info",
                message="系统 requirements 尚不存在；当前未启用 managed hooks，apply 时会创建。",
                path=target.path,
            )
        )
        return
    if target.action == "append_managed_hooks":
        checks.append(
            _validation_check(
                name="system_requirements",
                ok=True,
                severity="ok",
                message="系统 requirements 可安全追加 Agent Deck managed hooks block。",
                path=target.path,
            )
        )
        return
    if target.action == "refresh_managed_hooks":
        checks.append(
            _validation_check(
                name="system_requirements",
                ok=True,
                severity="ok",
                message="系统 requirements 已有旧 Agent Deck block，可由 apply 刷新。",
                path=target.path,
            )
        )
        return
    checks.append(
        _validation_check(
            name="system_requirements",
            ok=False,
            severity="error",
            message=f"未知 managed-system 合并动作：{target.action}",
            path=target.path,
        )
    )


def _validate_current_managed_wrapper(
    *,
    guide: CodexIntegrationGuide,
    checks: list[CodexManagedSystemValidationCheck],
) -> None:
    """验证当前 managed wrapper 文件是否存在、可执行并避免依赖裸 PATH。

    入参：`guide` 是 managed-system guide；`checks` 是调用方维护的检查结果列表。
    返回：无返回值；检查结果追加到 `checks`。
    错误处理：读取 wrapper 失败时追加 error；requirements 未安装时 wrapper 缺失只是 info。
    副作用：只读检查 wrapper 路径和文件内容。
    """

    if guide.managed_wrapper_path is None:
        checks.append(
            _validation_check(
                name="managed_wrapper",
                ok=False,
                severity="error",
                message="当前版本未生成 managed wrapper 路径。",
            )
        )
        return
    wrapper_path = Path(guide.managed_wrapper_path)
    requirements_exists = guide.merge_dry_run.hooks.exists
    if not wrapper_path.exists():
        checks.append(
            _validation_check(
                name="managed_wrapper",
                ok=not requirements_exists,
                severity="warning" if requirements_exists else "info",
                message=(
                    "managed wrapper 不存在；系统 requirements 已存在时需要先重新 apply。"
                    if requirements_exists
                    else "managed wrapper 尚不存在；apply 时会创建。"
                ),
                path=str(wrapper_path),
            )
        )
        return
    try:
        text = wrapper_path.read_text(encoding="utf-8")
    except OSError as exc:
        checks.append(
            _validation_check(
                name="managed_wrapper",
                ok=False,
                severity="error",
                message=f"managed wrapper 无法读取：{exc}",
                path=str(wrapper_path),
            )
        )
        return
    executable = bool(wrapper_path.stat().st_mode & 0o111)
    uses_bare_uv = "exec uv " in text
    if not executable:
        checks.append(
            _validation_check(
                name="managed_wrapper",
                ok=False,
                severity="error",
                message="managed wrapper 存在但不可执行；Codex 无法运行该 hook。",
                path=str(wrapper_path),
            )
        )
        return
    if uses_bare_uv:
        checks.append(
            _validation_check(
                name="managed_wrapper",
                ok=not requirements_exists,
                severity="error" if requirements_exists else "warning",
                message=(
                    "managed wrapper 仍使用裸 uv，Codex App 启动环境可能找不到命令；请重新 apply。"
                    if requirements_exists
                    else "残留 managed wrapper 仍使用裸 uv；当前系统 requirements 不存在，因此暂不生效。"
                ),
                path=str(wrapper_path),
            )
        )
        return
    checks.append(
        _validation_check(
            name="managed_wrapper",
            ok=True,
            severity="ok",
            message="managed wrapper 存在、可执行，且未使用裸 uv 启动。",
            path=str(wrapper_path),
        )
    )


def _validate_user_level_hook_overlap(
    *,
    report: CodexDetectionReport,
    checks: list[CodexManagedSystemValidationCheck],
) -> None:
    """检查用户级 hooks.json 是否仍有 Agent Deck hooks 造成重复上报风险。

    入参：`report` 是 managed-system 检测报告；`checks` 是调用方维护的检查结果列表。
    返回：无返回值；检查结果追加到 `checks`。
    错误处理：无法读取或解析用户 hooks 时追加 warning，避免 validate 命令中断。
    副作用：只读读取用户级 hooks.json；不修改配置。
    """

    path = Path(report.configuration.user_hooks_path)
    if not path.exists():
        checks.append(
            _validation_check(
                name="user_hooks_overlap",
                ok=True,
                severity="info",
                message="用户级 hooks.json 不存在，不会与 managed hooks 重复。",
                path=str(path),
            )
        )
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checks.append(
            _validation_check(
                name="user_hooks_overlap",
                ok=False,
                severity="warning",
                message=f"用户级 hooks.json 无法解析，无法确认是否重复：{exc}",
                path=str(path),
            )
        )
        return
    if _contains_agent_deck_hook_command(data):
        checks.append(
            _validation_check(
                name="user_hooks_overlap",
                ok=False,
                severity="warning",
                message="用户级 hooks.json 仍包含 Agent Deck hooks；managed-system apply 会清理以避免重复。",
                path=str(path),
            )
        )
        return
    checks.append(
        _validation_check(
            name="user_hooks_overlap",
            ok=True,
            severity="ok",
            message="用户级 hooks.json 未包含 Agent Deck hooks。",
            path=str(path),
        )
    )


def install_codex_integration(
    *,
    apply: bool = False,
    daemon_url: str = DEFAULT_AGENT_DECK_DAEMON_URL,
    codex_home: Path | None = None,
    app_path: Path | None = None,
    mode: str = "user",
    system_requirements_path: Path | None = None,
    managed_hooks_dir: Path | None = None,
) -> CodexIntegrationInstallResult:
    """执行 Codex integration 的 dry-run 或保守安装。

    入参：`apply` 为 False 时只输出计划；为 True 时按计划写入配置；`daemon_url`
    用于生成 hooks/notify helper 命令；`codex_home` 和 `app_path` 允许测试或特殊环境覆盖路径；
    `mode` 支持 `user` 与 `managed-system`；`system_requirements_path` 和
    `managed_hooks_dir` 允许测试或管理员安装覆盖系统路径。
    返回：`CodexIntegrationInstallResult`。
    错误处理：若 dry-run 显示需要人工合并，则 apply 模式抛 ValueError 并不写文件；
    文件系统错误按 OSError 向上传播。
    副作用：dry-run 不写文件；user apply 会创建 `CODEX_HOME`、写
    `config.toml`/`hooks.json`；managed-system apply 会写系统 requirements 和稳定 wrapper，
    并清理用户级 Agent Deck hooks 以避免重复事件。修改已有文件前创建同目录备份。
    """

    if mode not in {"user", "managed-system"}:
        raise ValueError("mode must be 'user' or 'managed-system'")
    report = build_codex_detection_report(
        enable_integration=True,
        daemon_url=daemon_url,
        codex_home=codex_home,
        app_path=app_path,
        integration_mode=mode,
        system_requirements_path=system_requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )
    guide = report.integration
    if guide is None:
        raise ValueError("integration guide missing")
    if not apply:
        return CodexIntegrationInstallResult(
            applied=False,
            writes_files=False,
            mode=mode,
            detection_report=report,
            backup_paths=(),
            written_paths=(),
            skipped_paths=(),
        )
    merge = guide.merge_dry_run
    manual_actions = [
        target
        for target in (merge.notify, merge.hooks)
        if target.action == "manual_merge_required"
    ]
    if manual_actions:
        targets = ", ".join(target.path for target in manual_actions)
        raise ValueError(f"manual merge required before apply: {targets}")
    if mode == "managed-system":
        return _apply_managed_system_integration(
            report=report,
            guide=guide,
            daemon_url=daemon_url,
        )
    codex_home_path = Path(report.configuration.codex_home)
    codex_home_path.mkdir(parents=True, exist_ok=True)
    backup_paths: list[str] = []
    written_paths: list[str] = []
    skipped_paths: list[str] = []
    _apply_notify_target(
        target=merge.notify,
        notify_toml=guide.notify_toml,
        agent_deck_notify_command=_agent_deck_notify_command(daemon_url),
        backup_paths=backup_paths,
        written_paths=written_paths,
        skipped_paths=skipped_paths,
    )
    _apply_hooks_target(
        target=merge.hooks,
        hooks_json=guide.hooks_json,
        backup_paths=backup_paths,
        written_paths=written_paths,
        skipped_paths=skipped_paths,
    )
    return CodexIntegrationInstallResult(
        applied=True,
        writes_files=True,
        mode=mode,
        detection_report=report,
        backup_paths=tuple(backup_paths),
        written_paths=tuple(written_paths),
        skipped_paths=tuple(skipped_paths),
    )


def build_codex_detection_report(
    *,
    enable_integration: bool = False,
    daemon_url: str = DEFAULT_AGENT_DECK_DAEMON_URL,
    codex_home: Path | None = None,
    cli_path: str | None = None,
    app_path: Path | None = None,
    integration_mode: str = "user",
    system_requirements_path: Path | None = None,
    managed_hooks_dir: Path | None = None,
) -> CodexDetectionReport:
    """构建 Codex 本机只读检测报告和可选集成引导。

    入参：`enable_integration` 控制是否生成 hooks/notify 手动接入说明；`daemon_url`
    是生成 helper 命令时使用的 Agent Deck daemon 地址；`codex_home` 可覆盖默认
    `CODEX_HOME`；`cli_path` 可注入已发现 CLI 路径，未提供时从 PATH 查找；`app_path`
    可覆盖默认 macOS App bundle 路径；`integration_mode` 选择 user 或 managed-system
    引导；`system_requirements_path` 和 `managed_hooks_dir` 覆盖系统集成路径。
    返回：`CodexDetectionReport`。
    错误处理：路径对象非法时由 `Path` 或 Pydantic 报告；本函数不因 Codex 未安装而抛错。
    副作用：读取 `CODEX_HOME` 环境变量、查询 PATH，并检查 app/config/hooks 路径是否存在。
    """

    resolved_codex_home = _resolve_codex_home(codex_home)
    configuration = _build_configuration_report(
        resolved_codex_home,
        system_requirements_path=system_requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )
    resolved_cli_path = cli_path if cli_path is not None else shutil.which("codex")
    resolved_app_path = (app_path or DEFAULT_CODEX_APP_PATH).expanduser()
    integration = (
        build_codex_integration_guide(
            daemon_url=daemon_url,
            permission_timeout_seconds=DEFAULT_PERMISSION_TIMEOUT_SECONDS,
            configuration=configuration,
            mode=integration_mode,
        )
        if enable_integration
        else None
    )
    return CodexDetectionReport(
        product="codex",
        installation=CodexInstallationReport(
            cli=CodexInstallSurface(
                detected=bool(resolved_cli_path),
                path=resolved_cli_path,
                source="PATH",
            ),
            app=CodexInstallSurface(
                detected=resolved_app_path.exists(),
                path=str(resolved_app_path),
                source="macOS application bundle",
            ),
        ),
        configuration=configuration,
        integration=integration,
    )


def _apply_notify_target(
    *,
    target: CodexIntegrationMergeTarget,
    notify_toml: str,
    agent_deck_notify_command: list[str],
    backup_paths: list[str],
    written_paths: list[str],
    skipped_paths: list[str],
) -> None:
    """按 dry-run 动作写入用户级 `config.toml` 的 notify 配置。

    入参：`target` 是 dry-run 对 `config.toml` 的判断；`notify_toml` 是要写入的片段；
    `agent_deck_notify_command` 是 fan-out wrapper 调用 Agent Deck notify helper 的命令；
    `backup_paths`/`written_paths`/`skipped_paths` 是调用方收集结果的列表。
    返回：无返回值。
    错误处理：不支持的 action 抛 ValueError；文件读写错误按 OSError 传播。
    副作用：可能创建或追加写入 `config.toml`，修改已有文件前创建备份。
    """

    path = Path(target.path)
    if target.action == "already_configured":
        skipped_paths.append(str(path))
        return
    if target.action == "create_config":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{notify_toml}\n", encoding="utf-8")
        written_paths.append(str(path))
        return
    if target.action == "append_notify":
        backup_paths.append(str(_backup_file(path)))
        existing = path.read_text(encoding="utf-8")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(f"{existing}{separator}{notify_toml}\n", encoding="utf-8")
        written_paths.append(str(path))
        return
    if target.action == "install_notify_fanout":
        existing_notify = _read_existing_notify_command(path)
        if existing_notify is None:
            raise ValueError("existing notify cannot be converted to fan-out")
        fanout_path = _write_notify_fanout_script(
            codex_home=path.parent,
            commands=_merge_notify_commands(
                [existing_notify],
                agent_deck_notify_command,
            ),
        )
        backup_paths.append(str(_backup_file(path)))
        _replace_notify_line(
            path=path,
            notify_toml=_toml_array("notify", ["python3", str(fanout_path)]),
        )
        written_paths.extend([str(fanout_path), str(path)])
        return
    if target.action == "refresh_notify_fanout":
        fanout_path = _find_notify_fanout_path(
            _read_existing_notify_command(path),
            codex_home=path.parent,
        )
        if fanout_path is None:
            raise ValueError("existing Agent Deck fan-out path cannot be resolved")
        preserved_commands = _read_notify_fanout_commands(fanout_path)
        fanout_path = _write_notify_fanout_script(
            codex_home=path.parent,
            commands=_merge_notify_commands(
                preserved_commands,
                agent_deck_notify_command,
            ),
        )
        written_paths.append(str(fanout_path))
        skipped_paths.append(str(path))
        return
    raise ValueError(f"unsupported notify merge action for apply: {target.action}")


def _apply_hooks_target(
    *,
    target: CodexIntegrationMergeTarget,
    hooks_json: dict[str, Any],
    backup_paths: list[str],
    written_paths: list[str],
    skipped_paths: list[str],
) -> None:
    """按 dry-run 动作写入用户级 `hooks.json` 配置。

    入参：`target` 是 dry-run 对 `hooks.json` 的判断；`hooks_json` 是要创建或合并的 hooks；
    `backup_paths`/`written_paths`/`skipped_paths` 是调用方收集结果的列表。
    返回：无返回值。
    错误处理：不支持的 action 抛 ValueError；JSON 或文件错误按标准异常传播。
    副作用：可能创建或重写 `hooks.json`，修改已有文件前创建备份。
    """

    path = Path(target.path)
    if target.action == "already_configured":
        skipped_paths.append(str(path))
        return
    if target.action == "create_hooks":
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(path, hooks_json)
        written_paths.append(str(path))
        return
    if target.action == "merge_hooks":
        backup_paths.append(str(_backup_file(path)))
        existing = json.loads(path.read_text(encoding="utf-8"))
        merged = _merge_hooks_json(existing, hooks_json)
        _write_json_file(path, merged)
        written_paths.append(str(path))
        return
    if target.action == "refresh_hooks":
        backup_paths.append(str(_backup_file(path)))
        existing = json.loads(path.read_text(encoding="utf-8"))
        merged = _replace_agent_deck_hooks_json(existing, hooks_json)
        _write_json_file(path, merged)
        written_paths.append(str(path))
        return
    raise ValueError(f"unsupported hooks merge action for apply: {target.action}")


def _apply_managed_system_integration(
    *,
    report: CodexDetectionReport,
    guide: CodexIntegrationGuide,
    daemon_url: str,
) -> CodexIntegrationInstallResult:
    """写入 Codex managed-system hooks 并清理用户级 Agent Deck hooks。

    入参：`report` 是同次检测报告；`guide` 是 managed-system guide；`daemon_url` 用于
    生成 wrapper fallback 命令。
    返回：`CodexIntegrationInstallResult`。
    错误处理：缺少 managed TOML 或 wrapper 路径时抛 ValueError；文件系统错误按 OSError
    传播。
    副作用：创建/修改系统 requirements、写 managed wrapper、可能备份并重写用户
    `hooks.json` 以移除重复的 user-level Agent Deck hooks。
    """

    if guide.managed_requirements_toml is None or guide.managed_wrapper_path is None:
        raise ValueError("managed-system guide missing required paths")
    backup_paths: list[str] = []
    written_paths: list[str] = []
    skipped_paths: list[str] = []
    wrapper_path = Path(guide.managed_wrapper_path)
    _write_managed_hook_wrapper(
        wrapper_path=wrapper_path,
        fallback_command=_agent_deck_hook_base_command(),
    )
    written_paths.append(str(wrapper_path))
    _apply_managed_requirements_target(
        target=guide.merge_dry_run.hooks,
        managed_requirements_toml=guide.managed_requirements_toml,
        backup_paths=backup_paths,
        written_paths=written_paths,
        skipped_paths=skipped_paths,
    )
    _cleanup_user_agent_deck_hooks(
        path=Path(report.configuration.user_hooks_path),
        backup_paths=backup_paths,
        written_paths=written_paths,
        skipped_paths=skipped_paths,
    )
    return CodexIntegrationInstallResult(
        applied=True,
        writes_files=True,
        mode="managed-system",
        detection_report=report,
        backup_paths=tuple(backup_paths),
        written_paths=tuple(written_paths),
        skipped_paths=tuple(skipped_paths),
    )


def _apply_managed_requirements_target(
    *,
    target: CodexIntegrationMergeTarget,
    managed_requirements_toml: str,
    backup_paths: list[str],
    written_paths: list[str],
    skipped_paths: list[str],
) -> None:
    """按 dry-run 动作写入系统 `requirements.toml` 的 managed hook block。

    入参：`target` 是系统 requirements 的 dry-run 判断；`managed_requirements_toml`
    是当前版本 block；`backup_paths`/`written_paths`/`skipped_paths` 是结果收集列表。
    返回：无返回值。
    错误处理：不支持的 action 抛 ValueError；文件读写错误按 OSError 传播。
    副作用：可能创建、追加或重写系统 requirements，修改已有文件前创建备份。
    """

    path = Path(target.path)
    _validate_toml_document(managed_requirements_toml)
    if target.action == "already_configured":
        skipped_paths.append(str(path))
        return
    if target.action == "create_system_requirements":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{managed_requirements_toml}\n", encoding="utf-8")
        written_paths.append(str(path))
        return
    if target.action == "append_managed_hooks":
        backup_paths.append(str(_backup_file(path)))
        existing = path.read_text(encoding="utf-8")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{separator}{managed_requirements_toml}\n"
        _validate_toml_document(updated)
        path.write_text(updated, encoding="utf-8")
        written_paths.append(str(path))
        return
    if target.action == "refresh_managed_hooks":
        backup_paths.append(str(_backup_file(path)))
        existing = path.read_text(encoding="utf-8")
        updated = _replace_managed_requirements_block(
            existing,
            managed_requirements_toml,
        )
        _validate_toml_document(updated)
        path.write_text(updated, encoding="utf-8")
        written_paths.append(str(path))
        return
    raise ValueError(f"unsupported managed-system merge action: {target.action}")


def _cleanup_user_agent_deck_hooks(
    *,
    path: Path,
    backup_paths: list[str],
    written_paths: list[str],
    skipped_paths: list[str],
) -> None:
    """从用户级 `hooks.json` 移除 Agent Deck hooks，避免与 managed hooks 重复。

    入参：`path` 是用户级 hooks.json；三个列表用于记录 apply 结果。
    返回：无返回值。
    错误处理：JSON 解析错误按标准异常传播；文件不存在时跳过。
    副作用：若文件存在且包含 Agent Deck hooks，会备份并重写该文件。
    """

    if not path.exists():
        skipped_paths.append(str(path))
        return
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not _contains_agent_deck_hook_command(existing):
        skipped_paths.append(str(path))
        return
    backup_paths.append(str(_backup_file(path)))
    cleaned = _replace_agent_deck_hooks_json(existing, {"hooks": {}})
    _write_json_file(path, cleaned)
    written_paths.append(str(path))


def _write_managed_hook_wrapper(
    *,
    wrapper_path: Path,
    fallback_command: list[str],
) -> None:
    """写入 managed-system 使用的稳定 hook wrapper。

    入参：`wrapper_path` 是系统级 wrapper 路径；`fallback_command` 是当前环境实际启动
    `agent-deck-codex-hook` 的命令前缀。
    返回：无返回值。
    错误处理：文件写入或 chmod 失败按 OSError 传播。
    副作用：创建父目录，重写 wrapper 文件并设置可执行权限。
    """

    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        _managed_hook_wrapper_script(fallback_command),
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)


def _merge_hooks_json(existing: Any, generated: dict[str, Any]) -> dict[str, Any]:
    """把 Agent Deck generated hooks 合并到现有 hooks object。

    入参：`existing` 是从用户 `hooks.json` 解析出的对象；`generated` 是
    `integration.hooks_json`。
    返回：新的 hooks JSON object；不会修改传入对象。
    错误处理：若 `existing` 顶层不是 object 或 `hooks` 不是 object，抛 ValueError。
    副作用：无；只构造内存 dict。
    """

    if not isinstance(existing, dict):
        raise ValueError("hooks.json top-level value must be an object")
    existing_hooks = existing.get("hooks")
    if existing_hooks is None:
        existing_hooks = {}
    if not isinstance(existing_hooks, dict):
        raise ValueError("hooks.json hooks field must be an object")
    generated_hooks = generated.get("hooks")
    if not isinstance(generated_hooks, dict):
        raise ValueError("generated hooks must contain hooks object")
    merged: dict[str, Any] = dict(existing)
    merged_hooks: dict[str, Any] = {
        key: list(value) if isinstance(value, list) else value
        for key, value in existing_hooks.items()
    }
    for event_name, hook_entries in generated_hooks.items():
        if not isinstance(hook_entries, list):
            continue
        existing_entries = merged_hooks.get(event_name)
        if existing_entries is None:
            merged_hooks[event_name] = list(hook_entries)
        elif isinstance(existing_entries, list):
            merged_hooks[event_name] = [*existing_entries, *hook_entries]
        else:
            raise ValueError(f"hooks.{event_name} must be a list")
    merged["hooks"] = merged_hooks
    return merged


def _replace_agent_deck_hooks_json(
    existing: Any,
    generated: dict[str, Any],
) -> dict[str, Any]:
    """用当前版本 hooks 替换已有 Agent Deck hooks。

    入参：`existing` 是用户现有 `hooks.json` 对象；`generated` 是当前版本生成的
    Agent Deck hooks。
    返回：保留非 Agent Deck hook、替换 Agent Deck hook 后的新 JSON object。
    错误处理：顶层或 `hooks` 字段结构不符合 Codex hooks object 时抛 ValueError。
    副作用：无；只构造内存 dict。
    """

    if not isinstance(existing, dict):
        raise ValueError("hooks.json top-level value must be an object")
    existing_hooks = existing.get("hooks")
    if existing_hooks is None:
        existing_hooks = {}
    if not isinstance(existing_hooks, dict):
        raise ValueError("hooks.json hooks field must be an object")
    generated_hooks = generated.get("hooks")
    if not isinstance(generated_hooks, dict):
        raise ValueError("generated hooks must contain hooks object")

    merged: dict[str, Any] = dict(existing)
    merged_hooks: dict[str, Any] = {}
    for event_name, existing_entries in existing_hooks.items():
        if not isinstance(existing_entries, list):
            raise ValueError(f"hooks.{event_name} must be a list")
        kept_entries = _remove_agent_deck_hook_entries(existing_entries)
        if kept_entries:
            merged_hooks[event_name] = kept_entries

    for event_name, generated_entries in generated_hooks.items():
        if not isinstance(generated_entries, list):
            continue
        existing_entries = merged_hooks.get(event_name, [])
        if not isinstance(existing_entries, list):
            raise ValueError(f"hooks.{event_name} must be a list")
        merged_hooks[event_name] = [*existing_entries, *generated_entries]
    merged["hooks"] = merged_hooks
    return merged


def _remove_agent_deck_hook_entries(entries: list[Any]) -> list[Any]:
    """从单个事件的 hook entries 中移除 Agent Deck 自身 entry 或 command。

    入参：`entries` 是某个 Codex hook event 下的 entry 数组。
    返回：删除 Agent Deck command 后仍有内容的 entries。
    错误处理：未知 entry 结构会原样保留，避免误删用户配置。
    副作用：无；只构造内存列表。
    """

    kept_entries: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept_entries.append(entry)
            continue
        if entry.get(_AGENT_DECK_HOOK_MARKER) is True:
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            kept_entries.append(entry)
            continue
        kept_hooks = [
            hook for hook in hooks if not _contains_agent_deck_hook_command(hook)
        ]
        if kept_hooks:
            kept_entry = dict(entry)
            kept_entry["hooks"] = kept_hooks
            kept_entries.append(kept_entry)
    return kept_entries


def _backup_file(path: Path) -> Path:
    """为已有配置文件创建同目录备份。

    入参：`path` 是即将被修改的现有文件路径。
    返回：创建出的备份文件路径。
    错误处理：若源文件不存在或复制失败，按 OSError 传播。
    副作用：复制一个备份文件到同目录，文件名追加 UTC 时间戳和 `.bak` 后缀。
    """

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.agent-deck-{timestamp}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """以稳定格式写入 JSON object。

    入参：`path` 是目标文件；`payload` 是要写入的 JSON object。
    返回：无返回值。
    错误处理：不可序列化对象或文件写入失败按标准异常传播。
    副作用：创建父目录并重写目标 JSON 文件。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def build_codex_integration_guide(
    *,
    daemon_url: str,
    permission_timeout_seconds: int,
    configuration: CodexConfigurationReport,
    mode: str = "user",
) -> CodexIntegrationGuide:
    """生成 Codex hooks/notify 的手动集成片段。

    入参：`daemon_url` 是 hook helper 发往 Agent Deck daemon 的 base URL；
    `permission_timeout_seconds` 是审批 hook 等待 daemon 决策的秒数；`configuration`
    是 Codex 配置路径检测结果；`mode` 选择 user 或 managed-system 引导。
    返回：`CodexIntegrationGuide`，其中 `writes_files` 固定为 False。
    错误处理：非正审批超时时间抛 ValueError，避免输出无效 hook 配置。
    副作用：只读解析用户级 `config.toml` 与 `hooks.json` 是否已有 Agent Deck 接入；
    不写配置文件。
    """

    if permission_timeout_seconds <= 0:
        raise ValueError("permission_timeout_seconds must be positive")
    if mode not in {"user", "managed-system"}:
        raise ValueError("mode must be 'user' or 'managed-system'")
    if mode == "managed-system":
        return _build_managed_system_integration_guide(
            daemon_url=daemon_url,
            permission_timeout_seconds=permission_timeout_seconds,
            configuration=configuration,
        )
    event_command = _with_shell_agent_pid_arg(
        shlex.join(
            [*_agent_deck_hook_base_command(), "event", "--daemon-url", daemon_url]
        )
    )
    permission_command = _with_shell_agent_pid_arg(
        shlex.join(
            [
                *_agent_deck_hook_base_command(),
                "permission-request",
                "--daemon-url",
                daemon_url,
                "--timeout-seconds",
                str(permission_timeout_seconds),
            ]
        )
    )
    smoke_permission_command = shlex.join(
        [
            *_agent_deck_hook_base_command(),
            "permission-request",
            "--daemon-url",
            daemon_url,
            "--timeout-seconds",
            "0.001",
        ]
    )
    hooks_json = _build_hooks_json(
        event_command=event_command,
        permission_command=permission_command,
        permission_timeout_seconds=permission_timeout_seconds + 5,
    )
    notify_toml = _toml_array(
        "notify",
        _agent_deck_notify_command(daemon_url),
    )
    merge_dry_run = build_codex_integration_merge_dry_run(
        configuration=configuration,
        notify_toml=notify_toml,
        hooks_json=hooks_json,
    )
    return CodexIntegrationGuide(
        mode="user",
        writes_files=False,
        daemon_command=_daemon_command_for_url(daemon_url),
        notify_toml=notify_toml,
        hooks_json=hooks_json,
        merge_dry_run=merge_dry_run,
        trust_steps=(
            "把 notify 片段合并到用户级 ~/.codex/config.toml；project .codex/config.toml 不能设置 notify。",
            "把 hooks_json 片段合并到用户级 ~/.codex/hooks.json，或等价地转成 ~/.codex/config.toml 内联 hooks。",
            "重启或新开 Codex 会话后，在 Codex 中输入 /hooks。",
            "检查 agent-deck-codex-hook event 与 permission-request，确认命令内容后 trust。",
        ),
        verification_commands=(
            _daemon_command_for_url(daemon_url),
            "agent-deckctl status",
            f"printf '{{\"session_id\":\"smoke\",\"hookEventName\":\"SessionStart\"}}' | {event_command}",
            f"printf '{{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"reason\":\"smoke\"}}' | {smoke_permission_command}",
        ),
        warnings=(
            "本命令不会自动写入 Codex 配置；请先人工合并并保留备份。",
            "非 managed hooks 需要在 /hooks 中 review/trust；未 trust 前 Codex 会跳过这些 hooks。",
            "PermissionRequest helper 默认 passthrough；仅当 agent-deck.toml 配置 mode=handle 时由 Agent Deck 接管审批并在 daemon 不可达时 fail-closed。",
            "notify 只是 turn 完成提醒 fallback；实时工具和审批状态以 hooks 为准。",
        ),
    )


def _build_managed_system_integration_guide(
    *,
    daemon_url: str,
    permission_timeout_seconds: int,
    configuration: CodexConfigurationReport,
) -> CodexIntegrationGuide:
    """生成 Codex managed-system hooks 的安装引导。

    入参：`daemon_url` 是 hook helper 发往 Agent Deck daemon 的 base URL；
    `permission_timeout_seconds` 是审批 hook 等待 daemon 决策的秒数；`configuration`
    提供系统 requirements 与 managed wrapper 路径。
    返回：`CodexIntegrationGuide`，其中 `managed_requirements_toml` 可写入系统
    `requirements.toml`。
    错误处理：路径字段不可用或超时非法时由调用方/Pydantic 报告。
    副作用：只读解析系统 requirements 是否已有 Agent Deck managed block；不写文件。
    """

    wrapper_path = Path(configuration.managed_hooks_dir) / "agent-deck-codex-hook"
    event_command = _with_shell_agent_pid_arg(
        shlex.join([str(wrapper_path), "event", "--daemon-url", daemon_url])
    )
    permission_command = _with_shell_agent_pid_arg(
        shlex.join(
            [
                str(wrapper_path),
                "permission-request",
                "--daemon-url",
                daemon_url,
                "--timeout-seconds",
                str(permission_timeout_seconds),
            ]
        )
    )
    smoke_permission_command = shlex.join(
        [
            str(wrapper_path),
            "permission-request",
            "--daemon-url",
            daemon_url,
            "--timeout-seconds",
            "0.001",
        ]
    )
    hooks_json = _build_hooks_json(
        event_command=event_command,
        permission_command=permission_command,
        permission_timeout_seconds=permission_timeout_seconds + 5,
    )
    managed_requirements_toml = _managed_requirements_hooks_toml(
        hooks_json,
        managed_dir=Path(configuration.managed_hooks_dir),
    )
    merge_dry_run = build_codex_managed_system_merge_dry_run(
        configuration=configuration,
        managed_requirements_toml=managed_requirements_toml,
    )
    return CodexIntegrationGuide(
        mode="managed-system",
        writes_files=False,
        daemon_command=_daemon_command_for_url(daemon_url),
        notify_toml="",
        hooks_json=hooks_json,
        managed_requirements_toml=managed_requirements_toml,
        managed_wrapper_path=str(wrapper_path),
        merge_dry_run=merge_dry_run,
        trust_steps=(
            "以管理员权限安装系统 requirements.toml 和 managed hook wrapper。",
            "重启或新开 Codex 会话后，managed hooks 会按 policy 受信任，不需要 /hooks 手动 trust。",
            "不要默认启用 allow_managed_hooks_only，避免禁用用户自己的 hooks 或插件 hooks。",
        ),
        verification_commands=(
            _daemon_command_for_url(daemon_url),
            "agent-deckctl status",
            f"printf '{{\"session_id\":\"smoke\",\"hookEventName\":\"SessionStart\"}}' | {event_command}",
            f"printf '{{\"session_id\":\"smoke\",\"tool_name\":\"Bash\",\"reason\":\"smoke\"}}' | {smoke_permission_command}",
        ),
        warnings=(
            "managed-system 模式需要管理员权限写入系统路径。",
            "本模式只管理 lifecycle hooks；notify turn-complete fallback 仍使用用户级 config.toml。",
            "安装后会清理用户级 Agent Deck hooks，避免 managed 与 user hooks 重复上报。",
        ),
    )


def build_codex_integration_merge_dry_run(
    *,
    configuration: CodexConfigurationReport,
    notify_toml: str,
    hooks_json: dict[str, Any],
) -> CodexIntegrationMergeDryRun:
    """读取现有用户级 Codex 配置并生成合并 dry-run。

    入参：`configuration` 提供 `config.toml` 与 `hooks.json` 路径；`notify_toml` 是建议
    添加的 notify 片段；`hooks_json` 是当前版本希望安装的 Agent Deck hooks。
    返回：`CodexIntegrationMergeDryRun`，包含两个目标文件的动作建议。
    错误处理：文件读取或解析失败不会向外抛出，而是收敛为 `manual_merge_required`
    和 warning，避免检测命令因为用户配置格式问题不可用。
    副作用：只读打开最多两个用户级 Codex 配置文件；不写文件、不输出原始配置内容。
    """

    notify_target, notify_warning = _build_notify_merge_target(
        path=Path(configuration.user_config_path),
    )
    hooks_target, hooks_warning = _build_hooks_merge_target(
        path=Path(configuration.user_hooks_path),
        hooks_json=hooks_json,
    )
    recommended_edits = _recommended_merge_edits(
        notify=notify_target,
        hooks=hooks_target,
        notify_toml=notify_toml,
    )
    warnings = tuple(
        warning for warning in (notify_warning, hooks_warning) if warning is not None
    )
    return CodexIntegrationMergeDryRun(
        writes_files=False,
        notify=notify_target,
        hooks=hooks_target,
        recommended_edits=recommended_edits,
        warnings=warnings,
    )


def build_codex_managed_system_merge_dry_run(
    *,
    configuration: CodexConfigurationReport,
    managed_requirements_toml: str,
) -> CodexIntegrationMergeDryRun:
    """读取系统 `requirements.toml` 并生成 managed-system 合并 dry-run。

    入参：`configuration` 提供系统 requirements 路径；`managed_requirements_toml`
    是当前版本生成的 Agent Deck managed hook TOML block。
    返回：复用 `CodexIntegrationMergeDryRun` 结构，其中 `hooks` 指向系统 requirements，
    `notify` 标记为不适用。
    错误处理：文件读取或解析失败收敛为 `manual_merge_required` 与 warning。
    副作用：只读打开系统 requirements；不写文件、不输出原始配置内容。
    """

    requirements_path = Path(configuration.system_requirements_path)
    hooks_target, hooks_warning = _build_managed_requirements_merge_target(
        path=requirements_path,
        managed_requirements_toml=managed_requirements_toml,
    )
    notify_target = CodexIntegrationMergeTarget(
        path=configuration.user_config_path,
        exists=Path(configuration.user_config_path).exists(),
        action="not_applicable",
        reason="managed-system 模式不写 notify；保留用户级 notify fan-out 作为 turn-complete fallback。",
    )
    recommended_edits = _recommended_managed_system_edits(hooks=hooks_target)
    warnings = (hooks_warning,) if hooks_warning is not None else ()
    return CodexIntegrationMergeDryRun(
        writes_files=False,
        notify=notify_target,
        hooks=hooks_target,
        recommended_edits=recommended_edits,
        warnings=warnings,
    )


def _build_managed_requirements_merge_target(
    *,
    path: Path,
    managed_requirements_toml: str,
) -> tuple[CodexIntegrationMergeTarget, str | None]:
    """检查系统 requirements 中 Agent Deck managed hook block 的状态。

    入参：`path` 是系统 requirements 路径；`managed_requirements_toml` 是当前版本 block。
    返回：merge target 和可选 warning。
    错误处理：读取或 TOML 解析失败收敛为 `manual_merge_required`。
    副作用：只读检查并可能读取一个 TOML 文件。
    """

    if not path.exists():
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=False,
                action="create_system_requirements",
                reason="系统 requirements.toml 不存在，可创建并写入 Agent Deck managed hooks。",
            ),
            None,
        )
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="系统 requirements.toml 无法安全解析；请人工检查后合并 managed hooks。",
            ),
            f"无法解析 {path}: {exc}",
        )
    features = data.get("features")
    if isinstance(features, dict) and features.get("hooks") is False:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="[features].hooks=false 会禁用 hooks；需要人工确认是否改为允许。",
            ),
            None,
        )
    existing_block = _extract_managed_requirements_block(text)
    if existing_block is not None:
        if existing_block.strip() == managed_requirements_toml.strip():
            return (
                CodexIntegrationMergeTarget(
                    path=str(path),
                    exists=True,
                    action="already_configured",
                    reason="系统 requirements.toml 已包含当前 Agent Deck managed hooks。",
                ),
                None,
            )
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="refresh_managed_hooks",
                reason="系统 requirements.toml 已包含旧 Agent Deck managed block，可刷新。",
            ),
            None,
        )
    if "agent-deck-codex-hook" in text:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="系统 requirements.toml 已含未标记的 Agent Deck hook；请人工合并以避免重复。",
            ),
            None,
        )
    if "hooks" in data:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="系统 requirements.toml 已有 hooks 表；为避免重复 TOML 表，需要人工合并。",
            ),
            None,
        )
    return (
        CodexIntegrationMergeTarget(
            path=str(path),
            exists=True,
            action="append_managed_hooks",
            reason="系统 requirements.toml 存在，可追加 Agent Deck managed hook block。",
        ),
        None,
    )


def _recommended_managed_system_edits(
    *,
    hooks: CodexIntegrationMergeTarget,
) -> tuple[str, ...]:
    """生成 managed-system 模式的人工编辑建议。

    入参：`hooks` 是系统 requirements 的 merge target。
    返回：按执行顺序排列的编辑说明。
    错误处理：无。
    副作用：无；只格式化字符串。
    """

    edits: list[str] = []
    if hooks.action == "create_system_requirements":
        edits.append(f"创建 {hooks.path}，写入 Agent Deck managed hooks block。")
    elif hooks.action == "append_managed_hooks":
        edits.append(f"向 {hooks.path} 追加 Agent Deck managed hooks block。")
    elif hooks.action == "refresh_managed_hooks":
        edits.append(f"备份 {hooks.path}，刷新 Agent Deck managed hooks block。")
    elif hooks.action == "manual_merge_required":
        edits.append(f"人工检查 {hooks.path} 后合并 Agent Deck managed hooks。")
    return tuple(edits)


def _build_notify_merge_target(
    *,
    path: Path,
) -> tuple[CodexIntegrationMergeTarget, str | None]:
    """检查用户级 `config.toml` 中 notify 的合并状态。

    入参：`path` 是用户级 Codex `config.toml` 路径。
    返回：merge target 和可选 warning；不会包含原始配置内容。
    错误处理：读取或 TOML 解析失败收敛为 `manual_merge_required`。
    副作用：只读检查并可能读取一个 TOML 文件。
    """

    if not path.exists():
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=False,
                action="create_config",
                reason="用户级 config.toml 不存在，可创建后加入 notify 片段。",
            ),
            None,
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="config.toml 无法安全解析；请人工检查后合并 notify。",
            ),
            f"无法解析 {path}: {exc}",
        )
    notify_value = data.get("notify")
    if _contains_agent_deck_hook_command(notify_value):
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="already_configured",
                reason="notify 已包含 agent-deck-codex-hook。",
            ),
            None,
        )
    if _find_notify_fanout_path(notify_value, codex_home=path.parent) is not None:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="refresh_notify_fanout",
                reason="notify 已指向 Agent Deck fan-out wrapper，可刷新 wrapper 内容。",
            ),
            None,
        )
    if notify_value is None:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="append_notify",
                reason="config.toml 存在但未设置 notify，可添加 Agent Deck notify 片段。",
            ),
            None,
        )
    if _is_string_list(notify_value) and _has_replaceable_notify_line(path):
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="install_notify_fanout",
                reason="config.toml 已有 notify；可安装 Agent Deck fan-out wrapper 保留现有通知。",
            ),
            None,
        )
    return (
        CodexIntegrationMergeTarget(
            path=str(path),
            exists=True,
            action="manual_merge_required",
            reason="config.toml 已有 notify；为避免覆盖现有命令，需要人工合并。",
        ),
        None,
    )


def _build_hooks_merge_target(
    *,
    path: Path,
    hooks_json: dict[str, Any],
) -> tuple[CodexIntegrationMergeTarget, str | None]:
    """检查用户级 `hooks.json` 中 Agent Deck hooks 的合并状态。

    入参：`path` 是用户级 Codex `hooks.json` 路径；`hooks_json` 是当前版本希望安装的
    Agent Deck hooks。
    返回：merge target 和可选 warning；不会包含原始 hooks 内容。
    错误处理：读取或 JSON 解析失败收敛为 `manual_merge_required`。
    副作用：只读检查并可能读取一个 JSON 文件。
    """

    if not path.exists():
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=False,
                action="create_hooks",
                reason="用户级 hooks.json 不存在，可创建后写入 Agent Deck hooks 片段。",
            ),
            None,
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="manual_merge_required",
                reason="hooks.json 无法安全解析；请人工检查后合并 hooks。",
            ),
            f"无法解析 {path}: {exc}",
        )
    if _contains_current_agent_deck_hooks(data, hooks_json):
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="already_configured",
                reason="hooks.json 已包含当前 Agent Deck hooks。",
            ),
            None,
        )
    if _contains_agent_deck_hook_command(data):
        return (
            CodexIntegrationMergeTarget(
                path=str(path),
                exists=True,
                action="refresh_hooks",
                reason="hooks.json 已包含旧 Agent Deck hooks，可刷新为当前 helper 命令。",
            ),
            None,
        )
    return (
        CodexIntegrationMergeTarget(
            path=str(path),
            exists=True,
            action="merge_hooks",
            reason="hooks.json 存在但未包含 Agent Deck hooks，可合并 hooks_json 片段。",
        ),
        None,
    )


def _recommended_merge_edits(
    *,
    notify: CodexIntegrationMergeTarget,
    hooks: CodexIntegrationMergeTarget,
    notify_toml: str,
) -> tuple[str, ...]:
    """生成不包含原始配置内容的人工编辑建议。

    入参：`notify`/`hooks` 是两个配置目标的动作；`notify_toml` 是建议新增片段。
    返回：按执行顺序排列的编辑说明。
    错误处理：无；未知 action 会被忽略。
    副作用：无；只格式化字符串。
    """

    edits: list[str] = []
    if notify.action == "create_config":
        edits.append(f"创建 {notify.path}，写入：{notify_toml}")
    elif notify.action == "append_notify":
        edits.append(f"向 {notify.path} 添加：{notify_toml}")
    elif notify.action == "manual_merge_required":
        edits.append(f"人工检查 {notify.path} 后合并 notify；不要覆盖现有 notify。")
    elif notify.action == "install_notify_fanout":
        edits.append(f"备份 {notify.path}，安装 Agent Deck notify fan-out wrapper 保留现有 notify。")
    elif notify.action == "refresh_notify_fanout":
        edits.append(f"刷新 {notify.path} 指向的 Agent Deck notify fan-out wrapper。")
    if hooks.action == "create_hooks":
        edits.append(f"创建 {hooks.path}，内容使用 integration.hooks_json。")
    elif hooks.action == "merge_hooks":
        edits.append(f"把 integration.hooks_json 中的事件合并进 {hooks.path}。")
    elif hooks.action == "refresh_hooks":
        edits.append(f"备份 {hooks.path}，把旧 Agent Deck hooks 刷新为当前 helper 命令。")
    elif hooks.action == "manual_merge_required":
        edits.append(f"人工检查 {hooks.path} 后合并 hooks；不要覆盖现有 hooks。")
    return tuple(edits)


def _contains_agent_deck_hook_command(value: Any) -> bool:
    """递归检查配置对象中是否包含 Agent Deck hook 标记或 helper。

    入参：`value` 是 TOML/JSON 解析出的任意对象。
    返回：任意 dict 带 `_agent_deck=true` 或字符串包含 `agent-deck-codex-hook` 时为 True。
    错误处理：无；未知对象类型按 False 处理。
    副作用：无；只递归读取内存对象。
    """

    if isinstance(value, str):
        return "agent-deck-codex-hook" in value
    if isinstance(value, list | tuple):
        return any(_contains_agent_deck_hook_command(item) for item in value)
    if isinstance(value, dict):
        if value.get(_AGENT_DECK_HOOK_MARKER) is True:
            return True
        return any(_contains_agent_deck_hook_command(item) for item in value.values())
    return False


def _contains_current_agent_deck_hooks(existing: Any, generated: dict[str, Any]) -> bool:
    """检查已有 hooks 是否包含当前版本生成的全部 Agent Deck command。

    入参：`existing` 是用户现有 hooks JSON；`generated` 是当前版本生成的 hooks JSON。
    返回：当前版本所有 Agent Deck command 都能在已有 hooks 中找到时为 True。
    错误处理：未知结构按 False 处理。
    副作用：无；只递归读取内存对象。
    """

    generated_commands = _collect_agent_deck_hook_commands(generated)
    if not generated_commands:
        return False
    existing_commands = _collect_agent_deck_hook_commands(existing)
    return generated_commands.issubset(existing_commands)


def _collect_agent_deck_hook_commands(value: Any) -> set[str]:
    """递归收集 hooks JSON 中的 Agent Deck command 字符串。

    入参：`value` 是任意 JSON-like 对象。
    返回：所有包含 `agent-deck-codex-hook` 的 command 字段字符串集合。
    错误处理：未知对象类型按空集合处理。
    副作用：无；只读取内存对象。
    """

    commands: set[str] = set()
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and "agent-deck-codex-hook" in command:
            commands.add(command)
        for item in value.values():
            commands.update(_collect_agent_deck_hook_commands(item))
    elif isinstance(value, list | tuple):
        for item in value:
            commands.update(_collect_agent_deck_hook_commands(item))
    return commands


def _agent_deck_notify_command(daemon_url: str) -> list[str]:
    """返回 Agent Deck notify helper 命令数组。

    入参：`daemon_url` 是 hook helper 发往 Agent Deck daemon 的 base URL。
    返回：适合 Codex notify fan-out wrapper 调用的 argv 列表。
    错误处理：无。
    副作用：无；只构造内存列表。
    """

    return [*_agent_deck_hook_base_command(), "notify", "--daemon-url", daemon_url]


def _agent_deck_hook_base_command() -> list[str]:
    """返回当前环境中最稳妥的 `agent-deck-codex-hook` 启动前缀。

    入参：无。
    返回：在源码仓库里且 `uv` 可用时返回 `uv --directory <repo> run agent-deck-codex-hook`；
    否则返回已安装 console script 名称。
    错误处理：无；路径不存在或 `uv` 不在 PATH 时自动降级到 console script。
    副作用：检查当前源码路径和 PATH，不读写文件。
    """

    repo_root = Path(__file__).resolve().parents[3]
    uv_path = shutil.which("uv")
    if (repo_root / "pyproject.toml").exists() and uv_path:
        return [
            uv_path,
            "--directory",
            str(repo_root),
            "run",
            "agent-deck-codex-hook",
        ]
    return ["agent-deck-codex-hook"]


def _is_string_list(value: Any) -> bool:
    """检查值是否为非空字符串数组。

    入参：`value` 是 TOML 解析出的任意值。
    返回：列表非空且所有元素都是字符串时为 True。
    错误处理：无。
    副作用：无；只读取内存对象。
    """

    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) for item in value
    )


def _read_existing_notify_command(path: Path) -> list[str] | None:
    """读取已有用户级 notify 命令。

    入参：`path` 是用户级 Codex `config.toml` 路径。
    返回：字符串 argv 列表；无法解析为字符串数组时返回 None。
    错误处理：TOML 解析错误向上传播，调用方在 apply 阶段应中止。
    副作用：只读读取一个 TOML 文件。
    """

    notify_value = tomllib.loads(path.read_text(encoding="utf-8")).get("notify")
    if not _is_string_list(notify_value):
        return None
    return list(notify_value)


def _find_notify_fanout_path(
    notify_value: Any,
    *,
    codex_home: Path,
) -> Path | None:
    """从 notify argv 中识别 Agent Deck 托管的 fan-out wrapper 路径。

    入参：`notify_value` 是 TOML 解析出的 notify 值；`codex_home` 用于解析相对路径。
    返回：识别到 `agent-deck/notify-fanout.py` 时返回路径，否则返回 None。
    错误处理：未知 notify 结构按 None 处理。
    副作用：无；只解析内存对象和路径字符串。
    """

    if not _is_string_list(notify_value):
        return None
    for item in notify_value:
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = codex_home / candidate
        if (
            candidate.name == _AGENT_DECK_NOTIFY_FANOUT_NAME
            and candidate.parent.name == _AGENT_DECK_NOTIFY_DIRNAME
        ):
            return candidate
    return None


def _read_notify_fanout_commands(fanout_path: Path) -> list[list[str]]:
    """读取已有 Agent Deck fan-out wrapper 中可保留的非 Agent Deck 命令。

    入参：`fanout_path` 是 `notify-fanout.py` 路径。
    返回：去除 Agent Deck 自身命令后的 notify 命令数组；无法解析时返回空数组。
    错误处理：读取失败、正则不匹配、JSON 解析失败或结构非法都会收敛为空数组。
    副作用：只读读取 wrapper 文件。
    """

    try:
        text = fanout_path.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(
        r"^COMMANDS\s*=\s*(\[.*?\])\n[A-Z_]+\s*=",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return []
    try:
        raw_commands = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_commands, list):
        return []
    commands: list[list[str]] = []
    for raw_command in raw_commands:
        if _is_string_list(raw_command) and not _is_agent_deck_command(raw_command):
            commands.append(list(raw_command))
    return commands


def _merge_notify_commands(
    preserved_commands: list[list[str]],
    agent_deck_notify_command: list[str],
) -> list[list[str]]:
    """合并现有 notify 命令与 Agent Deck notify 命令并去重。

    入参：`preserved_commands` 是应继续执行的原有通知命令；`agent_deck_notify_command`
    是当前环境下的 Agent Deck notify 命令。
    返回：去除 Agent Deck 自身旧命令和重复项后的命令数组，最后包含当前 Agent Deck 命令。
    错误处理：无；非法命令由上游解析阶段过滤。
    副作用：无；只处理内存列表。
    """

    merged: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in [*preserved_commands, agent_deck_notify_command]:
        if _is_agent_deck_command(command) and command != agent_deck_notify_command:
            continue
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        merged.append(list(command))
    return merged


def _is_agent_deck_command(command: list[str]) -> bool:
    """判断 notify 命令是否是 Agent Deck 自己生成的命令。

    入参：`command` 是 notify argv 字符串列表。
    返回：命令任一参数包含 `agent-deck-codex-hook` 或托管 fan-out 路径时为 True。
    错误处理：无。
    副作用：无；只检查内存字符串。
    """

    return any(
        "agent-deck-codex-hook" in item
        or (
            _AGENT_DECK_NOTIFY_DIRNAME in item
            and _AGENT_DECK_NOTIFY_FANOUT_NAME in item
        )
        for item in command
    )


def _has_replaceable_notify_line(path: Path) -> bool:
    """检查 config.toml 是否有可安全替换的顶层 notify 单行。

    入参：`path` 是用户级 Codex `config.toml` 路径。
    返回：存在以 `notify =` 开头的非缩进行时为 True；否则要求人工合并。
    错误处理：文件读取失败按 OSError 传播。
    副作用：只读读取一个 TOML 文件。
    """

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("notify") and "=" in line:
            return True
    return False


def _replace_notify_line(*, path: Path, notify_toml: str) -> None:
    """替换 config.toml 中可安全识别的顶层 notify 单行。

    入参：`path` 是用户级 Codex `config.toml` 路径；`notify_toml` 是新 notify 行。
    返回：无返回值。
    错误处理：找不到可替换行时抛 ValueError；文件读写错误按 OSError 传播。
    副作用：重写 config.toml 内容。
    """

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    output: list[str] = []
    for line in lines:
        if not replaced and line.startswith("notify") and "=" in line:
            newline = "\n" if line.endswith("\n") else ""
            output.append(f"{notify_toml}{newline}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        raise ValueError("config.toml does not contain a replaceable notify line")
    path.write_text("".join(output), encoding="utf-8")


def _write_notify_fanout_script(
    *,
    codex_home: Path,
    commands: list[list[str]],
) -> Path:
    """写入 Agent Deck notify fan-out wrapper。

    入参：`codex_home` 是 Codex home 目录；`commands` 是要顺序调用的 notify 命令数组。
    返回：写入的 wrapper 脚本路径。
    错误处理：文件写入失败按 OSError 传播。
    副作用：创建 `codex_home/agent-deck/notify-fanout.py` 并设置可执行权限。
    """

    fanout_dir = codex_home / _AGENT_DECK_NOTIFY_DIRNAME
    fanout_dir.mkdir(parents=True, exist_ok=True)
    fanout_path = fanout_dir / _AGENT_DECK_NOTIFY_FANOUT_NAME
    fanout_path.write_text(_notify_fanout_script(commands), encoding="utf-8")
    fanout_path.chmod(0o755)
    return fanout_path


def _notify_fanout_script(commands: list[list[str]]) -> str:
    """生成 notify fan-out Python 脚本文本。

    入参：`commands` 是要顺序调用的 notify 命令数组；每个命令都会收到同一个 JSON 参数
    或同一份 stdin。
    返回：完整 Python 脚本文本。
    错误处理：不可 JSON 序列化的命令会由 `json.dumps` 抛出异常。
    副作用：无；只格式化字符串。
    """

    commands_json = json.dumps(commands, ensure_ascii=False, indent=2)
    return f'''#!/usr/bin/env python3
"""Agent Deck managed Codex notify fan-out.

This file is generated by agent-deckctl codex-install --apply. It preserves an
existing Codex notify command while forwarding the same notification to Agent Deck.
"""

from __future__ import annotations

import subprocess
import sys

COMMANDS = {commands_json}
TIMEOUT_SECONDS = 10


def main() -> int:
    payload_arg = sys.argv[1] if len(sys.argv) > 1 else None
    stdin_data = None if payload_arg is not None else sys.stdin.read()
    for command in COMMANDS:
        argv = [*command]
        if payload_arg is not None:
            argv.append(payload_arg)
        try:
            subprocess.run(
                argv,
                input=stdin_data,
                text=stdin_data is not None,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except Exception as exc:
            print(f"agent-deck notify fan-out failed for {{command[0]}}: {{exc}}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _managed_requirements_hooks_toml(
    hooks_json: dict[str, Any],
    *,
    managed_dir: Path,
) -> str:
    """把 hooks JSON 片段转换为 requirements.toml 内联 hooks block。

    入参：`hooks_json` 是 `_build_hooks_json` 生成的 hooks object；`managed_dir` 是
    managed hook scripts 所在绝对目录。
    返回：带 Agent Deck marker 的 TOML block。
    错误处理：hooks 结构非法时抛 ValueError。
    副作用：无；只格式化内存对象。
    """

    hooks = hooks_json.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hooks_json must contain hooks object")
    lines = [
        _AGENT_DECK_MANAGED_BLOCK_BEGIN,
        "# Generated by agent-deckctl codex-install --managed-system --apply.",
        "# Managed hooks from requirements.toml are trusted by Codex policy.",
        "",
        "[hooks]",
        f"managed_dir = {_toml_string(str(managed_dir.expanduser()))}",
    ]
    for event_name in [*_LIFECYCLE_HOOK_EVENTS, "PermissionRequest"]:
        event_entries = hooks.get(event_name)
        if not isinstance(event_entries, list):
            continue
        for event_entry in event_entries:
            if not isinstance(event_entry, dict):
                continue
            lines.append("")
            lines.append(f"[[hooks.{event_name}]]")
            matcher = event_entry.get("matcher")
            if isinstance(matcher, str):
                lines.append(f"matcher = {_toml_string(matcher)}")
            hook_entries = event_entry.get("hooks")
            if not isinstance(hook_entries, list):
                continue
            for hook_entry in hook_entries:
                if not isinstance(hook_entry, dict):
                    continue
                lines.append("")
                lines.append(f"[[hooks.{event_name}.hooks]]")
                for key in ("type", "command", "timeout", "statusMessage"):
                    if key not in hook_entry:
                        continue
                    value = hook_entry[key]
                    if isinstance(value, str):
                        lines.append(f"{key} = {_toml_string(value)}")
                    elif isinstance(value, int):
                        lines.append(f"{key} = {value}")
    lines.append("")
    lines.append(_AGENT_DECK_MANAGED_BLOCK_END)
    return "\n".join(lines)


def _validate_toml_document(text: str) -> None:
    """验证生成或合并后的 TOML 文档可解析。

    入参：`text` 是完整 TOML 文本。
    返回：无返回值；能返回代表解析通过。
    错误处理：TOML 解析失败时抛 ValueError，避免写出会阻断 Codex 启动的配置。
    副作用：无；只解析内存字符串。
    """

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"generated managed requirements TOML is invalid: {exc}") from exc


def _toml_string(value: str) -> str:
    """把字符串格式化为 TOML basic string。

    入参：`value` 是要写入 TOML 的字符串。
    返回：可嵌入 TOML 的双引号字符串。
    错误处理：不可序列化时由 `json.dumps` 抛出异常。
    副作用：无；只格式化字符串。
    """

    return json.dumps(value, ensure_ascii=False)


def _extract_managed_requirements_block(text: str) -> str | None:
    """提取 Agent Deck 托管的 requirements TOML block。

    入参：`text` 是完整 requirements.toml 文本。
    返回：包含 marker 的 block；未找到时返回 None。
    错误处理：marker 不完整时返回 None，让上层要求人工处理。
    副作用：无；只处理字符串。
    """

    start = text.find(_AGENT_DECK_MANAGED_BLOCK_BEGIN)
    end = text.find(_AGENT_DECK_MANAGED_BLOCK_END)
    if start < 0 or end < start:
        return None
    return text[start : end + len(_AGENT_DECK_MANAGED_BLOCK_END)]


def _replace_managed_requirements_block(
    text: str,
    managed_requirements_toml: str,
) -> str:
    """替换已有 Agent Deck managed requirements block。

    入参：`text` 是完整 requirements.toml；`managed_requirements_toml` 是新 block。
    返回：替换后的完整文本。
    错误处理：找不到完整 marker block 时抛 ValueError。
    副作用：无；只处理字符串。
    """

    start = text.find(_AGENT_DECK_MANAGED_BLOCK_BEGIN)
    end = text.find(_AGENT_DECK_MANAGED_BLOCK_END)
    if start < 0 or end < start:
        raise ValueError("managed Agent Deck requirements block not found")
    end += len(_AGENT_DECK_MANAGED_BLOCK_END)
    suffix = text[end:]
    if suffix and not suffix.startswith("\n"):
        suffix = f"\n{suffix}"
    return f"{text[:start]}{managed_requirements_toml}{suffix}"


def _managed_hook_wrapper_script(fallback_command: list[str]) -> str:
    """生成 managed-system hook wrapper 脚本文本。

    入参：`fallback_command` 是实际启动 `agent-deck-codex-hook` 的命令前缀。
    返回：POSIX shell wrapper 文本。
    错误处理：无。
    副作用：无；只格式化字符串。
    """

    command = " ".join(shlex.quote(part) for part in fallback_command)
    return f"""#!/bin/sh
# Agent Deck managed Codex hook wrapper.
# Generated by agent-deckctl codex-install --managed-system --apply.
exec {command} "$@"
"""


def _with_shell_agent_pid_arg(command: str) -> str:
    """给 Codex hook command 追加由 shell 展开的父进程 pid 参数。

    入参：`command` 是已经由 `shlex.join` 生成的安全命令前缀。
    返回：追加 `--agent-pid "$PPID"` 的命令字符串，供 Codex command hook 在运行时展开。
    错误处理：本 helper 不解析 shell；调用方负责只用于 Codex hook command 字符串。
    副作用：无；只格式化字符串。
    """

    return f'{command} --agent-pid "$PPID"'


def _daemon_command_for_url(daemon_url: str) -> str:
    """把 daemon URL 转成建议启动命令。

    入参：`daemon_url` 是 hook helper 需要访问的 Agent Deck daemon base URL。
    返回：`agent-deckd --host ... --port ...` 命令；URL 缺少 host/port 时使用默认本地值。
    错误处理：非法端口由 `urlparse(...).port` 抛出 ValueError 并交给调用方报告。
    副作用：无；只解析字符串。
    """

    parsed = urlparse(daemon_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    return shlex.join(["agent-deckd", "--host", host, "--port", str(port)])


def _build_configuration_report(
    codex_home: Path,
    *,
    system_requirements_path: Path | None = None,
    managed_hooks_dir: Path | None = None,
) -> CodexConfigurationReport:
    """生成 Codex 用户级配置文件的只读路径报告。

    入参：`codex_home` 是已解析的 Codex home 目录；`system_requirements_path` 是系统
    requirements 路径覆盖；`managed_hooks_dir` 是 managed wrapper 目录覆盖。
    返回：包含 `config.toml` 与 `hooks.json` 路径及存在性的 report。
    错误处理：路径存在性检查的底层 OSError 会按 Python 语义传播。
    副作用：检查两个路径是否存在，不读取文件内容、不创建文件。
    """

    user_config_path = codex_home / "config.toml"
    user_hooks_path = codex_home / "hooks.json"
    resolved_system_requirements_path = (
        system_requirements_path or DEFAULT_CODEX_SYSTEM_REQUIREMENTS_PATH
    ).expanduser()
    resolved_managed_hooks_dir = (
        managed_hooks_dir or DEFAULT_CODEX_MANAGED_HOOKS_DIR
    ).expanduser()
    return CodexConfigurationReport(
        codex_home=str(codex_home),
        user_config_path=str(user_config_path),
        user_config_exists=user_config_path.exists(),
        user_hooks_path=str(user_hooks_path),
        user_hooks_exists=user_hooks_path.exists(),
        system_requirements_path=str(resolved_system_requirements_path),
        system_requirements_exists=resolved_system_requirements_path.exists(),
        managed_hooks_dir=str(resolved_managed_hooks_dir),
        managed_hooks_dir_exists=resolved_managed_hooks_dir.exists(),
    )


def _resolve_codex_home(codex_home: Path | None) -> Path:
    """解析 Codex home 目录。

    入参：`codex_home` 是显式覆盖路径；为空时优先读取 `CODEX_HOME`，再退回
    `~/.codex`。
    返回：展开 `~` 后的绝对或用户传入相对路径。
    错误处理：环境变量内容无法构造路径时由 `Path` 抛出底层异常。
    副作用：读取当前进程环境变量，不访问文件系统。
    """

    if codex_home is not None:
        return codex_home.expanduser()
    env_value = os.environ.get("CODEX_HOME")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".codex"


def _build_hooks_json(
    *,
    event_command: str,
    permission_command: str,
    permission_timeout_seconds: int,
) -> dict[str, Any]:
    """生成可合并到 Codex `hooks.json` 的 lifecycle hook 片段。

    入参：`event_command` 是普通 lifecycle event helper 命令；`permission_command`
    是审批 helper 命令；`permission_timeout_seconds` 是 Codex hook 级 timeout。
    返回：符合 Codex hooks JSON 结构的 dict。
    错误处理：本函数不主动校验命令字符串；调用方负责传入已转义命令。
    副作用：无；只构造内存 dict。
    """

    hooks: dict[str, Any] = {
        event_name: [
            {
                _AGENT_DECK_HOOK_MARKER: True,
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": event_command,
                        "timeout": 5,
                    }
                ],
            }
        ]
        for event_name in _LIFECYCLE_HOOK_EVENTS
    }
    hooks["PermissionRequest"] = [
        {
            _AGENT_DECK_HOOK_MARKER: True,
            "matcher": "*",
            "hooks": [
                {
                    "type": "command",
                    "command": permission_command,
                    "timeout": permission_timeout_seconds,
                    "statusMessage": "Waiting for Agent Deck decision",
                }
            ],
        }
    ]
    return {"hooks": hooks}


def _toml_array(name: str, values: list[str]) -> str:
    """格式化简单 TOML 字符串数组。

    入参：`name` 是 TOML key；`values` 是字符串数组值。
    返回：一行 `name = ["..."]` 文本，可复制进 `config.toml`。
    错误处理：不可 JSON 序列化的值会由 `json.dumps` 抛出异常；当前入参类型已约束为字符串。
    副作用：无；只格式化内存字符串。
    """

    encoded_values = ", ".join(json.dumps(value) for value in values)
    return f"{name} = [{encoded_values}]"
