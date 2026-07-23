"""Codex 全局宠物选择、任务宠物角色与精灵图集的只读适配器。

本模块只读取 ``${CODEX_HOME:-~/.codex}`` 下的 ``config.toml`` 和自定义宠物包，
将 Codex 的全局选择解析为经过路径边界、图集版本和透明像素校验的内存快照。它不
修改 Codex 配置、不写入派生图片，也不连接 Agent Deck daemon 或硬件。Desktop App
内置资源由独立 catalog 只读发现后复用本模块的图集校验函数；``CodexPetResolver`` 只在
能够确认选择 ID 未改变时保留 last-known-good。
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Final, Self

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_deck.core.events import AgentSource
from agent_deck.core.state import AgentState, AgentStatus

CODEX_PET_CELL_SIZE: Final[tuple[int, int]] = (192, 208)
"""Codex v1/v2 精灵图集中每个固定 cell 的像素尺寸。"""

CODEX_PET_COLUMNS: Final[int] = 8
"""Codex v1/v2 精灵图集的固定列数。"""

CODEX_PET_VERSION_GEOMETRY: Final[Mapping[int, tuple[int, int, int]]] = {
    1: (1536, 1872, 9),
    2: (1536, 2288, 11),
}
"""受支持图集版本到 ``(宽, 高, 行数)`` 的稳定映射。"""


class CodexPetResolutionStatus(StrEnum):
    """描述一次 Codex 宠物选择解析的可诊断结果。

    入参：枚举值由 resolver 内部产生，也可由 API 层作为稳定字符串序列化。
    返回：字符串枚举，区分可用、陈旧回退、未选择、内置不支持和读取错误。
    错误处理：未知值由 Enum/Pydantic 按标准语义拒绝。
    副作用：无；只定义状态合同。
    """

    LOADED = "loaded"
    STALE = "stale"
    NOT_SELECTED = "not_selected"
    BUILTIN_UNSUPPORTED = "builtin_unsupported"
    UNKNOWN_VERSION = "unknown_version"
    INVALID = "invalid"
    CONFIG_ERROR = "config_error"


class CodexPetLoadError(ValueError):
    """表示自定义宠物包不满足安全路径、manifest 或图集合同。

    入参：异常消息应是适合诊断接口展示的短文本，不包含图片数据或敏感配置。
    返回：作为 ``ValueError`` 子类由 resolver 捕获并转换为 ``INVALID``。
    错误处理：调用方可单独捕获本类型；未捕获时按普通 ValueError 传播。
    副作用：无；构造异常不访问文件。
    """


class CodexPetUnknownVersionError(CodexPetLoadError):
    """表示 manifest 声明了 Agent Deck 尚不理解的精灵图版本。

    入参：异常消息通常包含未知的整数版本号。
    返回：由 resolver 转换为明确的 ``UNKNOWN_VERSION``，不猜测动作行。
    错误处理：未捕获时按 ``CodexPetLoadError`` 传播。
    副作用：无。
    """


class CodexPetManifest(BaseModel):
    """Codex 自定义宠物 ``pet.json``/``avatar.json`` 的兼容模型。

    入参：支持 Codex 使用的 camelCase 字段，``spriteVersionNumber`` 缺省时视为 v1；
    manifest 中额外的 ``kind`` 等字段会被忽略。
    返回：冻结模型，供资产加载器和诊断接口读取。
    错误处理：空 ID、空展示名、空图集路径或非正版本由 Pydantic 拒绝。
    副作用：无；模型本身不读文件。
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="ignore",
    )

    id: str
    display_name: str = Field(alias="displayName")
    description: str = ""
    spritesheet_path: str = Field(alias="spritesheetPath")
    sprite_version_number: int = Field(default=1, alias="spriteVersionNumber", gt=0)

    @field_validator("id", "display_name", "spritesheet_path")
    @classmethod
    def _require_non_empty_text(cls, value: str) -> str:
        """去除必要文本字段首尾空白并拒绝空字符串。

        入参：``value`` 是 Pydantic 已转为字符串的 ID、展示名或路径。
        返回：去除首尾空白后的字符串。
        错误处理：结果为空时抛出 ValueError，由 Pydantic 包装。
        副作用：无；不修改输入对象或访问文件。
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("pet manifest text fields must not be empty")
        return stripped


@dataclass(frozen=True, slots=True)
class CodexPetAsset:
    """一个已安全加载、可直接切 cell 的 Codex 自定义宠物图集。

    入参：字段保存选择 ID、manifest、受信目录、图集路径、RGBA 内存图、加载时间和
    非阻塞 warning；``spritesheet`` 已完整解码且透明像素 RGB 已归零。
    返回：不可重新绑定字段的数据对象；Pillow 图像仅供调用方只读裁切。
    错误处理：本类型不重复校验几何，必须由 ``load_custom_codex_pet`` 构造。
    副作用：实例化仅保存内存引用，不访问文件或硬件。
    """

    selected_avatar_id: str
    manifest: CodexPetManifest
    package_dir: Path
    manifest_path: Path
    spritesheet_path: Path
    spritesheet: Image.Image
    loaded_at: datetime
    source_fingerprint: str
    warnings: tuple[str, ...] = ()

    @property
    def sprite_version_number(self) -> int:
        """返回 manifest 中已经验证可支持的图集版本。

        入参：无；读取当前资产 manifest。
        返回：整数 1 或 2。
        错误处理：加载器已拒绝其他值，因此本属性不主动抛错。
        副作用：无。
        """

        return self.manifest.sprite_version_number

    @property
    def row_count(self) -> int:
        """返回当前图集版本的固定行数。

        入参：无；读取已验证的版本号。
        返回：v1 为 9，v2 为 11。
        错误处理：若对象绕过加载器被非法构造，字典访问抛 KeyError。
        副作用：无。
        """

        return CODEX_PET_VERSION_GEOMETRY[self.sprite_version_number][2]


@dataclass(frozen=True, slots=True)
class CodexPetResolution:
    """一次全局宠物选择解析后的完整内存结果。

    入参：``selected_avatar_id`` 是本次确认的 Codex 选择；``status`` 描述解析状态；
    ``asset`` 仅在 loaded/stale 时存在；``error`` 是短诊断；``warnings`` 不阻止展示。
    返回：冻结快照，便于 daemon 原子替换和状态接口读取。
    错误处理：字段由 resolver 保证一致，不在 dataclass 构造时重复校验。
    副作用：无；仅保存结果。
    """

    selected_avatar_id: str | None
    status: CodexPetResolutionStatus
    updated_at: datetime
    asset: CodexPetAsset | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def is_available(self) -> bool:
        """判断当前解析结果是否包含可安全渲染的资产。

        入参：无；检查当前 ``asset``。
        返回：loaded 或同 ID stale 且资产存在时为 True。
        错误处理：无。
        副作用：无。
        """

        return self.asset is not None


class PetActivity(StrEnum):
    """Codex 顶层任务归约后的全局宠物活动语义。

    入参：枚举值由 ``derive_pet_activity`` 产生并供场景控制器消费。
    返回：稳定字符串枚举 ``idle/running/needs_input/blocked/review/ready``；review 只为未来
    显式信号预留，当前状态聚合不会从普通完成状态推断它。
    错误处理：未知值由 Enum/Pydantic 拒绝。
    副作用：无。
    """

    IDLE = "idle"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    REVIEW = "review"
    READY = "ready"


class PetActivitySnapshot(BaseModel):
    """一次顶层 Codex 状态聚合得到的宠物活动快照。

    入参：``activity`` 是全局活动；``status_since`` 和 ``agent_key`` 标识动作触发源；
    ``updated_at`` 是本次聚合时间，必须带时区。
    返回：冻结 Pydantic 模型，可安全跨线程复制或序列化。
    错误处理：naive datetime 由校验器拒绝。
    副作用：无；只保存内存数据。
    """

    model_config = ConfigDict(frozen=True)

    activity: PetActivity
    status_since: datetime | None = None
    agent_key: str | None = None
    updated_at: datetime

    @field_validator("status_since", "updated_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        """拒绝宠物活动快照中的 naive datetime。

        入参：``value`` 是可空的状态进入时间或必填更新时间。
        返回：原始 timezone-aware datetime 或 None。
        错误处理：naive datetime 抛 ValueError，由 Pydantic 包装。
        副作用：无。
        """

        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("pet activity datetimes must be timezone-aware")
        return value

    @property
    def trigger_key(self) -> tuple[PetActivity, datetime | None]:
        """返回场景反应去重使用的稳定触发键。

        入参：无；读取活动和状态进入时间。
        返回：二元组；``updated_at`` 与来源 agent 不参与，只有活动或新的状态时间戳才重播。
        错误处理：无。
        副作用：无。
        """

        return (self.activity, self.status_since)


class CodexAppPetActorSnapshot(BaseModel):
    """描述 PETS 面板中的一个顶层 ChatGPT App 活动任务。

    入参：``agent_key`` 标识任务；``activity``/``status_since`` 驱动动作切换；
    ``is_remote`` 与 ``remote_host_key`` 只表达远端主机视觉分组，不包含 prompt 或凭据。
    返回：冻结快照，可安全交给多宠物场景控制器。
    错误处理：时间必须带时区；本地角色不得携带 remote host，远端角色必须携带。
    副作用：无；只保存内存状态。
    """

    model_config = ConfigDict(frozen=True)

    agent_key: str
    activity: PetActivity
    status_since: datetime
    is_remote: bool = False
    remote_host_key: str | None = None

    @field_validator("agent_key")
    @classmethod
    def _require_actor_key(cls, value: str) -> str:
        """去除角色 key 空白并拒绝空值。"""

        stripped = value.strip()
        if not stripped:
            raise ValueError("pet actor agent_key must not be empty")
        return stripped

    @field_validator("status_since")
    @classmethod
    def _require_actor_timezone(cls, value: datetime) -> datetime:
        """拒绝角色状态时间中的 naive datetime。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("pet actor status_since must be timezone-aware")
        return value

    @field_validator("remote_host_key")
    @classmethod
    def _normalize_remote_host_key(cls, value: str | None) -> str | None:
        """规范化可选远端主机 key，并拒绝空字符串。"""

        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("remote_host_key must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_remote_pair(self) -> Self:
        """保证远端标记与 host key 成对出现。"""

        if self.is_remote != (self.remote_host_key is not None):
            raise ValueError("remote pet actor must pair is_remote with remote_host_key")
        return self

    @property
    def trigger_key(self) -> tuple[PetActivity, datetime]:
        """返回该角色动作重播去重使用的稳定触发键。"""

        return (self.activity, self.status_since)


_ACTIVITY_BY_STATUS: Final[Mapping[AgentStatus, PetActivity]] = {
    AgentStatus.APPROVAL_NEEDED: PetActivity.NEEDS_INPUT,
    AgentStatus.WAITING_USER: PetActivity.NEEDS_INPUT,
    AgentStatus.ERROR: PetActivity.BLOCKED,
    AgentStatus.COMPLETED_RECENTLY: PetActivity.READY,
    AgentStatus.THINKING: PetActivity.RUNNING,
    AgentStatus.RUNNING_TOOL: PetActivity.RUNNING,
}

_ACTIVITY_PRIORITY: Final[Mapping[PetActivity, int]] = {
    PetActivity.IDLE: 0,
    PetActivity.RUNNING: 1,
    PetActivity.READY: 2,
    PetActivity.REVIEW: 3,
    PetActivity.BLOCKED: 4,
    PetActivity.NEEDS_INPUT: 5,
}

_CODEX_APP_ACTIVITY_PRIORITY: Final[Mapping[PetActivity, int]] = {
    PetActivity.IDLE: 0,
    PetActivity.READY: 1,
    PetActivity.RUNNING: 2,
    PetActivity.REVIEW: 3,
    PetActivity.BLOCKED: 4,
    PetActivity.NEEDS_INPUT: 5,
}


class CodexPetResolver:
    """只读解析 Codex 全局宠物，并管理同选择 ID 的 last-known-good。

    入参：构造时可注入环境变量与 home 目录以支持测试；未注入时读取当前进程环境。
    返回：``resolve`` 每次产生新的 ``CodexPetResolution``。
    错误处理：读取/校验失败转换为诊断状态；只有无法确认选择 ID 时不会使用旧资产。
    副作用：调用 ``resolve`` 会读取配置、manifest 和图集并更新本实例 LKG 内存。
    """

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        home_dir: Path | None = None,
    ) -> None:
        """创建一个尚未解析选择的 resolver。

        入参：``environment`` 可覆盖 ``CODEX_HOME`` 查找；``home_dir`` 替代 ``Path.home``。
        返回：无显式返回；实例可反复 ``resolve``。
        错误处理：本方法不访问文件，因此不主动抛 I/O 异常。
        副作用：复制环境 mapping，并初始化 LKG 内存。
        """

        self._environment = dict(environment) if environment is not None else None
        self._home_dir = home_dir
        self._last_good: CodexPetAsset | None = None

    def resolve(
        self,
        *,
        codex_home: Path | None = None,
        now: datetime | None = None,
    ) -> CodexPetResolution:
        """读取当前 Codex 选择并解析自定义宠物资产。

        入参：``codex_home`` 可显式覆盖目录；``now`` 用于生成确定性的诊断时间。
        返回：loaded、stale 或明确降级状态的 ``CodexPetResolution``。
        错误处理：配置无法读取时返回 CONFIG_ERROR；未知版本单独返回 UNKNOWN_VERSION；
        自定义包失败时仅在 ID 与 LKG 相同时返回 STALE。
        副作用：读取本地文件；成功时更新实例 LKG，不写文件。
        """

        resolved_at = _aware_now(now)
        root = codex_home or resolve_codex_home(
            environment=self._environment,
            home_dir=self._home_dir,
        )
        try:
            selected_id = read_selected_avatar_id(root)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            return CodexPetResolution(
                selected_avatar_id=None,
                status=CodexPetResolutionStatus.CONFIG_ERROR,
                updated_at=resolved_at,
                error=_short_error("读取 Codex 宠物选择失败", exc),
            )

        if selected_id is None:
            self._last_good = None
            return CodexPetResolution(
                selected_avatar_id=None,
                status=CodexPetResolutionStatus.NOT_SELECTED,
                updated_at=resolved_at,
                error="Codex 尚未选择宠物",
            )
        if not selected_id.startswith("custom:"):
            self._last_good = None
            return CodexPetResolution(
                selected_avatar_id=selected_id,
                status=CodexPetResolutionStatus.BUILTIN_UNSUPPORTED,
                updated_at=resolved_at,
                error="首版不解析 Codex Desktop 内置宠物资源",
            )

        unchanged_asset = self._unchanged_last_good(selected_id)
        if unchanged_asset is not None:
            return CodexPetResolution(
                selected_avatar_id=selected_id,
                status=CodexPetResolutionStatus.LOADED,
                updated_at=resolved_at,
                asset=unchanged_asset,
                warnings=unchanged_asset.warnings,
            )

        try:
            asset = load_custom_codex_pet(
                codex_home=root,
                selected_avatar_id=selected_id,
                loaded_at=resolved_at,
            )
        except CodexPetUnknownVersionError as exc:
            self._last_good = None
            return CodexPetResolution(
                selected_avatar_id=selected_id,
                status=CodexPetResolutionStatus.UNKNOWN_VERSION,
                updated_at=resolved_at,
                error=str(exc),
            )
        except (CodexPetLoadError, OSError, ValueError) as exc:
            return self._failed_custom_resolution(
                selected_id=selected_id,
                status=CodexPetResolutionStatus.INVALID,
                error=_short_error("加载 Codex 宠物失败", exc),
                resolved_at=resolved_at,
            )

        self._last_good = asset
        return CodexPetResolution(
            selected_avatar_id=selected_id,
            status=CodexPetResolutionStatus.LOADED,
            updated_at=resolved_at,
            asset=asset,
            warnings=asset.warnings,
        )

    def _unchanged_last_good(self, selected_id: str) -> CodexPetAsset | None:
        """按 manifest/图集文件指纹复用未变化的已解码资产。

        入参：``selected_id`` 是本轮已从 Codex 配置确认的 custom 选择。
        返回：选择相同且两个来源文件的 path、mtime、size、version 指纹均未变化时返回 LKG；
        否则返回 None，让完整安全解析重新验证 manifest、symlink 和图集。
        错误处理：文件消失或 stat 失败视为已变化，由后续完整加载进入 stale/invalid 语义。
        副作用：只读取两个 LKG 文件的 stat，不解码图片、不写文件。
        """

        asset = self._last_good
        if asset is None or asset.selected_avatar_id != selected_id:
            return None
        try:
            current_fingerprint = _source_fingerprint(
                selected_avatar_id=selected_id,
                manifest_path=asset.manifest_path,
                spritesheet_path=asset.spritesheet_path,
                sprite_version_number=asset.sprite_version_number,
            )
        except OSError:
            return None
        if current_fingerprint != asset.source_fingerprint:
            return None
        return asset

    def _failed_custom_resolution(
        self,
        *,
        selected_id: str,
        status: CodexPetResolutionStatus,
        error: str,
        resolved_at: datetime,
    ) -> CodexPetResolution:
        """为失败的自定义包加载应用同 ID LKG 规则。

        入参：``selected_id`` 已从本次配置确认；``status``/``error`` 是失败原因；
        ``resolved_at`` 是本次 timezone-aware 时间。
        返回：同 ID 且有 LKG 时为 STALE，否则保留原失败状态且不携带旧资产。
        错误处理：无；所有 I/O 异常已由调用者捕获。
        副作用：选择已改变时清除旧 LKG；同 ID 时保持 LKG。
        """

        if (
            self._last_good is not None
            and self._last_good.selected_avatar_id == selected_id
        ):
            return CodexPetResolution(
                selected_avatar_id=selected_id,
                status=CodexPetResolutionStatus.STALE,
                updated_at=resolved_at,
                asset=self._last_good,
                error=error,
                warnings=self._last_good.warnings,
            )
        self._last_good = None
        return CodexPetResolution(
            selected_avatar_id=selected_id,
            status=status,
            updated_at=resolved_at,
            error=error,
        )


