"""本机 App catalog 与 App quick-action 测试。

这些测试只使用 pytest 临时目录里的 fake `.app` bundle 和 fake subprocess runner；
不扫描用户真实应用目录、不启动任何 App、不执行 shell。
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image

from agent_deck.actions.apps import list_local_apps, open_or_focus_local_app


def test_list_local_apps_reads_bundle_metadata_and_icon(tmp_path: Path) -> None:
    """App catalog 应读取 bundle 名称、bundle id、路径和图标 data URL。

    入参：`tmp_path` 提供 fake Applications root。
    返回：无返回值；断言通过代表 GUI 可使用真实 App metadata。
    错误处理：plist 或 icon 解析错误时由 pytest 报告。
    副作用：只写 pytest 临时目录中的 fake bundle。
    """

    app = _fake_app(
        tmp_path,
        name="Finder",
        bundle_id="com.apple.finder",
    )

    apps = list_local_apps(
        roots=(tmp_path,),
        app_bundles=(),
        limit=10,
        include_icon_data_url=True,
    )

    assert len(apps) == 1
    assert apps[0].name == "Finder"
    assert apps[0].bundle_id == "com.apple.finder"
    assert apps[0].app_path == str(app)
    assert apps[0].icon_token == "FI"
    assert apps[0].icon_data_url is not None
    assert apps[0].icon_data_url.startswith("data:image/png;base64,")


def test_list_local_apps_includes_explicit_system_bundles_before_limit(
    tmp_path: Path,
) -> None:
    """App catalog 应优先纳入 Finder 这类不在常规目录里的系统 App。

    入参：`tmp_path` 提供 fake Finder bundle 和 fake Applications root。
    返回：无返回值；断言通过代表 limit 很小时显式系统 App 不会被常规扫描挤掉。
    错误处理：系统 bundle 未优先纳入时由 pytest 报告。
    副作用：只写 pytest 临时目录中的 fake bundle。
    """

    finder = _fake_app(tmp_path, name="Finder", bundle_id="com.apple.finder")
    _fake_app(tmp_path, name="Activity Monitor", bundle_id="com.apple.ActivityMonitor")

    apps = list_local_apps(roots=(tmp_path,), app_bundles=(finder,), limit=1)

    assert len(apps) == 1
    assert apps[0].name == "Finder"
    assert apps[0].app_path == str(finder)
    assert apps[0].icon_data_url is None


def test_open_or_focus_local_app_uses_structured_open_args() -> None:
    """App quick-action 应通过结构化 `open` 参数执行，不使用 shell 字符串。

    入参：无；测试内注入 fake runner 捕获命令。
    返回：无返回值；断言通过代表 bundle id 优先且命令参数稳定。
    错误处理：命令拼接或结果映射错误时由 pytest 报告。
    副作用：只写测试内存列表。
    """

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        """记录命令并返回成功。"""

        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    result = open_or_focus_local_app(
        app_name="Finder",
        app_path="/System/Library/CoreServices/Finder.app",
        bundle_id="com.apple.finder",
        runner=fake_runner,
    )

    assert result.ok is True
    assert result.status == "succeeded"
    assert calls == [["open", "-b", "com.apple.finder"]]


def _fake_app(tmp_path: Path, *, name: str, bundle_id: str) -> Path:
    """创建最小 fake `.app` bundle。

    入参：`tmp_path` 是应用根目录；`name` 和 `bundle_id` 写入 Info.plist。
    返回：fake `.app` 路径。
    错误处理：文件写入失败按 pathlib/Pillow 异常传播。
    副作用：写 pytest 临时目录。
    """

    app = tmp_path / f"{name}.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleName": name,
                "CFBundleIdentifier": bundle_id,
                "CFBundleIconFile": "AppIcon.png",
            },
            handle,
        )
    Image.new("RGBA", (32, 32), (20, 120, 220, 255)).save(
        resources / "AppIcon.png"
    )
    return app
