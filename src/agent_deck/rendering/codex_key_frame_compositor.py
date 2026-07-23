"""为预生成 Codex Agent 动画帧派生带全局背景的可下发 PNG。

源帧保持仓库内 RGBA 资产不变；仅在用户设置全局背景时，把透明度合成到对应颜色并写入
daemon 生命周期缓存。模块不理解 layout、不访问 HID，也不会覆盖源资产。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from PIL import Image

from agent_deck.rendering.appearance import rgb_from_hex


def composite_codex_key_frame_paths(
    frame_paths: Mapping[int, tuple[Path, ...]],
    *,
    background_color: str,
    cache_root: Path,
) -> dict[int, tuple[Path, ...]]:
    """把一组 Agent RGBA 动画帧合成为指定背景色的缓存路径。

    入参：物理键到源帧路径、已校验 ``#RRGGBB`` 和 daemon 生命周期缓存目录。
    返回：保持物理键与帧顺序不变的派生 RGB PNG 路径。
    错误处理：源图缺失、图片损坏或缓存不可写时按 Pillow/pathlib 原异常传播。
    副作用：首次遇到源帧或源文件更新时创建父目录并原子替换派生 PNG。
    """

    color_key = background_color.removeprefix("#").upper()
    derived: dict[int, tuple[Path, ...]] = {}
    for key, paths in frame_paths.items():
        derived[key] = tuple(
            _composite_codex_key_frame(
                source_path,
                background_color=background_color,
                cache_root=cache_root / color_key,
            )
            for source_path in paths
        )
    return derived


def _composite_codex_key_frame(
    source_path: Path,
    *,
    background_color: str,
    cache_root: Path,
) -> Path:
    """派生单帧并用源路径 hash 与 mtime 保证缓存身份稳定。

    入参：源 PNG、背景色和颜色专属缓存根。
    返回：已存在或刚生成的 RGB PNG 路径。
    错误处理：stat、图片读取或文件写入失败时传播原异常。
    副作用：需要重建时写一个临时 PNG 并原子替换目标。
    """

    source_stat = source_path.stat()
    stable_name = (
        f"{source_path.parent.name}-{source_path.stem}-"
        f"{source_stat.st_size:x}-{source_stat.st_mtime_ns:x}.png"
    )
    destination = cache_root / stable_name
    if destination.is_file():
        return destination
    cache_root.mkdir(parents=True, exist_ok=True)
    with Image.open(source_path) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (*rgb_from_hex(background_color), 255))
        background.alpha_composite(rgba)
        output = background.convert("RGB")
    temporary = destination.with_suffix(".tmp.png")
    output.save(temporary, format="PNG")
    temporary.replace(destination)
    return destination
