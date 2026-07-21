"""daemon 宠物协调器的 motion 降级与诊断测试。

本模块只调用纯配置解析辅助函数，不读取真实 macOS 偏好、不加载图集、不启动 daemon 或硬件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from agent_deck.adapters.codex_pet import CodexPetAsset, CodexPetManifest, PetActivity
from agent_deck.config import CodexPetMotion
from agent_deck.rendering.codex_pet import PetSceneSample
from agent_deck.server import codex_pet_runtime
from agent_deck.server.codex_pet_runtime import CodexPetRuntime, resolve_codex_pet_motion


def test_auto_motion_follows_reader_and_failure_falls_back_to_full() -> None:
    """auto 应跟随系统读数，读取失败时明确降级 full 并留下诊断。

    入参：无；测试注入 True/False reader 和一个抛异常 reader。
    返回：无；断言通过代表 reduced-motion 与 fail-open-to-full 合同稳定。
    错误处理：注入异常必须被转换为短文本，不向调用者传播。
    副作用：只调用内存 lambda，不启动 ``defaults`` 子进程。
    """

    assert resolve_codex_pet_motion(
        CodexPetMotion.AUTO,
        reader=lambda: True,
    ) == (True, "reduced", None)
    assert resolve_codex_pet_motion(
        CodexPetMotion.AUTO,
        reader=lambda: False,
    ) == (False, "full", None)

    def fail_reader() -> bool:
        """模拟系统偏好不可读。

        入参：无。
        返回：不会返回。
        错误处理：总是抛 RuntimeError，供被测函数降级。
        副作用：无。
        """

        raise RuntimeError("preference unavailable")

    reduced, effective, error = resolve_codex_pet_motion(
        CodexPetMotion.AUTO,
        reader=fail_reader,
    )
    assert reduced is False
    assert effective == "full"
    assert error is not None
    assert "preference unavailable" in error


def test_forced_motion_modes_do_not_read_system() -> None:
    """full/reduced 显式模式不应调用系统偏好 reader。

    入参：无；注入若被调用就失败的 reader。
    返回：无；断言通过代表显式配置稳定且无多余 I/O。
    错误处理：reader 被意外调用时 pytest 会收到 AssertionError。
    副作用：无。
    """

    def unexpected_reader() -> bool:
        """在显式模式中禁止被调用的测试 reader。

        入参：无。
        返回：不会返回。
        错误处理：总是抛 AssertionError。
        副作用：无。
        """

        raise AssertionError("reader should not be called")

    assert resolve_codex_pet_motion(
        CodexPetMotion.FULL,
        reader=unexpected_reader,
    ) == (False, "full", None)
    assert resolve_codex_pet_motion(
        CodexPetMotion.REDUCED,
        reader=unexpected_reader,
    ) == (True, "reduced", None)


def test_motion_failure_is_diagnosed_without_masquerading_as_asset_failure(
    tmp_path: Path,
) -> None:
    """auto 偏好读取失败应独立诊断，不占用素材 ``last_error``。

    入参：pytest 临时目录；注入必定失败的 reduced-motion reader。
    返回：无；断言 motion_error 有值而 last_error 仍为空。
    错误处理：runtime 应吸收 reader 异常并降级 full。
    副作用：仅构造内存 runtime，不读取 Codex 配置或写缓存。
    """

    def fail_reader() -> bool:
        """模拟 macOS 偏好读取失败。

        入参：无。
        返回：不会返回。
        错误处理：总是抛 RuntimeError，供 runtime 转换为诊断。
        副作用：无。
        """

        raise RuntimeError("preference unavailable")

    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.AUTO,
        cache_root=tmp_path,
        fallback_key_path=None,
        reduced_motion_reader=fail_reader,
    )

    diagnostics = runtime.diagnostics()
    assert diagnostics["effective_motion"] == "full"
    assert diagnostics["motion_error"] is not None
    assert diagnostics["last_error"] is None


def test_missing_macos_reduce_motion_defaults_mean_motion_is_not_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS 从未启用 Reduce Motion 时缺少默认键，应视为 False 而不是读取失败。

    入参：pytest monkeypatch；模拟 Darwin 和两个 ``defaults read`` 的标准“does not exist”。
    返回：无；断言系统 reader 返回 False。
    错误处理：其他 subprocess 错误仍由独立路径处理，本测试只覆盖正常缺省语义。
    副作用：只替换模块内平台/子进程函数，不启动真实 ``defaults``。
    """

    monkeypatch.setattr(codex_pet_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        codex_pet_runtime.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="The domain/default pair does not exist",
        ),
    )

    assert codex_pet_runtime.read_macos_reduced_motion() is False