def resolve_codex_home(
    *,
    environment: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> Path:
    """按 ``CODEX_HOME`` 后 ``~/.codex`` 的顺序定位 Codex 数据目录。

    入参：``environment`` 缺省使用 ``os.environ``；``home_dir`` 缺省使用 ``Path.home``。
    返回：展开用户目录后的 Path，不要求目录当前存在。
    错误处理：环境值无法转换为路径时按 pathlib 标准异常传播。
    副作用：可能读取进程环境，不访问文件内容。
    """

    source = os.environ if environment is None else environment
    configured = source.get("CODEX_HOME")
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    base_home = home_dir if home_dir is not None else Path.home()
    return base_home.expanduser() / ".codex"


def read_selected_avatar_id(codex_home: Path) -> str | None:
    """只读解析 Codex ``config.toml`` 的全局 Desktop 宠物选择。

    入参：``codex_home`` 是由显式参数或 ``resolve_codex_home`` 得到的目录。
    返回：去除空白后的选择 ID；字段缺失或空字符串时返回 None。
    错误处理：文件缺失/权限错误、TOML 非法或字段非字符串时抛异常。
    副作用：读取一个本地配置文件，不修改配置。
    """

    config_path = codex_home.expanduser() / "config.toml"
    with config_path.open("rb") as handle:
        parsed = tomllib.load(handle)
    selected = parsed.get("selected-avatar-id")
    if selected is None:
        desktop = parsed.get("desktop")
        if isinstance(desktop, dict):
            selected = desktop.get("selected-avatar-id")
    if selected is None:
        return None
    if not isinstance(selected, str):
        raise ValueError("selected-avatar-id 必须是字符串")
    normalized = selected.strip()
    return normalized or None


def load_custom_codex_pet(
    *,
    codex_home: Path,
    selected_avatar_id: str,
    loaded_at: datetime | None = None,
) -> CodexPetAsset:
    """加载并校验一个 ``custom:<name>`` Codex 宠物包。

    入参：``codex_home`` 是配置根；``selected_avatar_id`` 必须是安全的 custom ID；
    ``loaded_at`` 可固定资产时间。
    返回：包含规范化 RGBA 图集的 ``CodexPetAsset``。
    错误处理：目录逃逸、manifest 非法、图集逃逸/缺失、未知版本或几何不符时抛
    ``CodexPetLoadError`` 子类；Pillow 解码错误包装为 load error。
    副作用：读取 manifest 与图集到内存，不写文件。
    """

    selected_id = selected_avatar_id.strip()
    if not selected_id.startswith("custom:"):
        raise CodexPetLoadError("选择不是 custom 宠物")
    pet_name = selected_id.removeprefix("custom:")
    _validate_pet_folder_name(pet_name)
    root = codex_home.expanduser().resolve()
    package_dir, manifest_path = _find_custom_manifest(root, pet_name)

    try:
        manifest = CodexPetManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise CodexPetLoadError(f"宠物 manifest 无效: {type(exc).__name__}") from exc
    version = manifest.sprite_version_number
    if version not in CODEX_PET_VERSION_GEOMETRY:
        raise CodexPetUnknownVersionError(f"不支持 spriteVersionNumber={version}")

    spritesheet_path = _safe_spritesheet_path(
        package_dir=package_dir,
        configured_path=manifest.spritesheet_path,
    )
    try:
        with Image.open(spritesheet_path) as source:
            source.load()
            spritesheet = source.convert("RGBA")
    except Exception as exc:
        raise CodexPetLoadError(f"宠物图集无法解码: {type(exc).__name__}") from exc

    normalized, warnings = normalize_codex_pet_spritesheet(
        spritesheet,
        sprite_version_number=version,
    )
    return CodexPetAsset(
        selected_avatar_id=selected_id,
        manifest=manifest,
        package_dir=package_dir,
        manifest_path=manifest_path,
        spritesheet_path=spritesheet_path,
        spritesheet=normalized,
        loaded_at=_aware_now(loaded_at),
        source_fingerprint=_source_fingerprint(
            selected_avatar_id=selected_id,
            manifest_path=manifest_path,
            spritesheet_path=spritesheet_path,
            sprite_version_number=version,
        ),
        warnings=warnings,
    )


def normalize_codex_pet_spritesheet(
    image: Image.Image,
    *,
    sprite_version_number: int,
) -> tuple[Image.Image, tuple[str, ...]]:
    """校验并规范化一张 custom 或 Desktop 内置宠物完整图集。

    入参：``image`` 是已解码图片；``sprite_version_number`` 必须是受支持的 v1/v2 合同。
    返回：固定几何的 RGBA 副本与非阻塞 warning；不会裁 cell 或重新对齐角色。
    错误处理：未知版本抛 ``CodexPetUnknownVersionError``，几何不符抛 ``CodexPetLoadError``。
    副作用：只创建内存图像，不修改来源或写文件。
    """

    geometry = CODEX_PET_VERSION_GEOMETRY.get(sprite_version_number)
    if geometry is None:
        raise CodexPetUnknownVersionError(
            f"不支持 spriteVersionNumber={sprite_version_number}"
        )
    expected_width, expected_height, _ = geometry
    if image.size != (expected_width, expected_height):
        raise CodexPetLoadError(
            "宠物图集几何无效: "
            f"期望 {expected_width}x{expected_height}，实际 {image.width}x{image.height}"
        )
    normalized, had_residue = _normalize_transparent_rgb(image)
    warnings = (
        ("已将完全透明像素中的非零 RGB 残留归零",) if had_residue else ()
    )
    return normalized, warnings


def derive_pet_activity(
    states: Iterable[AgentState],
    *,
    updated_at: datetime | None = None,
) -> PetActivitySnapshot:
    """按官方优先级聚合顶层 Codex Agent 状态。

    入参：``states`` 是 state store 快照；``updated_at`` 可固定聚合时间。
    返回：``Needs input > Blocked > Ready > Running > Idle`` 的全局活动；同优先级
    选择 ``status_since`` 最新的顶层 Codex 状态作为触发源。
    错误处理：传入状态模型已负责时间校验；非法 ``updated_at`` 抛 ValueError。
    副作用：无；不修改状态集合。
    """

    observed_at = _aware_now(updated_at)
    candidates: list[tuple[int, datetime, AgentState, PetActivity]] = []
    for state in states:
        if state.source != AgentSource.CODEX:
            continue
        if state.is_child_agent or state.parent_agent_key is not None:
            continue
        activity = _ACTIVITY_BY_STATUS.get(state.status)
        if activity is None:
            continue
        candidates.append(
            (_ACTIVITY_PRIORITY[activity], state.status_since, state, activity)
        )
    if not candidates:
        return PetActivitySnapshot(
            activity=PetActivity.IDLE,
            updated_at=observed_at,
        )
    _, _, winner, activity = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2].agent_key),
    )
    return PetActivitySnapshot(
        activity=activity,
        status_since=winner.status_since,
        agent_key=winner.agent_key,
        updated_at=observed_at,
    )


