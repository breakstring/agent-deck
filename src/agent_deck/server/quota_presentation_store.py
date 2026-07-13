"""Codex quota 展示策略的 JSON 持久化与纯内存归约。

本模块把 app-server 归一后的原始 quota 快照映射为硬件展示快照。规则只控制某个
`limit_id` 的可见性、排序和短标签，绝不修改原始快照、调用 Codex 或访问硬件。未匹配到
规则的新 limit 默认保留展示，避免 Codex 增加新的限制后被静默隐藏。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agent_deck.adapters.codex_quota import CodexQuotaSnapshot, CodexQuotaWindow

_QUOTA_PRESENTATION_ENV = "AGENT_DECK_QUOTA_PRESENTATION"
_USER_QUOTA_PRESENTATION_PATH = (
    Path.home() / "Library/Application Support/AgentDeck/quota-presentation.json"
)
_STORE_VERSION = 1


class QuotaPresentationStoreError(ValueError):
    """表示 quota 展示策略文件无法读取、校验或写入。

    入参：标准 `ValueError` 参数，通常是可写入 daemon status 的中文错误说明。
    返回：异常实例。
    错误处理：调用方可保留默认策略并报告该错误，不应清空已有 quota 数据。
    副作用：异常对象本身不访问文件或硬件。
    """


class QuotaPresentationRule(BaseModel):
    """描述一个按 Codex limit_id 匹配的 quota 展示规则。

    入参：`limit_id` 是 app-server 归一后的稳定 limit 标识；`label` 是可选短标签；
    `visible` 控制该 limit 的所有窗口是否进入硬件展示；`order` 越小越靠前。
    返回：不可变规则模型。
    错误处理：空标识、空标签或超长标签由 Pydantic 拒绝。
    副作用：无；模型不读写文件或外部状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_id: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=16)
    visible: bool = True
    order: int = 0

    @field_validator("limit_id", "label")
    @classmethod
    def _strip_required_text(cls, value: str | None) -> str | None:
        """清理规则中用户写入的文本，并拒绝空白标识或空白标签。

        入参：`value` 是 `limit_id` 或可选 `label`。
        返回：去除首尾空白后的字符串，None 保持不变。
        错误处理：字符串清理后为空时抛 `ValueError`。
        副作用：无。
        """

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("展示规则文本不能为空")
        return normalized


