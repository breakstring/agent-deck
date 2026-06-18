"""Codex 只读检测与集成引导的单元测试。

本文件验证 Codex detection report 和 `--enable-integration` 引导内容的稳定契约。
测试不读取真实 `~/.codex` 内容、不启动 Codex、不写用户配置，也不连接 Agent Deck
daemon；所有路径都使用 pytest 临时目录或显式 fake 值。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agent_deck.adapters.codex_discovery import (
    build_codex_detection_report,
    install_codex_integration,
    validate_codex_managed_system_integration,
)


def test_detection_report_includes_install_paths_without_integration(
    tmp_path: Path,
) -> None:
    """验证默认检测报告只包含只读安装与配置路径信息。

    入参：`tmp_path` 是 pytest 临时目录，用作 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表默认 report 不生成集成修改建议。
    错误处理：模型字段缺失或路径解析错误由 pytest 断言报告。
    副作用：仅检查临时路径存在性，不访问真实 Codex 配置或外部进程。
    """

    app_path = tmp_path / "Codex.app"
    app_path.mkdir()
    report = build_codex_detection_report(
        codex_home=tmp_path / "codex-home",
        cli_path="/tmp/fake-codex",
        app_path=app_path,
        enable_integration=False,
    )

    assert report.product == "codex"
    assert report.installation.cli.detected is True
    assert report.installation.cli.path == "/tmp/fake-codex"
    assert report.installation.app.detected is True
    assert report.installation.app.path == str(app_path)
    assert report.configuration.user_config_path.endswith("config.toml")
    assert report.configuration.user_hooks_path.endswith("hooks.json")
    assert report.integration is None


def test_enable_integration_builds_manual_hook_and_notify_guide(
    tmp_path: Path,
) -> None:
    """验证集成引导输出可复制的 hooks、notify 与 trust 步骤。

    入参：`tmp_path` 是 fake `CODEX_HOME`，避免读取真实用户配置。
    返回：无返回值；断言通过代表 guide 不写文件且包含可执行 helper 命令。
    错误处理：缺少关键事件、命令或安全提示时由 pytest 断言报告。
    副作用：只构造内存模型，不执行 helper、不修改 Codex 配置。
    """

    report = build_codex_detection_report(
        codex_home=tmp_path / "codex-home",
        cli_path=None,
        app_path=tmp_path / "missing.app",
        enable_integration=True,
        daemon_url="http://127.0.0.1:9999",
    )

    guide = report.integration
    assert guide is not None
    assert guide.writes_files is False
    assert guide.merge_dry_run is not None
    assert guide.merge_dry_run.notify.action == "create_config"
    assert guide.merge_dry_run.hooks.action == "create_hooks"
    assert guide.daemon_command.endswith("--port 9999")
    assert "agent-deck-codex-hook" in guide.notify_toml
    assert "http://127.0.0.1:9999" in guide.notify_toml
    assert "PermissionRequest" in guide.hooks_json["hooks"]
    assert "SessionStart" in guide.hooks_json["hooks"]
    permission_hook = guide.hooks_json["hooks"]["PermissionRequest"][0]["hooks"][0]
    session_hook = guide.hooks_json["hooks"]["SessionStart"][0]["hooks"][0]
    assert "permission-request" in permission_hook["command"]
    assert "event" in session_hook["command"]
    assert any(
        "--daemon-url http://127.0.0.1:9999" in command
        for command in guide.verification_commands
    )
    assert "/hooks" in "\n".join(guide.trust_steps)
    assert any("不会自动写入" in warning for warning in guide.warnings)


def test_enable_integration_dry_run_detects_existing_agent_deck_config(
    tmp_path: Path,
) -> None:
    """验证 dry-run 能识别已经接入过 Agent Deck 的用户级配置。

    入参：`tmp_path` 是 fake `CODEX_HOME`，其中写入最小 `config.toml` 与 `hooks.json`。
    返回：无返回值；断言通过代表 dry-run 不回显原文件内容，只报告已配置状态。
    错误处理：解析或检测结果不符合预期时由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake 配置，不触碰真实 `~/.codex`。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'notify = ["agent-deck-codex-hook", "notify"]\n',
        encoding="utf-8",
    )
    initial_report = build_codex_detection_report(
        codex_home=codex_home,
        cli_path=None,
        app_path=tmp_path / "missing.app",
        enable_integration=True,
    )
    assert initial_report.integration is not None
    (codex_home / "hooks.json").write_text(
        json.dumps(initial_report.integration.hooks_json),
        encoding="utf-8",
    )

    report = build_codex_detection_report(
        codex_home=codex_home,
        cli_path=None,
        app_path=tmp_path / "missing.app",
        enable_integration=True,
    )

    guide = report.integration
    assert guide is not None
    assert guide.merge_dry_run is not None
    assert guide.merge_dry_run.notify.action == "already_configured"
    assert guide.merge_dry_run.hooks.action == "already_configured"
    assert "agent-deck-codex-hook event" not in "\n".join(
        guide.merge_dry_run.recommended_edits
    )