def derive_codex_app_pet_activity(
    states: Iterable[AgentState],
    *,
    updated_at: datetime | None = None,
) -> PetActivitySnapshot:
    """聚合只属于 Codex/ChatGPT Desktop 顶层任务的 Key 覆盖活动。

    入参：``states`` 是完整 store 快照；仅消费 ``codex-app:*`` 或兼容的
    ``app:Codex``/``app:ChatGPT`` focus target，并排除 CLI、child agent 与其他来源；
    ``updated_at`` 可固定聚合时间。
    返回：``Needs input > Error > Review > Running > Completed > Idle`` 活动；当前没有显式
    review 状态来源，因此普通 ``COMPLETED_RECENTLY`` 只会返回 ``READY``。
    错误处理：非法 ``updated_at`` 抛 ValueError；状态字段由 ``AgentState`` 预先校验。
    副作用：无；不修改状态集合、不探测前台 App。
    """

    observed_at = _aware_now(updated_at)
    candidates: list[tuple[int, datetime, AgentState, PetActivity]] = []
    for state in states:
        if state.source != AgentSource.CODEX:
            continue
        if state.is_child_agent or state.parent_agent_key is not None:
            continue
        focus_target = state.focus_target or ""
        if not (
            focus_target.startswith("codex-app:")
            or focus_target in {"app:Codex", "app:ChatGPT"}
        ):
            continue
        activity = _ACTIVITY_BY_STATUS.get(state.status)
        if activity is None:
            continue
        candidates.append(
            (
                _CODEX_APP_ACTIVITY_PRIORITY[activity],
                state.status_since,
                state,
                activity,
            )
        )
    if not candidates:
        return PetActivitySnapshot(
            activity=PetActivity.IDLE,
            updated_at=observed_at,
        )
    _, _, winner, activity = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2].agent_key),
    )
    return PetActivitySnapshot(
        activity=activity,
        status_since=winner.status_since,
        agent_key=winner.agent_key,
        updated_at=observed_at,
    )


