"""Codex 宠物在 daemon 内的线程安全展示协调器。

本模块组合只读资产 resolver、顶层 Agent 状态聚合、绝对时间场景控制器和临时 Key
缓存，为 FastAPI runtime 与现有 N4 Pro persistent animator 提供原子快照。它不创建
HID 会话、不启动刷新线程、不修改 Codex 配置，也不持久化宠物选择；调用方负责安排
资产轮询、把返回 revision 合并进全局 surface，并在退出时清理由其创建的临时目录。
"""

from __future__ import annotations

import platform
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Final

from PIL import Image

from agent_deck.adapters.codex_pet import (
    CodexPetResolution,
    CodexPetResolver,
    PetActivitySnapshot,
    derive_codex_app_pet_activity,
    derive_pet_activity,
)
from agent_deck.config import CodexPetMotion
from agent_deck.core.state import AgentState
from agent_deck.rendering.codex_pet import (
    CodexAppPetOverlayController,
    PetSceneController,
    PetSceneSample,
    pet_key_frame_for_sample,
    pre_render_pet_key_frames,
    render_pet_panel_image,
)
from agent_deck.rendering.n4pro_panel import compose_n4pro_background

ReducedMotionReader = Callable[[], bool]
"""返回 macOS 当前是否启用 Reduce Motion 的可注入只读函数。"""

CODEX_APP_PET_KEY_WRITE_BUDGET_PER_SECOND: Final[int] = 10
"""所有关联 App Key 共享的默认图片写入预算。"""

CODEX_APP_PET_KEY_MIN_DYNAMIC_FPS: Final[int] = 5
"""一个被选为动态展示的 App Key 可接受的最低目标帧率。"""