def test_enable_integration_dry_run_detects_stale_agent_deck_hooks(
    tmp_path: Path,
) -> None:
    """验证 dry-run 能识别旧版 Agent Deck hooks 需要刷新。

    入参：`tmp_path` 是 fake `CODEX_HOME`，其中写入旧的裸 `agent-deck-codex-hook`
    command。
    返回：无返回值；断言通过代表 dry-run 不重复追加旧 hooks，而是给出 refresh 动作。
    错误处理：动作判断不符合预期时由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake hooks.json。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(
        '{"hooks":{"SessionStart":[{"hooks":[{"type":"command",'
        '"command":"agent-deck-codex-hook event --daemon-url http://old.invalid"}]}]}}',
        encoding="utf-8",
    )

    report = build_codex_detection_report(
        codex_home=codex_home,
        cli_path=None,
        app_path=tmp_path / "missing.app",
        enable_integration=True,
    )

    guide = report.integration
    assert guide is not None
    assert guide.merge_dry_run.hooks.action == "refresh_hooks"
    assert any("刷新" in edit for edit in guide.merge_dry_run.recommended_edits)


def test_install_codex_integration_dry_run_does_not_write(tmp_path: Path) -> None:
    """验证安装命令默认 dry-run，不创建或修改 Codex 配置。

    入参：`tmp_path` 是 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表 dry-run 输出计划但没有文件系统写入。
    错误处理：若 dry-run 写入文件或 report 结构错误，由 pytest 断言报告。
    副作用：只检查临时目录下文件是否不存在，不触碰真实 `~/.codex`。
    """

    codex_home = tmp_path / "codex-home"
    result = install_codex_integration(codex_home=codex_home, apply=False)

    assert result.applied is False
    assert result.writes_files is False
    assert result.detection_report.integration is not None
    assert result.backup_paths == ()
    assert result.written_paths == ()
    assert not (codex_home / "config.toml").exists()
    assert not (codex_home / "hooks.json").exists()


def test_install_codex_integration_managed_system_dry_run_does_not_write(
    tmp_path: Path,
) -> None:
    """验证 managed-system dry-run 只输出系统安装计划，不写临时系统路径。

    入参：`tmp_path` 提供 fake `CODEX_HOME`、requirements 路径和 managed wrapper 目录。
    返回：无返回值；断言通过代表 managed-system guide 包含系统片段但没有文件写入。
    错误处理：若 dry-run 写入文件或缺少 managed 字段，由 pytest 断言报告。
    副作用：只检查 pytest 临时目录路径，不触碰真实 `/etc` 或 `/usr/local`。
    """

    codex_home = tmp_path / "codex-home"
    requirements_path = tmp_path / "etc" / "codex" / "requirements.toml"
    managed_hooks_dir = tmp_path / "usr-local" / "agent-deck" / "codex-hooks"

    result = install_codex_integration(
        codex_home=codex_home,
        apply=False,
        mode="managed-system",
        system_requirements_path=requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )

    guide = result.detection_report.integration
    assert result.mode == "managed-system"
    assert result.applied is False
    assert guide is not None
    assert guide.mode == "managed-system"
    assert guide.managed_wrapper_path == str(managed_hooks_dir / "agent-deck-codex-hook")
    assert guide.managed_requirements_toml is not None
    assert "BEGIN_AGENT_DECK_MANAGED_HOOKS" in guide.managed_requirements_toml
    managed_requirements = tomllib.loads(guide.managed_requirements_toml)
    assert managed_requirements["hooks"]["managed_dir"] == str(managed_hooks_dir)
    assert "不需要 /hooks 手动 trust" in "\n".join(guide.trust_steps)
    assert guide.merge_dry_run.hooks.action == "create_system_requirements"
    assert not requirements_path.exists()
    assert not managed_hooks_dir.exists()


def test_install_codex_integration_managed_system_refuses_existing_hooks_table(
    tmp_path: Path,
) -> None:
    """验证 managed-system 遇到已有系统 hooks 表时拒绝自动追加。

    入参：`tmp_path` 提供 fake 系统 requirements 路径。
    返回：无返回值；断言通过代表 installer 不会写出重复 `[hooks]` TOML 表。
    错误处理：若未拒绝或误改原文件，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake requirements 文件。
    """

    requirements_path = tmp_path / "etc" / "codex" / "requirements.toml"
    requirements_path.parent.mkdir(parents=True)
    requirements_path.write_text(
        '[hooks]\nmanaged_dir = "/existing/hooks"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manual merge required"):
        install_codex_integration(
            codex_home=tmp_path / "codex-home",
            apply=True,
            mode="managed-system",
            system_requirements_path=requirements_path,
            managed_hooks_dir=tmp_path / "managed-hooks",
        )

    assert requirements_path.read_text(encoding="utf-8") == (
        '[hooks]\nmanaged_dir = "/existing/hooks"\n'
    )


def test_validate_codex_managed_system_reports_clean_dry_run(
    tmp_path: Path,
) -> None:
    """验证 managed-system 只读验证能在未安装时给出安全通过结果。

    入参：`tmp_path` 提供 fake Codex home、系统 requirements 和 managed wrapper 路径。
    返回：无返回值；断言通过代表 validate 不写文件、生成 TOML 可解析且缺失文件只是 info。
    错误处理：若验证错误或写入临时路径，由 pytest 断言报告。
    副作用：只检查 pytest 临时路径，不触碰真实 Codex 或系统目录。
    """

    requirements_path = tmp_path / "etc" / "codex" / "requirements.toml"
    managed_hooks_dir = tmp_path / "usr-local" / "agent-deck" / "codex-hooks"

    result = validate_codex_managed_system_integration(
        codex_home=tmp_path / "codex-home",
        system_requirements_path=requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )

    assert result.ok is True
    assert result.mode == "managed-system"
    assert result.detection_report.integration is not None
    assert result.detection_report.integration.managed_requirements_toml is not None
    parsed = tomllib.loads(
        result.detection_report.integration.managed_requirements_toml
    )
    assert parsed["hooks"]["managed_dir"] == str(managed_hooks_dir)
    assert not requirements_path.exists()
    assert not managed_hooks_dir.exists()
    assert any(
        check.name == "system_requirements" and check.severity == "info"
        for check in result.checks
    )


def test_validate_codex_managed_system_flags_existing_hooks_table(
    tmp_path: Path,
) -> None:
    """验证 validate 能把需要人工合并的系统 hooks 表标成错误。

    入参：`tmp_path` 提供含既有 `[hooks]` 表的 fake requirements。
    返回：无返回值；断言通过代表 validate 不写文件且通过 JSON 模型暴露阻断项。
    错误处理：若未标成 error 或误改原文件，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake requirements 文件。
    """

    requirements_path = tmp_path / "etc" / "codex" / "requirements.toml"
    requirements_path.parent.mkdir(parents=True)
    requirements_path.write_text(
        '[hooks]\nmanaged_dir = "/existing/hooks"\n',
        encoding="utf-8",
    )

    result = validate_codex_managed_system_integration(
        codex_home=tmp_path / "codex-home",
        system_requirements_path=requirements_path,
        managed_hooks_dir=tmp_path / "managed-hooks",
    )

    assert result.ok is False
    assert any(
        check.name == "system_requirements"
        and check.severity == "error"
        and "人工合并" in check.message
        for check in result.checks
    )
    assert requirements_path.read_text(encoding="utf-8") == (
        '[hooks]\nmanaged_dir = "/existing/hooks"\n'
    )


def test_install_codex_integration_apply_creates_missing_files(tmp_path: Path) -> None:
    """验证 apply 会在缺失配置时创建 config.toml 和 hooks.json。

    入参：`tmp_path` 是 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表 apply 写入 Agent Deck notify 与 hooks 片段。
    错误处理：若文件缺失、内容不含 helper 或未记录 written paths，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake Codex 配置。
    """

    codex_home = tmp_path / "codex-home"
    result = install_codex_integration(codex_home=codex_home, apply=True)

    assert result.applied is True
    assert result.writes_files is True
    assert result.backup_paths == ()
    assert str(codex_home / "config.toml") in result.written_paths
    assert str(codex_home / "hooks.json") in result.written_paths
    assert "agent-deck-codex-hook" in (codex_home / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "agent-deck-codex-hook event" in (codex_home / "hooks.json").read_text(
        encoding="utf-8"
    )


def test_install_codex_integration_managed_system_apply_writes_system_files(
    tmp_path: Path,
) -> None:
    """验证 managed-system apply 写系统 requirements/wrapper 并清理用户级重复 hooks。

    入参：`tmp_path` 提供 fake 系统路径和 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表 requirements 带 managed block，wrapper 可执行，用户自定义
    hook 被保留而 Agent Deck user hook 被移除。
    错误处理：若未备份、未写系统文件或清理错误，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake 系统路径和 Codex 配置。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": "user-hook --keep"},
                                {
                                    "type": "command",
                                    "command": "agent-deck-codex-hook event",
                                },
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    requirements_path = tmp_path / "etc" / "codex" / "requirements.toml"
    managed_hooks_dir = tmp_path / "usr-local" / "agent-deck" / "codex-hooks"

    result = install_codex_integration(
        codex_home=codex_home,
        apply=True,
        mode="managed-system",
        system_requirements_path=requirements_path,
        managed_hooks_dir=managed_hooks_dir,
    )

    wrapper_path = managed_hooks_dir / "agent-deck-codex-hook"
    assert result.mode == "managed-system"
    assert str(requirements_path) in result.written_paths
    assert str(wrapper_path) in result.written_paths
    assert str(hooks_path) in result.written_paths
    assert len(result.backup_paths) == 1
    assert wrapper_path.exists()
    assert wrapper_path.stat().st_mode & 0o100
    wrapper_text = wrapper_path.read_text(encoding="utf-8")
    assert "agent-deck-codex-hook" in wrapper_text
    assert "exec uv " not in wrapper_text
    requirements_text = requirements_path.read_text(encoding="utf-8")
    assert "BEGIN_AGENT_DECK_MANAGED_HOOKS" in requirements_text
    parsed_requirements = tomllib.loads(requirements_text)
    assert parsed_requirements["hooks"]["managed_dir"] == str(managed_hooks_dir)
    assert str(wrapper_path) in requirements_text
    hooks_text = hooks_path.read_text(encoding="utf-8")
    assert "user-hook --keep" in hooks_text
    assert "agent-deck-codex-hook event" not in hooks_text