def derive_codex_app_pet_actors(
    states: Iterable[AgentState],
) -> tuple[CodexAppPetActorSnapshot, ...]:
    """提取 PETS 面板应独立绘制的顶层 ChatGPT App 活动任务。

    入参：``states`` 是完整 store 快照；只消费 ``codex-app:*`` 顶层 Codex 状态。
    返回：按介入优先级、状态新鲜度和 agent key 排序的不可变角色快照；idle/offline、CLI、
    child agent 与普通 App 被排除。``remote-ssh`` target 会提取不含线程 ID 的稳定 host key。
    错误处理：状态模型已校验时间；无法识别 host 的 target 按本地任务处理。
    副作用：无；不修改状态、不连接远端。
    """

    actors: list[CodexAppPetActorSnapshot] = []
    for state in states:
        if state.source != AgentSource.CODEX:
            continue
        if state.is_child_agent or state.parent_agent_key is not None:
            continue
        focus_target = state.focus_target or ""
        if not (
            focus_target.startswith("codex-app:")
            or focus_target in {"app:Codex", "app:ChatGPT"}
        ):
            continue
        activity = _ACTIVITY_BY_STATUS.get(state.status)
        if activity is None or activity == PetActivity.IDLE:
            continue
        remote_host_key = _remote_host_key_from_focus_target(focus_target)
        actors.append(
            CodexAppPetActorSnapshot(
                agent_key=state.agent_key,
                activity=activity,
                status_since=state.status_since,
                is_remote=remote_host_key is not None,
                remote_host_key=remote_host_key,
            )
        )
    actors.sort(
        key=lambda actor: (
            -_CODEX_APP_ACTIVITY_PRIORITY[actor.activity],
            -actor.status_since.timestamp(),
            actor.agent_key,
        )
    )
    return tuple(actors)