class CodexPetRuntime:
    """协调 daemon 中的宠物解析、活动、缓存、采样和诊断。

    入参：``enabled``、``panel_fps`` 与 ``motion`` 来自 ``[codex.pet]``；
    ``cache_root`` 是 daemon 生命周期临时目录；``fallback_key_path`` 是内置 Codex 静态图；
    resolver 与 reduced-motion reader 可在测试中替换。
    返回：实例方法提供线程安全的 Key Path、PETS 背景和 JSON-safe 诊断。
    错误处理：可预期的解析失败保存在 resolution；预渲染或系统偏好读取失败进入短诊断，
    不中断 daemon。
    副作用：启用时可能读取 macOS 偏好；``refresh`` 读取 Codex 文件并在临时目录写派生 PNG。
    """

    def __init__(
        self,
        *,
        enabled: bool,
        panel_fps: int,
        motion: CodexPetMotion,
        cache_root: Path,
        fallback_key_path: Path | None,
        resolver: CodexPetResolver | None = None,
        reduced_motion_reader: ReducedMotionReader | None = None,
        started_at_monotonic: float | None = None,
    ) -> None:
        """创建尚未解析资产、但可立即输出 idle 诊断的宠物 runtime。

        入参：见类说明；``started_at_monotonic`` 用于确定性测试。
        返回：无显式返回。
        错误处理：FPS 非正时抛 ValueError；强制 full/reduced 不读取系统偏好；auto 读取失败
        会降级 full 并保留诊断。
        副作用：可能调用一次只读 reduced-motion reader，不加载宠物图集。
        """

        if panel_fps <= 0:
            raise ValueError("codex pet panel fps must be positive")
        self._enabled = bool(enabled)
        self._panel_fps = panel_fps
        self._motion = motion
        self._cache_root = cache_root
        self._fallback_key_path = fallback_key_path
        self._resolver = resolver or CodexPetResolver()
        reduced_motion, effective_motion, motion_error = resolve_codex_pet_motion(
            motion,
            reader=reduced_motion_reader,
            enabled=self._enabled,
        )
        self._effective_motion = effective_motion
        self._motion_error = motion_error
        self._controller = PetSceneController(
            reduced_motion=reduced_motion,
            started_at_monotonic=started_at_monotonic,
        )
        self._app_overlay_controller = CodexAppPetOverlayController(
            reduced_motion=reduced_motion,
            started_at_monotonic=started_at_monotonic,
        )
        self._lock = RLock()
        self._resolution: CodexPetResolution | None = None
        self._activity = derive_pet_activity(())
        self._app_activity = derive_codex_app_pet_activity(())
        self._key_frames: dict[str, tuple[Path, ...]] = {}
        self._key_asset_fingerprint: str | None = None
        self._last_error: str | None = None
        self._updated_at: datetime | None = None
        self._panel_revision = 0
        self._panel_image: Image.Image | None = None
        self._panel_visual_key: tuple[object, ...] | None = None
        self._panel_semantic_key: tuple[object, ...] | None = None
        self._panel_render_bucket: int | None = None
        self._app_overlay_key_last_pressed: dict[int, float] = {}
        self._app_overlay_dynamic_cache_key: tuple[object, ...] | None = None
        self._app_overlay_dynamic_source: Path | None = None
        self._app_overlay_diagnostics: dict[str, Any] = {
            "activity": self._app_activity.activity.value,
            "linked_key_count": 0,
            "visible_key_count": 0,
            "animated_key_count": 0,
            "static_fallback_key_count": 0,
            "effective_fps": 0.0,
            "write_budget_per_second": CODEX_APP_PET_KEY_WRITE_BUDGET_PER_SECOND,
            "minimum_dynamic_fps": CODEX_APP_PET_KEY_MIN_DYNAMIC_FPS,
        }

    @property
    def enabled(self) -> bool:
        """返回 Pets 是否进入人工面板轮换。

        入参：无。
        返回：配置开关布尔值。
        错误处理：无。
        副作用：无。
        """

        return self._enabled

    def refresh(self, *, now: datetime | None = None) -> CodexPetResolution | None:
        """重新读取 Codex 全局选择，并按素材指纹更新临时 Key 缓存。

        入参：``now`` 可固定 resolver 时间；功能关闭时忽略。
        返回：最新 resolution；关闭时返回 None。
        错误处理：resolver 的已建模错误直接返回；Key 预渲染失败时保留新 resolution、清除
        不匹配缓存并写短诊断，使 Key 回退静态 Codex 图而不是冒充旧选择。
        副作用：读取 Codex 配置/manifest/图集；新指纹会在 ``cache_root`` 写派生 PNG。
        """

        if not self._enabled:
            return None
        resolved_at = now or datetime.now(UTC)
        try:
            resolution = self._resolver.resolve(now=resolved_at)
        except Exception as exc:  # noqa: BLE001 - 第三方图片解码不能终止 daemon。
            self.mark_refresh_error(exc, updated_at=resolved_at)
            return None

        new_frames: dict[str, tuple[Path, ...]] | None = None
        cache_error: str | None = None
        asset = resolution.asset
        with self._lock:
            cached_fingerprint = self._key_asset_fingerprint
        if asset is not None and asset.source_fingerprint != cached_fingerprint:
            try:
                rendered = pre_render_pet_key_frames(
                    asset,
                    cache_dir=self._cache_root / asset.source_fingerprint[:20],
                )
                new_frames = {str(action): paths for action, paths in rendered.items()}
            except Exception as exc:  # noqa: BLE001 - Key 可安全回退静态图。
                cache_error = _short_error("预渲染宠物 Key 失败", exc)

        with self._lock:
            selection_changed = (
                self._resolution is not None
                and self._resolution.selected_avatar_id
                != resolution.selected_avatar_id
            )
            fingerprint_changed = (
                asset is not None
                and asset.source_fingerprint != self._key_asset_fingerprint
            )
            self._resolution = resolution
            self._updated_at = resolution.updated_at
            self._last_error = cache_error
            if asset is None:
                self._key_frames = {}
                self._key_asset_fingerprint = None
            elif new_frames is not None:
                self._key_frames = new_frames
                self._key_asset_fingerprint = asset.source_fingerprint
            elif cache_error is not None and (selection_changed or fingerprint_changed):
                self._key_frames = {}
                self._key_asset_fingerprint = None
            return resolution

    def mark_refresh_error(self, error: Exception, *, updated_at: datetime) -> None:
        """记录 resolver 未建模异常但不清除同选择的最近成功画面。

        入参：``error`` 是意外读取/解码异常；``updated_at`` 必须 timezone-aware。
        返回：无。
        错误处理：时间字段不在本层重复校验；异常文本被截断。
        副作用：更新内存诊断；不修改 Codex 文件或硬件。
        """

        with self._lock:
            self._updated_at = updated_at
            self._last_error = _short_error("刷新 Codex 宠物失败", error)

    def update_activity(
        self,
        states: Iterable[AgentState],
        *,
        updated_at: datetime | None = None,
    ) -> PetActivitySnapshot:
        """同时更新全局宠物活动和 Desktop App Key 专属活动。

        入参：``states`` 是 store 快照；``updated_at`` 可固定聚合时间。
        返回：兼容现有调用方的全局不可变活动快照；App 专属快照保存在 runtime 内部。
        错误处理：时间非法由核心聚合器抛出。
        副作用：原子替换两类内存活动并更新 App 诊断 activity；相同 trigger 不会重播反应。
        """

        state_snapshot = tuple(states)
        snapshot = derive_pet_activity(state_snapshot, updated_at=updated_at)
        app_snapshot = derive_codex_app_pet_activity(
            state_snapshot,
            updated_at=updated_at,
        )
        with self._lock:
            self._activity = snapshot
            self._app_activity = app_snapshot
            self._app_overlay_diagnostics = {
                **self._app_overlay_diagnostics,
                "activity": app_snapshot.activity.value,
            }
        return snapshot

    def record_app_overlay_key_press(
        self,
        key_index: int,
        *,
        monotonic_seconds: float | None = None,
    ) -> None:
        """记录关联 App Key 最近一次按下时间，供超预算动态槽排序。

        入参：``key_index`` 是 1-based 物理主键编号；``monotonic_seconds`` 可固定测试时钟。
        返回：无显式返回。
        错误处理：非正键号、负时间或非有限时间抛 ValueError。
        副作用：更新内存最近按下表；不改变按键动作、不确认任务提醒、不访问硬件。
        """

        if key_index <= 0:
            raise ValueError("codex app overlay key index must be positive")
        pressed_at = time.monotonic() if monotonic_seconds is None else monotonic_seconds
        if not isinstance(pressed_at, (int, float)) or pressed_at < 0:
            raise ValueError("codex app overlay key press time must be non-negative")
        if pressed_at == float("inf") or pressed_at != pressed_at:
            raise ValueError("codex app overlay key press time must be finite")
        with self._lock:
            self._app_overlay_key_last_pressed[key_index] = float(pressed_at)

    def app_overlay_key_sources(
        self,
        key_indexes: Iterable[int],
        *,
        monotonic_seconds: float | None = None,
    ) -> dict[int, Path]:
        """按任务状态与共享写屏预算返回 App Key 宠物覆盖图来源。

        入参：``key_indexes`` 是已通过 binding 校验的 1-based 关联键；可注入 monotonic 时间。
        返回：应覆盖基础 App 图的 ``key -> 预渲染 Path`` 新映射；任务不可见、全局关闭或素材
        不可用时返回空映射，使调用方保留原图标。
        错误处理：显式时间倒退由覆盖控制器抛 ValueError；缺动作帧安全回退空映射。
        副作用：推进一次覆盖采样，更新共享动态 bucket 缓存和 JSON-safe 调度诊断；不写图片或 HID。
        """

        indexes = tuple(sorted({index for index in key_indexes if index > 0}))
        with self._lock:
            sampled_at = time.monotonic() if monotonic_seconds is None else monotonic_seconds
            sample = self._app_overlay_controller.sample(
                self._app_activity,
                monotonic_seconds=sampled_at,
            )
            paths = self._key_frames.get(str(sample.action), ()) if sample.action else ()
            available = self._enabled and sample.visible and bool(paths)
            if not indexes or not available:
                self._app_overlay_dynamic_cache_key = None
                self._app_overlay_dynamic_source = None
                self._update_app_overlay_diagnostics(
                    activity=sample.activity.value,
                    linked_key_count=len(indexes),
                )
                return {}

            dynamic_capacity = (
                min(
                    len(indexes),
                    CODEX_APP_PET_KEY_WRITE_BUDGET_PER_SECOND
                    // CODEX_APP_PET_KEY_MIN_DYNAMIC_FPS,
                )
                if sample.animated
                else 0
            )
            ranked_indexes = sorted(
                indexes,
                key=lambda index: (
                    -self._app_overlay_key_last_pressed.get(index, -1.0),
                    index,
                ),
            )
            dynamic_indexes = frozenset(ranked_indexes[:dynamic_capacity])
            effective_fps = (
                CODEX_APP_PET_KEY_WRITE_BUDGET_PER_SECOND / len(dynamic_indexes)
                if dynamic_indexes
                else 0.0
            )
            sources: dict[int, Path] = {}
            if dynamic_indexes:
                render_bucket = int(sampled_at * effective_fps)
                cache_key = (
                    self._key_asset_fingerprint,
                    self._app_activity.trigger_key,
                    sample.action,
                    tuple(sorted(dynamic_indexes)),
                    effective_fps,
                    render_bucket,
                )
                if cache_key != self._app_overlay_dynamic_cache_key:
                    self._app_overlay_dynamic_source = paths[
                        (sample.frame_index or 0) % len(paths)
                    ]
                    self._app_overlay_dynamic_cache_key = cache_key
                if self._app_overlay_dynamic_source is not None:
                    for index in dynamic_indexes:
                        sources[index] = self._app_overlay_dynamic_source

            static_frame_index = (
                sample.frame_index
                if sample.completion_feedback and not sample.animated
                else 0
            ) or 0
            static_source = paths[static_frame_index % len(paths)]
            for index in indexes:
                sources.setdefault(index, static_source)
            self._update_app_overlay_diagnostics(
                activity=sample.activity.value,
                linked_key_count=len(indexes),
                visible_key_count=len(sources),
                animated_key_count=len(dynamic_indexes),
                static_fallback_key_count=len(sources) - len(dynamic_indexes),
                effective_fps=effective_fps,
            )
            return sources

    def _update_app_overlay_diagnostics(
        self,
        *,
        activity: str,
        linked_key_count: int,
        visible_key_count: int = 0,
        animated_key_count: int = 0,
        static_fallback_key_count: int = 0,
        effective_fps: float = 0.0,
    ) -> None:
        """在持锁区内原子替换 App Key 覆盖层调度诊断。

        入参：当前活动、关联/可见/动态/降级计数和单动态键有效目标 FPS。
        返回：无显式返回。
        错误处理：调用方负责提供非负计数，本方法不重复校验。
        副作用：替换内存诊断 dict；不采样状态、不访问文件或硬件。
        """

        self._app_overlay_diagnostics = {
            "activity": activity,
            "linked_key_count": linked_key_count,
            "visible_key_count": visible_key_count,
            "animated_key_count": animated_key_count,
            "static_fallback_key_count": static_fallback_key_count,
            "effective_fps": effective_fps,
            "write_budget_per_second": CODEX_APP_PET_KEY_WRITE_BUDGET_PER_SECOND,
            "minimum_dynamic_fps": CODEX_APP_PET_KEY_MIN_DYNAMIC_FPS,
        }

    def sample(self, *, monotonic_seconds: float | None = None) -> PetSceneSample:
        """线程安全地采样当前活动对应的绝对时间场景。

        入参：``monotonic_seconds`` 可用于测试；为空时在取得锁后读取系统 monotonic，避免
        两线程先读时间、后逆序加锁造成假倒退。
        返回：当前动作、帧、位置和方向。
        错误处理：显式时间倒退由控制器抛 ValueError。
        副作用：更新控制器最近采样时间与新 trigger 锚点。
        """

        with self._lock:
            sampled_at = time.monotonic() if monotonic_seconds is None else monotonic_seconds
            return self._controller.sample(
                self._activity,
                monotonic_seconds=sampled_at,
            )

    def key_image_source(
        self,
        *,
        monotonic_seconds: float | None = None,
    ) -> tuple[Path | None, tuple[object, ...]]:
        """返回当前宠物 Key 应复用的预渲染 Path 与视觉 revision key。

        入参：``monotonic_seconds`` 可固定动画采样时间。
        返回：``(Path 或 None, visual key)``；无可用自定义素材时尽力返回静态 Codex fallback。
        错误处理：缓存缺帧时降级 fallback，不抛硬件异常。
        副作用：推进一次场景采样；不编码图片、不写文件。
        """

        sample = self.sample(monotonic_seconds=monotonic_seconds)
        action, frame_index = pet_key_frame_for_sample(sample)
        with self._lock:
            paths = self._key_frames.get(str(action), ())
            if paths:
                path = paths[frame_index % len(paths)]
                return path, (
                    "custom",
                    self._key_asset_fingerprint,
                    sample.activity.value,
                    action,
                    frame_index,
                )
            fallback = self._fallback_key_path
            if fallback is not None and fallback.is_file():
                return fallback, ("fallback", str(fallback))
            return None, ("missing", self._enabled)

    def panel_background(
        self,
        *,
        monotonic_seconds: float | None = None,
    ) -> tuple[int, Image.Image | None]:
        """按最高 panel FPS 返回当前 PETS 的完整 800x480 背景。

        入参：``monotonic_seconds`` 可固定采样时钟；为空时在锁内读取 monotonic。
        返回：内部单调 revision 和完整背景；无自定义 asset 时 image 为 None，调用方应显示
        简短诊断面板。
        错误处理：Pillow 渲染异常按原异常传播，由 daemon renderer 诊断保护。
        副作用：新视觉 bucket 到期时创建一张内存图片；不访问 HID 或写磁盘。
        """

        with self._lock:
            sampled_at = time.monotonic() if monotonic_seconds is None else monotonic_seconds
            sample = self._controller.sample(
                self._activity,
                monotonic_seconds=sampled_at,
            )
            resolution = self._resolution
            asset = resolution.asset if resolution is not None else None
            if asset is None:
                return self._panel_revision, None
            visual_key = (
                asset.source_fingerprint,
                sample.action,
                sample.frame_index,
                sample.x_bucket(800),
                self._effective_motion,
            )
            semantic_key = (
                asset.source_fingerprint,
                sample.activity,
                sample.reaction_active,
            )
            if visual_key == self._panel_visual_key and self._panel_image is not None:
                return self._panel_revision, self._panel_image
            render_bucket = int(sampled_at * self._panel_fps)
            rate_limited = (
                render_bucket == self._panel_render_bucket
                and semantic_key == self._panel_semantic_key
            )
            if rate_limited and self._panel_image is not None:
                return self._panel_revision, self._panel_image
            panel = render_pet_panel_image(asset, sample)
            self._panel_image = compose_n4pro_background(panel)
            self._panel_visual_key = visual_key
            self._panel_semantic_key = semantic_key
            self._panel_render_bucket = render_bucket
            self._panel_revision += 1
            return self._panel_revision, self._panel_image

    def diagnostics(self) -> dict[str, Any]:
        """返回不含原图、路径或完整 manifest 的 JSON-safe 宠物诊断。

        入参：无。
        返回：开关、选择、解析状态、名称、版本、activity、motion、更新时间、warning、
        资产短错误与独立的 motion 降级诊断。
        错误处理：无；读取原子快照。
        副作用：无；不采样动画、不访问文件。
        """

        with self._lock:
            resolution = self._resolution
            asset = resolution.asset if resolution is not None else None
            last_error = self._last_error
            if last_error is None and resolution is not None:
                last_error = resolution.error
            warnings = resolution.warnings if resolution is not None else ()
            return {
                "enabled": self._enabled,
                "selected_avatar_id": (
                    resolution.selected_avatar_id if resolution is not None else None
                ),
                "resolution_status": (
                    resolution.status.value
                    if resolution is not None
                    else ("not_polled" if self._enabled else "disabled")
                ),
                "display_name": (
                    asset.manifest.display_name if asset is not None else None
                ),
                "sprite_version": (
                    asset.sprite_version_number if asset is not None else None
                ),
                "activity": self._activity.activity.value,
                "app_overlay": dict(self._app_overlay_diagnostics),
                "motion": self._motion.value,
                "effective_motion": self._effective_motion,
                "updated_at": (
                    self._updated_at.isoformat() if self._updated_at is not None else None
                ),
                "warnings": list(warnings),
                "last_error": last_error,
                "motion_error": self._motion_error,
            }

    def diagnostic_panel_content(self) -> tuple[str, str, tuple[str, ...]]:
        """返回 PETS 无可用自定义图集时的简短占位文案。

        入参：无。
        返回：``(标题, 状态行, 附加行)``，不包含本机文件路径或图片内容。
        错误处理：无。
        副作用：无。
        """

        diagnostics = self.diagnostics()
        selected = diagnostics["selected_avatar_id"] or "No pet selected"
        status = diagnostics["resolution_status"]
        error = diagnostics["last_error"]
        lines = (str(error),) if error else ("Select a custom Codex pet to animate here.",)
        return "Codex Pet", f"{selected} · {status}", lines