def test_install_codex_integration_apply_preserves_existing_notify_with_fanout(
    tmp_path: Path,
) -> None:
    """验证 apply 遇到已有 notify 时通过 fan-out 保留原通知。

    入参：`tmp_path` 是 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表自动安装会备份配置、创建 fan-out wrapper 和 hooks。
    错误处理：若原通知未备份、wrapper 未生成或 hooks 缺失，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake config.toml。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('notify = ["existing-command"]\n', encoding="utf-8")

    result = install_codex_integration(codex_home=codex_home, apply=True)

    assert result.applied is True
    assert len(result.backup_paths) == 1
    backup_path = Path(result.backup_paths[0])
    assert backup_path.read_text(encoding="utf-8") == 'notify = ["existing-command"]\n'
    assert "agent-deck/notify-fanout.py" in config_path.read_text(encoding="utf-8")
    fanout_path = codex_home / "agent-deck" / "notify-fanout.py"
    assert fanout_path.exists()
    fanout_text = fanout_path.read_text(encoding="utf-8")
    assert '"existing-command"' in fanout_text
    assert "agent-deck-codex-hook" in fanout_text
    assert (codex_home / "hooks.json").exists()


def test_install_codex_integration_apply_refreshes_existing_agent_deck_fanout(
    tmp_path: Path,
) -> None:
    """验证 apply 遇到旧 Agent Deck fan-out 时刷新 wrapper 而不套娃。

    入参：`tmp_path` 是 fake `CODEX_HOME`，其中预置一次旧版本安装留下的 wrapper。
    返回：无返回值；断言通过代表原通知被保留、旧 Agent Deck 命令被替换、config 不重写。
    错误处理：若 wrapper 嵌套、原命令丢失或 config 被误备份，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake config.toml、hooks.json 和 wrapper 文件。
    """

    codex_home = tmp_path / "codex-home"
    fanout_path = codex_home / "agent-deck" / "notify-fanout.py"
    fanout_path.parent.mkdir(parents=True)
    config_path = codex_home / "config.toml"
    config_path.write_text(
        f'notify = ["python3", "{fanout_path}"]\n',
        encoding="utf-8",
    )
    fanout_path.write_text(
        """#!/usr/bin/env python3