def _remote_host_key_from_focus_target(focus_target: str) -> str | None:
    """从 remote-ssh App target 中提取不含线程 ID 的稳定主机 key。

    入参：``focus_target`` 是 state scanner 产生的只读 focus target。
    返回：observer 使用的 ``host-id``；本地、兼容 App target 或结构不完整时返回 None。
    错误处理：无；未知远端协议不猜测。
    副作用：无。
    """

    prefix = "codex-app:remote-ssh:"
    if not focus_target.startswith(prefix):
        return None
    remainder = focus_target.removeprefix(prefix)
    host_id, separator, _thread_id = remainder.partition(":")
    if not separator or not host_id:
        return None
    return host_id


def _validate_pet_folder_name(pet_name: str) -> None:
    """拒绝可能越过 ``pets/<name>`` 边界的选择名称。

    入参：``pet_name`` 是移除 ``custom:`` 后的原始片段。
    返回：名称安全时无返回值。
    错误处理：空值、点目录、路径分隔符或 Windows 绝对路径抛 load error。
    副作用：无。
    """

    if (
        not pet_name
        or pet_name in {".", ".."}
        or Path(pet_name).name != pet_name
        or PureWindowsPath(pet_name).is_absolute()
        or "/" in pet_name
        or "\\" in pet_name
    ):
        raise CodexPetLoadError("custom 宠物名称包含不安全路径")


