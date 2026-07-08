"""本机 App catalog 与打开/切换动作。

本模块只处理受信任的本机 `.app` bundle：只读扫描应用目录、解析 `Info.plist`、尽量提取
bundle 图标，并通过结构化 subprocess 参数打开 App。它不接受任意 shell 字符串、不拼接命令、
不读取 App 内业务数据，也不操作 App UI 内部控件。
"""

from __future__ import annotations

import base64
import io
import plistlib
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

AppRunner = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_APP_ROOTS = (
    Path("/Applications"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
)
DEFAULT_APP_BUNDLES = (
    Path("/System/Library/CoreServices/Finder.app"),
)
"""不会稳定出现在常规应用目录扫描中的系统核心 App。"""


class LocalAppInfo(BaseModel):
    """描述一个可被用户绑定到按键的本机 App。

    入参：字段来自 `.app/Contents/Info.plist` 和 bundle 路径；`icon_data_url` 是可选 PNG data URL。
    返回：frozen Pydantic model，可由 FastAPI 序列化。
    错误处理：字段类型非法由 Pydantic 报告。
    副作用：模型自身不读取文件、不启动 App。
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    app_path: str = Field(min_length=1)
    bundle_id: str | None = None
    icon_token: str = Field(min_length=1)
    icon_data_url: str | None = None


class LocalAppActionResult(BaseModel):
    """描述一次本机 App 打开/切换动作结果。

    入参：`ok` 表示动作是否成功；`status` 是稳定状态；`message` 是给 status 的诊断文本。
    返回：frozen Pydantic model。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身不执行动作。
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    status: str
    app_name: str | None = None
    app_path: str | None = None
    bundle_id: str | None = None
    message: str


def list_local_apps(
    *,
    roots: Iterable[Path] = DEFAULT_APP_ROOTS,
    app_bundles: Iterable[Path] = DEFAULT_APP_BUNDLES,
    limit: int = 120,
    include_icon_data_url: bool = False,
) -> tuple[LocalAppInfo, ...]:
    """扫描本机应用目录并返回可配置 App 列表。

    入参：`roots` 是允许扫描的应用目录；`app_bundles` 是额外显式纳入的系统 App；
    `limit` 控制最多返回多少个 App；`include_icon_data_url` 只供兼容或测试使用，正式 GUI
    应优先消费 App icon cache URL。
    返回：按名称排序的 `LocalAppInfo` tuple。
    错误处理：不可读目录、坏 plist 或坏图标会跳过对应项，不让 catalog API 失败。
    副作用：只读访问指定目录下 `.app` bundle 和图标文件；不启动 App、不访问网络。
    """

    apps: dict[str, LocalAppInfo] = {}
    for app_path in app_bundles:
        info = _read_app_info(
            app_path.expanduser(),
            include_icon_data_url=include_icon_data_url,
        )
        if info is None:
            continue
        key = info.bundle_id or info.app_path
        apps.setdefault(key, info)
        if len(apps) >= limit:
            return tuple(sorted(apps.values(), key=lambda app: app.name.casefold()))
    for app_path in _iter_app_bundles(roots):
        info = _read_app_info(
            app_path,
            include_icon_data_url=include_icon_data_url,
        )
        if info is None:
            continue
        key = info.bundle_id or info.app_path
        apps.setdefault(key, info)
        if len(apps) >= limit:
            break
    return tuple(sorted(apps.values(), key=lambda app: app.name.casefold()))


def load_local_app_icon(
    app_path: str | Path,
    *,
    max_size: tuple[int, int] = (96, 96),
) -> Image.Image | None:
    """读取单个 `.app` bundle 的图标并转换成 RGBA 图像。

    入参：`app_path` 是 `.app` bundle 路径；`max_size` 是返回图像的最大尺寸。
    返回：成功时返回独立的 RGBA `Image`；缺少 plist、缺少图标或解析失败时返回 None。
    错误处理：图标读取异常被吞掉，避免坏 App 图标影响硬件渲染。
    副作用：只读访问 bundle 内 `Info.plist` 和图标资源；不启动 App、不访问网络。
    """

    bundle_path = Path(app_path).expanduser()
    plist_path = bundle_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    icon_path = _icon_path(info, bundle_path)
    if icon_path is None:
        return None
    return load_image_icon(icon_path, max_size=max_size)


def resolve_local_app_icon_path(app_path: str | Path) -> Path | None:
    """解析 `.app` bundle 中声明的图标文件路径。

    入参：`app_path` 是 `.app` bundle 路径。
    返回：存在的图标文件路径；缺少 plist、缺少图标或文件不存在时返回 None。
    错误处理：读取或解析 plist 失败返回 None。
    副作用：只读访问 bundle 内 `Info.plist` 和 Resources 目录。
    """

    bundle_path = Path(app_path).expanduser()
    plist_path = bundle_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return _icon_path(info, bundle_path)


def load_image_icon(
    path: str | Path,
    *,
    max_size: tuple[int, int] = (96, 96),
) -> Image.Image | None:
    """读取 Pillow 支持的图标文件并缩放到最大尺寸内。

    入参：`path` 是 `.icns`、PNG 等图标文件路径；`max_size` 是返回图像的最大尺寸。
    返回：成功时返回独立 RGBA 图像；读取或转换失败返回 None。
    错误处理：吞掉图标解析异常，调用方用文字 fallback。
    副作用：只读图片文件。
    """

    try:
        with Image.open(path) as image:
            image.load()
            converted = image.convert("RGBA")
            converted.thumbnail(max_size, Image.Resampling.LANCZOS)
            return converted.copy()
    except Exception:
        return None


def open_or_focus_local_app(
    *,
    app_name: str | None = None,
    app_path: str | None = None,
    bundle_id: str | None = None,
    runner: AppRunner = subprocess.run,
) -> LocalAppActionResult:
    """打开或切换到一个本机 App。

    入参：优先使用 `bundle_id`，其次 `app_path`，最后 `app_name`；`runner` 是可注入 subprocess
    runner。
    返回：动作成功或失败诊断。
    错误处理：缺少目标返回 unsupported；runner 异常或非 0 exit 返回 failed。
    副作用：调用 macOS `open` 命令；不使用 shell、不执行任意命令字符串。
    """

    args: list[str]
    target = app_name or bundle_id or app_path or "App"
    if bundle_id:
        args = ["open", "-b", bundle_id]
    elif app_path:
        args = ["open", app_path]
    elif app_name:
        args = ["open", "-a", app_name]
    else:
        return LocalAppActionResult(
            ok=False,
            status="unsupported",
            app_name=app_name,
            app_path=app_path,
            bundle_id=bundle_id,
            message="open_or_focus_app ignored; missing app target",
        )
    try:
        completed = runner(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - 系统动作必须转换成可诊断结果。
        return LocalAppActionResult(
            ok=False,
            status="failed",
            app_name=app_name,
            app_path=app_path,
            bundle_id=bundle_id,
            message=f"open_or_focus_app failed: {type(exc).__name__}: {exc}",
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = f"open_or_focus_app failed with exit {completed.returncode}"
        if detail:
            message = f"{message}: {detail}"
        return LocalAppActionResult(
            ok=False,
            status="failed",
            app_name=app_name,
            app_path=app_path,
            bundle_id=bundle_id,
            message=message,
        )
    return LocalAppActionResult(
        ok=True,
        status="succeeded",
        app_name=app_name,
        app_path=app_path,
        bundle_id=bundle_id,
        message=f"opened or focused {target}",
    )


def _iter_app_bundles(roots: Iterable[Path]) -> Iterable[Path]:
    """枚举指定 root 下一层和常见 Utilities 子目录里的 `.app` bundle。

    入参：`roots` 是候选应用目录。
    返回：`.app` bundle 路径迭代器。
    错误处理：不可读目录会被跳过。
    副作用：只读目录项。
    """

    seen: set[Path] = set()
    for root in roots:
        expanded = root.expanduser()
        candidates = (expanded, expanded / "Utilities")
        for directory in candidates:
            if not directory.is_dir():
                continue
            try:
                children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
            except OSError:
                continue
            for child in children:
                if child.suffix == ".app" and child.is_dir() and child not in seen:
                    seen.add(child)
                    yield child


def _read_app_info(
    app_path: Path,
    *,
    include_icon_data_url: bool = False,
) -> LocalAppInfo | None:
    """读取一个 `.app` bundle 的基础信息。

    入参：`app_path` 是 `.app` bundle 路径。
    返回：可用 App 信息；缺少 Info.plist 或名称时返回 None。
    错误处理：坏 plist、坏图标或读取失败返回 None 或忽略图标。
    副作用：只读 bundle 内 `Info.plist` 和图标资源。
    """

    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    name = _app_name(info, app_path)
    if not name:
        return None
    icon_path = _icon_path(info, app_path) if include_icon_data_url else None
    return LocalAppInfo(
        name=name,
        app_path=str(app_path),
        bundle_id=_string_value(info.get("CFBundleIdentifier")),
        icon_token=_icon_token(name),
        icon_data_url=_icon_data_url(icon_path) if icon_path else None,
    )


def _app_name(info: dict[object, object], app_path: Path) -> str:
    """从 plist 和路径推导 App 名称。

    入参：`info` 是 Info.plist dict；`app_path` 是 bundle 路径。
    返回：非空名称或空字符串。
    错误处理：无。
    副作用：无。
    """

    for key in ("CFBundleDisplayName", "CFBundleName"):
        value = _string_value(info.get(key))
        if value:
            return value
    return app_path.stem.strip()


def _icon_path(info: dict[object, object], app_path: Path) -> Path | None:
    """解析 App icon 文件路径。

    入参：`info` 是 Info.plist dict；`app_path` 是 bundle 路径。
    返回：存在的 icon 路径，支持显式后缀或默认 `.icns`。
    错误处理：缺少或不存在时返回 None。
    副作用：只检查文件存在性。
    """

    raw_icon = _string_value(info.get("CFBundleIconFile"))
    if raw_icon is None:
        return None
    resources = app_path / "Contents" / "Resources"
    candidates = [resources / raw_icon]
    if "." not in Path(raw_icon).name:
        candidates.append(resources / f"{raw_icon}.icns")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _icon_data_url(path: Path) -> str | None:
    """把 App icon 转成小尺寸 PNG data URL。

    入参：`path` 是 `.icns` 或测试用 PNG 等 Pillow 支持的图片路径。
    返回：PNG data URL；读取或转换失败返回 None。
    错误处理：吞掉图标解析异常，避免坏图标破坏 App catalog。
    副作用：只读图片文件并在内存中编码。
    """

    converted = load_image_icon(path)
    if converted is None:
        return None
    buffer = io.BytesIO()
    converted.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _icon_token(name: str) -> str:
    """生成没有图标时使用的短 token。

    入参：`name` 是 App 名称。
    返回：1-2 个大写字符。
    错误处理：空名称返回 `A`。
    副作用：无。
    """

    words = [part for part in name.replace("-", " ").split() if part]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (name[:2] or "A").upper()


def _string_value(value: object) -> str | None:
    """读取 plist 中的非空字符串值。

    入参：`value` 是任意 plist 字段值。
    返回：trim 后的字符串或 None。
    错误处理：非字符串返回 None。
    副作用：无。
    """

    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