def resolve_codex_pet_motion(
    motion: CodexPetMotion,
    *,
    reader: ReducedMotionReader | None = None,
    enabled: bool = True,
) -> tuple[bool, str, str | None]:
    """把配置 motion 解析为控制器布尔值、有效模式和可选诊断。

    入参：``motion`` 是 auto/full/reduced；``reader`` 可替换系统读取；关闭功能时不读系统。
    返回：``(reduced, effective_mode, error)``；auto 失败时为 ``False, full, 短错误``。
    错误处理：reader 异常被转换为诊断，不向 daemon 启动路径传播。
    副作用：auto 且启用时调用一次只读系统偏好 reader。
    """

    if motion == CodexPetMotion.REDUCED:
        return True, CodexPetMotion.REDUCED.value, None
    if motion == CodexPetMotion.FULL or not enabled:
        return False, CodexPetMotion.FULL.value, None
    resolved_reader = reader or read_macos_reduced_motion
    try:
        reduced = bool(resolved_reader())
    except Exception as exc:  # noqa: BLE001 - auto 必须降级 full。
        return False, CodexPetMotion.FULL.value, _short_error(
            "读取 macOS Reduce Motion 失败，已使用 full",
            exc,
        )
    return reduced, (
        CodexPetMotion.REDUCED.value if reduced else CodexPetMotion.FULL.value
    ), None