def _find_custom_manifest(codex_home: Path, pet_name: str) -> tuple[Path, Path]:
    """按 current pets 后 legacy avatars 的顺序定位 manifest。

    入参：``codex_home`` 是已 resolve 的 Codex 根，``pet_name`` 已通过名称校验。
    返回：经过 symlink containment 校验的 ``(package_dir, manifest_path)``。
    错误处理：两种目录都不存在或任一命中路径逃逸时抛 load error。
    副作用：读取文件系统元数据，不读取文件内容。
    """

    choices = (("pets", "pet.json"), ("avatars", "avatar.json"))
    for family, filename in choices:
        family_root = codex_home / family
        manifest_candidate = family_root / pet_name / filename
        if not manifest_candidate.is_file():
            continue
        resolved_family = family_root.resolve()
        if not resolved_family.is_relative_to(codex_home):
            raise CodexPetLoadError(f"{family} 目录通过 symlink 越过 CODEX_HOME")
        package_dir = manifest_candidate.parent.resolve()
        manifest_path = manifest_candidate.resolve()
        if not package_dir.is_relative_to(resolved_family):
            raise CodexPetLoadError("宠物目录通过 symlink 越过受信根目录")
        if not manifest_path.is_relative_to(package_dir):
            raise CodexPetLoadError("宠物 manifest 通过 symlink 越过宠物目录")
        return package_dir, manifest_path
    raise CodexPetLoadError("未找到 pets/<name>/pet.json 或 avatars/<name>/avatar.json")


