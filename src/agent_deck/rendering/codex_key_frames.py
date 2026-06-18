"""Codex 按键视觉变体到本地生成帧路径的解析工具。

本模块只读取 generated asset 目录的文件系统元数据，把 renderer-neutral 的视觉变体
映射成 StreamDock N4 Pro 可下发的 PNG 帧路径。它不打开图片、不读取 Codex 状态、
不访问真实硬件、不启动 daemon，也不修改任何文件。调用方负责决定哪些 slot 需要展示、
何时播放，以及是否把路径传给真实硬件 sink。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path


def codex_key_frame_paths_for_variants(
    *,
    frame_root: Path,
    variants: Iterable[str],
    start_key: int = 1,
    max_keys: int = 10,
) -> dict[int, tuple[Path, ...]]:
    """把 Codex 视觉变体序列映射到连续 N4 Pro 物理按钮帧路径。

    入参：`frame_root` 是 `generate-codex-assets` 产出的根目录；`variants` 是按展示顺序
    排列的变体名；`start_key` 是第一个物理按钮编号，默认 1；`max_keys` 是最多映射数量。
    返回：dict，key 为 N4 Pro 物理按钮编号，value 为对应变体 PNG 帧路径元组。
    错误处理：`start_key` 或 `max_keys` 非正、frame root 缺失、变体缺帧时抛出异常。
    副作用：只读取文件系统元数据；不打开图片、不访问硬件。
    """

    if start_key <= 0:
        raise ValueError("start_key must be positive")
    if max_keys <= 0:
        raise ValueError("max_keys must be positive")
    mapping: dict[int, tuple[Path, ...]] = {}
    for offset, variant_id in enumerate(tuple(variants)[:max_keys]):
        mapping[start_key + offset] = codex_variant_frame_paths(
            frame_root=frame_root,
            variant_id=variant_id,
        )
    return mapping


def codex_key_frame_paths_for_key_variants(
    *,
    frame_root: Path,
    key_variants: Mapping[int, str],
) -> dict[int, tuple[Path, ...]]:
    """把显式物理按钮编号到变体名的映射转换为帧路径。

    入参：`frame_root` 是生成资产根目录；`key_variants` 的 key 是 N4 Pro 物理按钮编号，
    value 是 `VisualIconSpec.variant_id`。
    返回：同 key 的帧路径 dict。
    错误处理：按钮编号非 1..15、frame root 缺失或变体缺帧时抛出异常。
    副作用：只读取文件系统元数据；不打开图片、不访问硬件。
    """

    invalid_keys = sorted(key for key in key_variants if key not in range(1, 16))
    if invalid_keys:
        raise ValueError(f"keys must be in range 1..15: {invalid_keys}")
    return {
        key: codex_variant_frame_paths(frame_root=frame_root, variant_id=variant_id)
        for key, variant_id in key_variants.items()
    }


def codex_variant_frame_paths(*, frame_root: Path, variant_id: str) -> tuple[Path, ...]:
    """读取某个 Codex 按键视觉变体的帧路径。

    入参：`frame_root` 是 generated frame 根目录；`variant_id` 是视觉层输出的变体名。
    返回：按文件名排序的 PNG 帧路径元组；offline 静态图返回单帧元组。
    错误处理：缺少根目录、offline 静态图、变体目录或 PNG 帧时抛 `FileNotFoundError`。
    副作用：只读取文件系统元数据。
    """

    resolved_root = frame_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise FileNotFoundError(f"Codex key frame root not found: {resolved_root}")

    if variant_id == "offline":
        offline_path = resolved_root / "offline.png"
        if not offline_path.is_file():
            raise FileNotFoundError(f"Codex offline frame not found: {offline_path}")
        return (offline_path,)

    variant_dir = resolved_root / variant_id
    if not variant_dir.is_dir():
        raise FileNotFoundError(f"Codex variant frame directory not found: {variant_dir}")
    frames = tuple(sorted(variant_dir.glob("frame_*.png")))
    if not frames:
        raise FileNotFoundError(f"Codex variant has no PNG frames: {variant_dir}")
    return frames