class QuotaPresentation(BaseModel):
    """定义 quota 的可见性、排序和短标签策略。

    入参：`rules` 是按 `limit_id` 匹配的规则集合；`unmatched_visible` 控制未来新 limit
    的默认可见性，默认 True。
    返回：不可变策略模型。
    错误处理：同一个 limit_id 出现多条规则会被拒绝，避免配置顺序产生歧义。
    副作用：无；只保存配置值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[QuotaPresentationRule, ...] = ()
    unmatched_visible: bool = True

    @model_validator(mode="after")
    def _require_unique_limit_rules(self) -> "QuotaPresentation":
        """确保每个 limit_id 只有一条展示规则。

        入参：无；读取当前模型的 `rules`。
        返回：当前已校验模型。
        错误处理：重复 limit_id 时抛 `ValueError`。
        副作用：无。
        """

        ids = tuple(rule.limit_id for rule in self.rules)
        if len(ids) != len(set(ids)):
            raise ValueError("quota 展示规则中的 limit_id 不能重复")
        return self

    def present(self, snapshot: CodexQuotaSnapshot) -> "QuotaPresentationResult":
        """将原始 quota 快照按当前策略转换为展示集合。

        入参：`snapshot` 是 adapter 输出的完整原始 quota 快照。
        返回：包含原始数量、可见窗口和可选展示快照的结果。
        错误处理：快照模型与规则均已校验；本方法不抛 I/O 错误。
        副作用：无；通过 model copy 创建展示窗口，不会改写原始快照。
        """

        by_limit_id = {rule.limit_id: rule for rule in self.rules}
        ordered: list[tuple[tuple[int, int, int], CodexQuotaWindow]] = []
        for source_index, window in enumerate(snapshot.available_windows()):
            rule = by_limit_id.get(window.limit_id)
            if rule is not None and not rule.visible:
                continue
            if rule is None and not self.unmatched_visible:
                continue
            label = rule.label if rule is not None else _default_presentation_label(window)
            rank = (
                0 if rule is not None else 1,
                rule.order if rule is not None else source_index,
                source_index,
            )
            ordered.append(
                (rank, window.model_copy(update={"presentation_label": label}))
            )
        ordered.sort(key=lambda item: item[0])
        return QuotaPresentationResult(
            source_window_count=len(snapshot.windows),
            windows=tuple(item[1] for item in ordered),
            snapshot=snapshot,
        )


@dataclass(frozen=True)
class QuotaPresentationResult:
    """一次 quota 展示策略归约的结果。

    入参：`source_window_count` 是原始窗口数量；`windows` 是允许展示的窗口；`snapshot`
    保留原始计划与 credits 元数据以便生成展示快照。
    返回：不可变结果对象。
    错误处理：无；空 `windows` 合法，表示用户已隐藏当前所有限额。
    副作用：无。
    """

    source_window_count: int
    windows: tuple[CodexQuotaWindow, ...]
    snapshot: CodexQuotaSnapshot

    def display_snapshot(self) -> CodexQuotaSnapshot | None:
        """返回只含可见窗口的 quota 快照，或在全隐藏时返回 None。

        入参：无。
        返回：有可见窗口时返回复制后的 `CodexQuotaSnapshot`，否则返回 None。
        错误处理：无；原快照已保证自身字段有效。
        副作用：无；不修改原始快照。
        """

        if not self.windows:
            return None
        return self.snapshot.model_copy(update={"windows": self.windows, "raw": {}})


def resolve_quota_presentation_path(path: Path | None = None) -> Path:
    """解析 quota 展示策略的稳定持久化路径。

    入参：`path` 是调用方显式覆盖；为空时先读环境变量，再回退用户级 Application Support。
    返回：展开用户目录后的路径；路径尚不存在也合法。
    错误处理：不因路径不存在抛错。
    副作用：只读取环境变量，不写文件、不访问硬件。
    """

    if path is not None:
        return path.expanduser()
    env_value = os.environ.get(_QUOTA_PRESENTATION_ENV)
    if env_value:
        return Path(env_value).expanduser()
    return _USER_QUOTA_PRESENTATION_PATH


def load_quota_presentation(path: Path) -> QuotaPresentation | None:
    """从版本化 JSON 文件读取 quota 展示策略。

    入参：`path` 是策略 JSON envelope 路径。
    返回：文件不存在时返回 None；文件有效时返回不可变策略。
    错误处理：读取、JSON 或 Pydantic 校验失败时抛 `QuotaPresentationStoreError`。
    副作用：只读取指定文件，不写文件、不访问网络或硬件。
    """

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QuotaPresentationStoreError(f"无法读取 quota 展示策略文件 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QuotaPresentationStoreError(
            f"quota 展示策略文件 {path} 不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise QuotaPresentationStoreError(f"quota 展示策略文件 {path} 顶层必须是 object")
    if data.get("version") != _STORE_VERSION:
        raise QuotaPresentationStoreError(
            f"quota 展示策略文件 {path} 的 version 不支持: {data.get('version')!r}"
        )
    presentation_data = data.get("presentation")
    if not isinstance(presentation_data, dict):
        raise QuotaPresentationStoreError(f"quota 展示策略文件 {path} 缺少 presentation object")
    try:
        return QuotaPresentation.model_validate(presentation_data)
    except ValidationError as exc:
        raise QuotaPresentationStoreError(
            f"quota 展示策略文件 {path} 校验失败: {exc}"
        ) from exc


def save_quota_presentation(presentation: QuotaPresentation, path: Path) -> None:
    """将 quota 展示策略以原子 replace 方式写入 JSON 文件。

    入参：`presentation` 是已校验策略；`path` 是目标 envelope 路径。
    返回：无显式返回值。
    错误处理：目录创建、临时文件写入或 replace 失败时抛 `QuotaPresentationStoreError`。
    副作用：创建父目录，写入同目录临时文件，并更新目标文件。
    """

    envelope: dict[str, Any] = {
        "version": _STORE_VERSION,
        "presentation": presentation.model_dump(mode="json"),
    }
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)
    except OSError as exc:
        raise QuotaPresentationStoreError(
            f"无法写入 quota 展示策略文件 {path}: {exc}"
        ) from exc


def _default_presentation_label(window: CodexQuotaWindow) -> str | None:
    """为未配置规则的窗口生成紧凑且稳定的默认身份标签。

    入参：`window` 是一条原始 quota 窗口。
    返回：主 Codex limit 为 `Codex`；有名称的附加 limit 优先取末段；无名称时返回 None。
    错误处理：名称格式异常时安全回退为 `Limit`。
    副作用：无。
    """

    if window.limit_id == "codex" and not window.limit_name:
        return "Codex"
    if not window.limit_name:
        return None
    segments = tuple(
        part.strip() for part in window.limit_name.replace("_", "-").split("-") if part.strip()
    )
    return segments[-1][:16] if segments else "Limit"