def _safe_spritesheet_path(*, package_dir: Path, configured_path: str) -> Path:
    """将 manifest 图集相对路径安全解析在宠物目录内。

    入参：``package_dir`` 是已校验目录；``configured_path`` 来自 manifest。
    返回：resolve 后且存在的普通文件路径。
    错误处理：绝对路径、``..``、Windows drive、symlink escape 或缺失文件抛 load error。
    副作用：读取路径元数据，不打开图片。
    """

    relative = Path(configured_path)
    windows = PureWindowsPath(configured_path)
    if (
        relative.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in relative.parts
    ):
        raise CodexPetLoadError("spritesheetPath 必须是宠物目录内的安全相对路径")
    resolved = (package_dir / relative).resolve()
    if not resolved.is_relative_to(package_dir):
        raise CodexPetLoadError("spritesheetPath 通过路径或 symlink 越过宠物目录")
    if not resolved.is_file():
        raise CodexPetLoadError("spritesheetPath 指向的文件不存在")
    return resolved


def _normalize_transparent_rgb(image: Image.Image) -> tuple[Image.Image, bool]:
    """清除 alpha=0 像素的隐藏 RGB，保留所有 cell 固定坐标。

    入参：``image`` 是完整解码的 RGBA 图集。
    返回：``(规范化副本, 是否发现残留)``；不会裁透明边或重新居中。
    错误处理：Pillow 通道操作异常按原异常传播。
    副作用：只创建内存图像，不修改调用方图像或写文件。
    """

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    transparent_mask = alpha.point(lambda value: 255 if value == 0 else 0)
    red, green, blue, _ = rgba.split()
    had_residue = any(
        ImageChops.multiply(channel, transparent_mask).getextrema()[1] > 0
        for channel in (red, green, blue)
    )
    if not had_residue:
        return rgba.copy(), False
    normalized = rgba.copy()
    normalized.paste((0, 0, 0, 0), (0, 0, *normalized.size), transparent_mask)
    return normalized, True


