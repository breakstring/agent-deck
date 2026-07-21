"""Agent Deck 本地配置加载模型。

本模块负责把可编辑 TOML 配置转换为 daemon 启动所需的稳定默认值。它不启动 daemon、
不访问 Codex、不接管硬件，也不写配置文件；唯一副作用是按调用方提供的路径读取一个可选
TOML 文件。配置字段刻意使用硬件中立命名，当前默认设备 profile 是 `n4pro`，但调用方不需要
记住具体 N4 Pro renderer 的实现参数名。
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_CONFIG_ENV = "AGENT_DECK_CONFIG"
_CWD_CONFIG_PATH = Path("agent-deck.toml")
_USER_CONFIG_PATH = Path.home() / "Library/Application Support/AgentDeck/config.toml"


class AgentDeckConfigError(ValueError):
    """表示 Agent Deck 配置文件无法读取、解析或校验。

    入参：标准 `ValueError` 参数，通常包含面向 CLI 用户的错误说明。
    返回：异常实例。
    错误处理：调用方应捕获并转换成 CLI exit code 或 status 诊断。
    副作用：无；异常本身不读写外部资源。
    """


class HardwareRendererConfig(BaseModel):
    """描述真实硬件渲染的用户可配置默认值。

    入参：`enabled` 控制是否默认执行真实硬件渲染；`device_profile` 是硬件能力 profile，
    当前默认 `n4pro`；`render_interval_seconds` 是每轮真实硬件刷新/播放窗口秒数；
    `fps` 是按钮动画刷新率；`frame_root` 是当前 Codex key 预渲染帧目录。
    返回：frozen Pydantic model，供 CLI 映射到 daemon poller config。
    错误处理：非正 interval、非法 fps 或空 profile 由 Pydantic 校验失败。
    副作用：模型自身不读取文件、不访问硬件。
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    device_profile: str = Field(default="n4pro", min_length=1)
    render_interval_seconds: float = Field(default=3.0, gt=0)
    fps: int = Field(default=10, gt=0, le=20)
    frame_root: Path = Path("assets/codex/generated/n4pro-key-112-fps10")


class PermissionRequestMode(StrEnum):
    """定义 Codex PermissionRequest hook 的 Agent Deck 响应策略。

    入参：枚举值来自本地 TOML 配置。
    返回：字符串枚举，可直接参与 Pydantic 校验和 CLI 分支判断。
    错误处理：未知模式由 Pydantic 报告配置校验失败。
    副作用：无；定义枚举不访问外部资源。
    """

    PASSTHROUGH = "passthrough"
    HANDLE = "handle"
    DENY = "deny"


class CodexPermissionRequestConfig(BaseModel):
    """配置 Agent Deck 是否接管 Codex 权限审批请求。

    入参：`mode` 为 `passthrough` 时不返回 decision，让 Codex 原生审批继续；`handle`
    时进入 Agent Deck decision broker；`deny` 时直接拒绝。`timeout_seconds` 是 handle
    模式等待硬件/API 决策的秒数；`deny_message` 是 deny 模式和可控拒绝时返回给 Codex 的说明。
    返回：frozen Pydantic model。
    错误处理：非法 mode、非正 timeout 或空 deny_message 由 Pydantic 校验失败。
    副作用：模型自身不访问 daemon、Codex、文件或硬件。
    """

    model_config = ConfigDict(frozen=True)

    mode: PermissionRequestMode = PermissionRequestMode.PASSTHROUGH
    timeout_seconds: float = Field(default=25.0, gt=0)
    deny_message: str = Field(default="Denied by Agent Deck.", min_length=1)


class CodexPetMotion(StrEnum):
    """定义 Codex 宠物在硬件表面上的动态展示策略。

    入参：枚举值来自 ``[codex.pet].motion``。
    返回：字符串枚举，可直接传给 daemon 的宠物场景控制器。
    错误处理：未知字符串由 Pydantic 配置校验拒绝。
    副作用：无；声明枚举不读取系统 reduced-motion 设置。
    """

    AUTO = "auto"
    FULL = "full"
    REDUCED = "reduced"


class CodexPetConfig(BaseModel):
    """配置 Agent Deck 对 Codex 全局宠物选择的只读展示。

    入参：``enabled`` 控制 PETS 面板和宠物 Key 动态资源；
    ``refresh_interval_seconds`` 是重新读取 Codex 选择与 manifest 的间隔；``panel_fps``
    是 PETS 可见时背景的最高目标帧率；``motion`` 控制完整、减少或系统跟随动画。
    返回：frozen Pydantic model；不包含独立 ``pet_id``，始终跟随 Codex 全局选择。
    错误处理：非正刷新间隔、超出 1-20 的 FPS 或未知 motion 由 Pydantic 拒绝。
    副作用：模型自身不读取 Codex 配置、不加载图片、不访问硬件。
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    refresh_interval_seconds: float = Field(default=5.0, gt=0)
    panel_fps: int = Field(default=8, ge=1, le=20)
    motion: CodexPetMotion = CodexPetMotion.AUTO


class CodexConfig(BaseModel):
    """配置 Agent Deck 对 Codex 专属能力的处理方式。

    入参：`permission_request` 控制 sandbox/escalation 审批 hook 的响应策略；`pet`
    控制只读跟随 Codex 全局宠物选择的硬件展示。
    返回：frozen Pydantic model。
    错误处理：子配置非法时由 Pydantic 报告。
    副作用：模型自身不读写外部资源。
    """

    model_config = ConfigDict(frozen=True)

    permission_request: CodexPermissionRequestConfig = Field(
        default_factory=CodexPermissionRequestConfig
    )
    pet: CodexPetConfig = Field(default_factory=CodexPetConfig)


class FocusActionConfig(BaseModel):
    """配置 Agent Deck 是否允许执行真实 focus action。

    入参：`enabled` 为 true 时，daemon 收到 `focus_agent` intent 且目标 agent 有显式
    focus target 后，会调用本机 action executor；默认 true，让 agent key 直接尝试激活会话。
    返回：frozen Pydantic model。
    错误处理：字段类型非法时由 Pydantic 报告。
    副作用：模型自身不执行动作、不访问窗口系统。
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True