COMMANDS = [
  ["existing-command"],
  ["agent-deck-codex-hook", "notify", "--daemon-url", "http://old.invalid"]
]
TIMEOUT_SECONDS = 10
""",
        encoding="utf-8",
    )
    initial_report = build_codex_detection_report(
        codex_home=codex_home,
        cli_path=None,
        app_path=tmp_path / "missing.app",
        enable_integration=True,
    )
    assert initial_report.integration is not None
    (codex_home / "hooks.json").write_text(
        json.dumps(initial_report.integration.hooks_json),
        encoding="utf-8",
    )

    result = install_codex_integration(codex_home=codex_home, apply=True)

    assert result.applied is True
    assert result.backup_paths == ()
    assert str(config_path) in result.skipped_paths
    assert str(fanout_path) in result.written_paths
    assert config_path.read_text(encoding="utf-8") == (
        f'notify = ["python3", "{fanout_path}"]\n'
    )
    fanout_text = fanout_path.read_text(encoding="utf-8")
    assert '"existing-command"' in fanout_text
    assert "http://old.invalid" not in fanout_text
    assert "notify-fanout.py" not in fanout_text
    assert fanout_text.count("agent-deck-codex-hook") == 1


def test_install_codex_integration_apply_refreshes_existing_agent_deck_hooks(
    tmp_path: Path,
) -> None:
    """验证 apply 会刷新旧 Agent Deck hooks 并保留用户其他 hooks。

    入参：`tmp_path` 是 fake `CODEX_HOME`，其中 hooks.json 混合旧 Agent Deck command
    和用户自定义 command。
    返回：无返回值；断言通过代表旧 command 被移除、当前 helper command 被写入、用户 hook
    被保留。
    错误处理：若 hooks 未备份、旧 command 未清理或用户 hook 丢失，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake Codex 配置。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'notify = ["agent-deck-codex-hook", "notify"]\n',
        encoding="utf-8",
    )
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "user-command --keep",
                                },
                                {
                                    "type": "command",
                                    "command": (
                                        "agent-deck-codex-hook event "
                                        "--daemon-url http://old.invalid"
                                    ),
                                },
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = install_codex_integration(codex_home=codex_home, apply=True)

    assert result.applied is True
    assert len(result.backup_paths) == 1
    assert str(hooks_path) in result.written_paths
    hooks_text = hooks_path.read_text(encoding="utf-8")
    assert "user-command --keep" in hooks_text
    assert "http://old.invalid" not in hooks_text
    assert "uv --directory" in hooks_text or "agent-deck-codex-hook event" in hooks_text
    hooks_data = json.loads(hooks_text)
    assert "PermissionRequest" in hooks_data["hooks"]


def test_install_codex_integration_apply_refuses_unparseable_config(
    tmp_path: Path,
) -> None:
    """验证 apply 遇到无法解析的 config.toml 时拒绝自动写入。

    入参：`tmp_path` 是 fake `CODEX_HOME`。
    返回：无返回值；断言通过代表解析失败会要求人工合并。
    错误处理：若未抛错或原文件被覆盖，由 pytest 断言报告。
    副作用：只写 pytest 临时目录下的 fake config.toml。
    """

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("notify = [\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manual merge required"):
        install_codex_integration(codex_home=codex_home, apply=True)

    assert config_path.read_text(encoding="utf-8") == "notify = [\n"