def read_macos_reduced_motion() -> bool:
    """尽力读取 macOS 全局 Reduce Motion 偏好。

    入参：无。
    返回：系统明确返回 1/true/yes 时为 True，0/false/no 时为 False；两个默认键都不存在
    也表示用户从未启用该选项，因此返回 False。
    错误处理：非 macOS、``defaults`` 不可用、超时或返回非缺省的不可识别错误时抛
    RuntimeError，交由 auto 策略降级 full。
    副作用：启动最多两个有界 ``defaults read`` 只读子进程，不写系统偏好。
    """

    if platform.system() != "Darwin":
        raise RuntimeError("当前平台不是 macOS")
    attempts = (
        ("com.apple.universalaccess", "reduceMotion"),
        ("NSGlobalDomain", "NSReduceMotion"),
    )
    errors: list[str] = []
    missing_defaults = 0
    for domain, key in attempts:
        try:
            completed = subprocess.run(
                ["defaults", "read", domain, key],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(type(exc).__name__)
            continue
        value = completed.stdout.strip().lower()
        if completed.returncode == 0 and value in {"1", "true", "yes"}:
            return True
        if completed.returncode == 0 and value in {"0", "false", "no"}:
            return False
        if "does not exist" in completed.stderr.lower():
            missing_defaults += 1
            continue
        errors.append(f"{domain}:{key}:{completed.returncode}")
    if missing_defaults == len(attempts):
        return False
    raise RuntimeError("; ".join(errors) or "未找到 Reduce Motion 偏好")


def _short_error(prefix: str, error: BaseException, *, limit: int = 240) -> str:
    """把 runtime 或系统读取异常压缩为单行短诊断。

    入参：``prefix`` 是稳定上下文；``error`` 是异常；``limit`` 是最大字符数。
    返回：不超过 limit 的单行文本。
    错误处理：异常字符串化按 Python 标准语义。
    副作用：无。
    """

    detail = " ".join(str(error).split()) or type(error).__name__
    return f"{prefix}: {detail}"[:limit]