def test_panel_fps_uses_absolute_time_buckets_at_ten_hz_sampling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 Hz 调用 8 FPS provider 时应产出约 8 帧，而不是被 tick 量化成 5 FPS。

    入参：pytest 临时目录与 monkeypatch；用持续变化的位置样本驱动 0.0..0.9 秒采样。
    返回：无；断言绝对时间 bucket 在十次采样中生成八个 revision。
    错误处理：旧的“距上次渲染满 125ms”实现会稳定只生成五帧并令测试失败。
    副作用：只替换内存渲染函数，不读 Codex 配置、不编码 PNG、不访问硬件。
    """

    runtime = CodexPetRuntime(
        enabled=True,
        panel_fps=8,
        motion=CodexPetMotion.FULL,
        cache_root=tmp_path,
        fallback_key_path=None,
        started_at_monotonic=0.0,
    )
    manifest = CodexPetManifest.model_validate(
        {
            "id": "fps-test",
            "displayName": "FPS Test",
            "spritesheetPath": "spritesheet.png",
        }
    )
    asset = CodexPetAsset(
        selected_avatar_id="custom:fps-test",
        manifest=manifest,
        package_dir=tmp_path,
        manifest_path=tmp_path / "pet.json",
        spritesheet_path=tmp_path / "spritesheet.png",
        spritesheet=Image.new("RGBA", (1, 1)),
        loaded_at=datetime(2026, 7, 21, tzinfo=UTC),
        source_fingerprint="fps-test-fingerprint",
    )
    runtime._resolution = SimpleNamespace(asset=asset)  # noqa: SLF001 - 定向验证 provider 调度。

    def sample_scene(
        _activity: object,
        *,
        monotonic_seconds: float,
    ) -> PetSceneSample:
        """返回每个 10 Hz tick 都有不同水平 bucket 的运行样本。

        入参：忽略活动快照；monotonic_seconds 是被测 provider 传入的绝对时钟。
        返回：位置随 0..0.9 秒线性变化的合法场景样本。
        错误处理：测试时间越界时由 PetSceneSample 校验失败。
        副作用：无。
        """

        return PetSceneSample(
            activity=PetActivity.RUNNING,
            action="running-right",
            frame_index=0,
            x=monotonic_seconds,
            direction="right",
            sampled_at_monotonic=monotonic_seconds,
        )

    def render_panel(_asset: CodexPetAsset, _sample: PetSceneSample) -> Image.Image:
        """返回无需真实图集裁切的合成 PETS viewport。

        入参：忽略测试资产与样本。
        返回：800x136 RGBA 图像。
        错误处理：Pillow 分配失败按原异常传播。
        副作用：仅分配内存图像。
        """

        return Image.new("RGBA", (800, 136), (11, 15, 22, 255))

    def compose_background(_panel: Image.Image) -> Image.Image:
        """返回硬件 provider 所需的完整 800x480 合成背景。

        入参：忽略合成 viewport。
        返回：800x480 RGBA 图像。
        错误处理：Pillow 分配失败按原异常传播。
        副作用：仅分配内存图像。
        """

        return Image.new("RGBA", (800, 480), (11, 15, 22, 255))

    monkeypatch.setattr(runtime._controller, "sample", sample_scene)  # noqa: SLF001
    monkeypatch.setattr(codex_pet_runtime, "render_pet_panel_image", render_panel)
    monkeypatch.setattr(codex_pet_runtime, "compose_n4pro_background", compose_background)

    revisions = [
        runtime.panel_background(monotonic_seconds=index / 10)[0]
        for index in range(10)
    ]

    assert revisions == [1, 1, 2, 3, 4, 5, 5, 6, 7, 8]