class ActionsConfig(BaseModel):
    """配置 Agent Deck 允许执行的外部动作开关。

    入参：`focus` 控制 `focus_agent` 是否允许执行真实本机 focus，默认允许。
    返回：frozen Pydantic model。
    错误处理：子配置非法时由 Pydantic 报告。
    副作用：模型自身不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    focus: FocusActionConfig = Field(default_factory=FocusActionConfig)


class AgentDeckConfig(BaseModel):
    """Agent Deck daemon 的本地可编辑配置根模型。

    入参：`hardware_renderer` 是真实硬件渲染相关默认值；`codex` 是 Codex 专属集成能力
    配置域；`actions` 是外部动作能力开关，focus 默认启用；后续可继续加入 agent、quota、
    interaction 等配置域。
    返回：frozen Pydantic model。
    错误处理：子配置非法时由 Pydantic 返回结构化校验错误。
    副作用：模型自身不读写外部资源。
    """

    model_config = ConfigDict(frozen=True)

    hardware_renderer: HardwareRendererConfig = Field(
        default_factory=HardwareRendererConfig
    )
    codex: CodexConfig = Field(default_factory=CodexConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)


def resolve_agent_deck_config_path(path: Path | None = None) -> Path:
    """解析 Agent Deck 配置路径，支持 hook 在任意 cwd 下读取稳定配置。

    入参：`path` 是 CLI 显式传入的配置路径；为 None 时依次检查 `AGENT_DECK_CONFIG`、
    当前目录 `agent-deck.toml` 和用户级 `~/Library/Application Support/AgentDeck/config.toml`。
    返回：要传给 `load_agent_deck_config` 的路径；路径可以不存在，此时 loader 会返回默认配置。
    错误处理：本函数不读取文件，不因路径不存在抛错。
    副作用：读取进程环境变量和当前目录文件存在性，不写文件、不访问网络或硬件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_CONFIG_ENV)
    if env_value:
        return Path(env_value).expanduser()
    if _CWD_CONFIG_PATH.exists():
        return _CWD_CONFIG_PATH
    return _USER_CONFIG_PATH


def load_agent_deck_config(path: Path) -> AgentDeckConfig:
    """读取可选 Agent Deck TOML 配置并返回带默认值的配置模型。

    入参：`path` 是 TOML 配置路径；不存在时返回内置默认配置。
    返回：`AgentDeckConfig`，其中缺失字段使用模型默认值。
    错误处理：TOML 语法错误、顶层不是 object 或 Pydantic 校验失败时抛
    `AgentDeckConfigError`；路径不存在不是错误。
    副作用：当文件存在时读取该文件内容，不写文件、不访问网络或硬件。
    """

    if not path.exists():
        return AgentDeckConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgentDeckConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AgentDeckConfigError(f"配置文件 {path} 不是合法 TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentDeckConfigError(f"配置文件 {path} 顶层必须是 TOML table")
    try:
        return AgentDeckConfig.model_validate(data)
    except ValidationError as exc:
        raise AgentDeckConfigError(f"配置文件 {path} 校验失败: {exc}") from exc


def resolve_hardware_renderer_defaults(
    config: AgentDeckConfig,
    *,
    disabled: bool,
    device_profile: str | None,
    render_interval_seconds: float | None,
    fps: int | None,
) -> HardwareRendererConfig:
    """合并配置文件默认值与 CLI 覆盖项，得到真实硬件渲染配置。

    入参：`config` 是已加载的配置；`disabled` 来自 `--disable-hardware-renderer`；
    其余可空参数来自 CLI 覆盖项，`None` 表示沿用配置文件默认值。
    返回：合并后的 `HardwareRendererConfig`。
    错误处理：覆盖后的值非法时由 Pydantic 抛 `ValidationError`。
    副作用：无；只构造新的不可变配置对象。
    """

    hardware = config.hardware_renderer
    data = hardware.model_dump()
    data.update(
        {
            "enabled": hardware.enabled and not disabled,
            "device_profile": device_profile or hardware.device_profile,
            "render_interval_seconds": (
                render_interval_seconds
                if render_interval_seconds is not None
                else hardware.render_interval_seconds
            ),
            "fps": fps if fps is not None else hardware.fps,
        }
    )
    return HardwareRendererConfig.model_validate(data)