def _source_fingerprint(
    *,
    selected_avatar_id: str,
    manifest_path: Path,
    spritesheet_path: Path,
    sprite_version_number: int,
) -> str:
    """生成足以跳过无变化预渲染的稳定来源指纹。

    入参：选择 ID、两个已 resolve 文件路径及受支持版本号；文件 stat 提供 mtime_ns/size。
    返回：SHA-256 十六进制摘要，不在 API 中暴露完整本机路径。
    错误处理：文件在加载期间消失或无法 stat 时按 OSError 传播，使 resolver 进入失败路径。
    副作用：读取两个文件的元数据，不读取额外内容或写文件。
    """

    manifest_stat = manifest_path.stat()
    spritesheet_stat = spritesheet_path.stat()
    components = (
        selected_avatar_id,
        str(manifest_path),
        str(manifest_stat.st_mtime_ns),
        str(manifest_stat.st_size),
        str(spritesheet_path),
        str(spritesheet_stat.st_mtime_ns),
        str(spritesheet_stat.st_size),
        str(sprite_version_number),
    )
    return hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()


def _aware_now(value: datetime | None) -> datetime:
    """返回 timezone-aware 的当前或注入时间。

    入参：``value`` 可为空；为空时使用当前 UTC。
    返回：timezone-aware datetime，不做时区转换。
    错误处理：naive datetime 抛 ValueError。
    副作用：未注入时读取系统时钟。
    """

    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("pet timestamps must be timezone-aware")
    return result


def _short_error(prefix: str, exc: BaseException, *, limit: int = 240) -> str:
    """把本地读取异常压缩为不泄露图片内容的短诊断。

    入参：``prefix`` 是稳定中文上下文；``exc`` 是已捕获异常；``limit`` 是最大字符数。
    返回：包含异常类型和单行消息的截断文本。
    错误处理：异常字符串化失败时按 Python 标准语义传播。
    副作用：无。
    """

    detail = " ".join(str(exc).split())
    combined = f"{prefix}: {detail or type(exc).__name__}"
    return combined[:limit]
