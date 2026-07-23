"""把已启用 Remote SSH 主机的 Codex 自定义宠物受限镜像到 Agent Deck 缓存。

本模块只通过系统 ``sftp`` 对调用方明确给出的 SSH alias 读取当前 custom 宠物的 manifest
和 manifest 声明的单张图集。它不会枚举 ``~/.ssh/config``、不会写远端、不会执行宠物代码，
也不会把文件写进本机 ``.codex``。成功内容以摘要版本保存在 Agent Deck Application Support；
临时失败时只允许同一选择 ID 使用最近成功版本。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from typing import Final

from pydantic import ValidationError

from agent_deck.adapters.codex_pet import (
    CODEX_PET_VERSION_GEOMETRY,
    CodexPetAsset,
    CodexPetLoadError,
    CodexPetManifest,
    load_custom_codex_pet,
)
from agent_deck.adapters.codex_remote_ssh import validate_ssh_host_alias

_DEFAULT_CACHE_ROOT: Final[Path] = (
    Path.home() / "Library/Application Support/AgentDeck/remote-pets"
)
_MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
_MAX_SPRITESHEET_BYTES: Final[int] = 32 * 1024 * 1024
_DEFAULT_REFRESH_SECONDS: Final[float] = 300.0
_SAFE_CACHE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SFTP_MODE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?P<mode>[bcdlps-][rwxStTs-]{9})\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
)

SftpRunner = Callable[..., subprocess.CompletedProcess[bytes]]
"""执行一次系统 sftp batch 的可注入函数；生产默认使用 ``subprocess.run``。"""


class CodexRemotePetMirrorStatus(StrEnum):
    """描述一次远端 custom 宠物镜像解析结果。

    入参：值由镜像器生成，也可用于稳定诊断序列化。
    返回：字符串枚举，区分加载、同 ID 陈旧回退、非 custom、无可用内容和内容非法。
    错误处理：未知值由标准 Enum 拒绝。
    副作用：无。
    """

    LOADED = "loaded"
    STALE = "stale"
    NOT_CUSTOM = "not_custom"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CodexRemotePetMirrorResolution:
    """保存一个远端主机当前 custom 宠物的最小镜像结果。

    入参：host/selection 标识结果归属；``asset`` 只在 loaded/stale 时存在；``error`` 仅保存
    稳定错误代码，不包含远端路径、SFTP stderr 或 manifest 内容。
    返回：冻结结果，可在线程间原子替换。
    错误处理：一致性由镜像器保证，不在 dataclass 构造时重复校验。
    副作用：无。
    """

    host_id: str
    selected_avatar_id: str | None
    status: CodexRemotePetMirrorStatus
    updated_at: datetime
    asset: CodexPetAsset | None = None
    error: str | None = None

    @property
    def is_available(self) -> bool:
        """判断结果是否携带经过完整校验的可渲染图集。

        入参：无。
        返回：``asset`` 存在时为 True。
        错误处理：无。
        副作用：无。
        """

        return self.asset is not None


class CodexRemotePetMirrorError(RuntimeError):
    """表示受限 SFTP 镜像的安全、传输或缓存错误。

    入参：消息必须是稳定短代码，不得拼入远端输出或路径。
    返回：由 ``resolve`` 收敛为 unavailable/stale。
    错误处理：调用方通常不需要直接捕获。
    副作用：构造异常不访问网络或文件。
    """


class CodexRemotePetInvalidError(CodexRemotePetMirrorError):
    """表示远端内容违反 manifest、路径、文件类型或图集合同。

    入参：消息必须是无敏感信息的稳定短代码。
    返回：由 ``resolve`` 收敛为 invalid/stale。
    错误处理：同 ID 已有缓存时仍可返回 stale。
    副作用：无。
    """


@dataclass(frozen=True, slots=True)
class _RemoteFileMetadata:
    """保存下载前从 ``sftp ls -ln`` 投影出的普通文件大小。

    入参：``size`` 是远端声明字节数，必须由调用方检查上限。
    返回：冻结内部值。
    错误处理：模型不重复校验。
    副作用：无。
    """

    size: int


class CodexRemotePetMirror:
    """按 host/selection 缓存并刷新 Remote SSH 自定义宠物。

    入参：``cache_root`` 必须是 Agent Deck 自有目录；``refresh_interval_seconds`` 控制同一
    host/selection 的最短联网间隔；``timeout_seconds`` 限制每条 SFTP batch；runner 可测试替换。
    返回：``resolve`` 提供可渲染资产或安全诊断。
    错误处理：联网、解析和缓存错误不会向 daemon 轮询传播；同 ID LKG 可变为 stale。
    副作用：按需启动短生命周期只读 sftp，并在 cache root 内原子增加内容寻址版本。
    """

    def __init__(
        self,
        *,
        cache_root: Path | None = None,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_SECONDS,
        timeout_seconds: float = 10.0,
        runner: SftpRunner | None = None,
    ) -> None:
        """创建尚未连接远端的镜像器。

        入参：见类说明；刷新间隔可为 0 以便测试强制每次读取，timeout 必须为正。
        返回：无显式返回。
        错误处理：负刷新间隔或非正 timeout 抛 ValueError。
        副作用：不创建目录、不启动进程。
        """

        if refresh_interval_seconds < 0:
            raise ValueError("remote pet refresh interval must be non-negative")
        if timeout_seconds <= 0:
            raise ValueError("remote pet timeout must be positive")
        self._cache_root = resolve_codex_remote_pet_cache_root(cache_root)
        self._refresh_interval_seconds = float(refresh_interval_seconds)
        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner or subprocess.run
        self._last_attempts: dict[tuple[str, str], datetime] = {}
        self._last_resolutions: dict[
            tuple[str, str], CodexRemotePetMirrorResolution
        ] = {}

    def resolve(
        self,
        *,
        host: str,
        host_id: str,
        selected_avatar_id: str | None,
        now: datetime | None = None,
    ) -> CodexRemotePetMirrorResolution:
        """解析或刷新一个已启用 SSH 主机当前选择的 custom 宠物。

        入参：``host`` 必须来自 ChatGPT enabled Connection；``host_id`` 是 observer 摘要；
        selection 来自只读 ``config/read``；``now`` 可固定测试时间。
        返回：loaded/stale/invalid/unavailable/not_custom 冻结结果。
        错误处理：参数错误抛 ValueError；传输与内容错误被收敛且不含原始远端输出。
        副作用：custom 且到刷新窗口时执行只读 SFTP，并可能写 Agent Deck 缓存。
        """

        observed_at = _aware_now(now)
        normalized_host = validate_ssh_host_alias(host)
        normalized_host_id = _validate_cache_key(host_id, label="host id")
        selected_id = selected_avatar_id.strip() if selected_avatar_id else None
        if selected_id is None or not selected_id.startswith("custom:"):
            return CodexRemotePetMirrorResolution(
                host_id=normalized_host_id,
                selected_avatar_id=selected_id,
                status=CodexRemotePetMirrorStatus.NOT_CUSTOM,
                updated_at=observed_at,
            )
        try:
            pet_name = _validate_remote_pet_name(
                selected_id.removeprefix("custom:")
            )
        except ValueError as exc:
            return CodexRemotePetMirrorResolution(
                host_id=normalized_host_id,
                selected_avatar_id=selected_id,
                status=CodexRemotePetMirrorStatus.INVALID,
                updated_at=observed_at,
                error=_safe_error_code(exc),
            )
        cache_key = (normalized_host_id, selected_id)
        last_attempt = self._last_attempts.get(cache_key)
        last_resolution = self._last_resolutions.get(cache_key)
        if (
            last_attempt is not None
            and last_resolution is not None
            and (observed_at - last_attempt).total_seconds()
            < self._refresh_interval_seconds
        ):
            return last_resolution
        self._last_attempts[cache_key] = observed_at

        try:
            asset = self._mirror_asset(
                host=normalized_host,
                host_id=normalized_host_id,
                selected_avatar_id=selected_id,
                pet_name=pet_name,
                loaded_at=observed_at,
            )
        except (
            CodexRemotePetMirrorError,
            CodexPetLoadError,
            OSError,
            ValidationError,
            ValueError,
        ) as exc:
            cached_asset = (
                last_resolution.asset
                if last_resolution is not None and last_resolution.asset is not None
                else self._load_latest_cached(
                    host_id=normalized_host_id,
                    selected_avatar_id=selected_id,
                    pet_name=pet_name,
                    loaded_at=observed_at,
                )
            )
            status = (
                CodexRemotePetMirrorStatus.STALE
                if cached_asset is not None
                else (
                    CodexRemotePetMirrorStatus.INVALID
                    if isinstance(
                        exc,
                        (
                            CodexRemotePetInvalidError,
                            CodexPetLoadError,
                            ValidationError,
                            ValueError,
                        ),
                    )
                    else CodexRemotePetMirrorStatus.UNAVAILABLE
                )
            )
            resolution = CodexRemotePetMirrorResolution(
                host_id=normalized_host_id,
                selected_avatar_id=selected_id,
                status=status,
                updated_at=observed_at,
                asset=cached_asset,
                error=_safe_error_code(exc),
            )
        else:
            resolution = CodexRemotePetMirrorResolution(
                host_id=normalized_host_id,
                selected_avatar_id=selected_id,
                status=CodexRemotePetMirrorStatus.LOADED,
                updated_at=observed_at,
                asset=asset,
            )
        self._last_resolutions[cache_key] = resolution
        return resolution

    def _mirror_asset(
        self,
        *,
        host: str,
        host_id: str,
        selected_avatar_id: str,
        pet_name: str,
        loaded_at: datetime,
    ) -> CodexPetAsset:
        """下载两个受限文件、完整校验并原子提升为内容寻址缓存版本。

        入参：均已通过公开入口校验；``loaded_at`` 必须带时区。
        返回：从最终缓存路径重新加载的 ``CodexPetAsset``。
        错误处理：manifest 不存在、非普通文件、超限、路径不安全或图片非法时抛安全异常。
        副作用：current 包执行四条、legacy 包最多五条只读 SFTP batch，并在 cache root
        增加一个不可变版本目录。
        """

        self._cache_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".staging-", dir=self._cache_root) as temporary:
            staging_root = Path(temporary)
            bundle_root = staging_root / "bundle"
            codex_home = bundle_root / "codex"
            family, manifest_filename, remote_manifest_path = (
                self._select_remote_manifest(host=host, pet_name=pet_name)
            )
            package_dir = codex_home / family / pet_name
            package_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = package_dir / manifest_filename
            self._download_remote_file(
                host=host,
                remote_path=remote_manifest_path,
                local_path=manifest_path,
                maximum_bytes=_MAX_MANIFEST_BYTES,
                metadata_checked=True,
            )
            try:
                manifest_bytes = manifest_path.read_bytes()
                manifest = CodexPetManifest.model_validate_json(manifest_bytes)
            except (OSError, ValidationError, ValueError) as exc:
                raise CodexRemotePetInvalidError("manifest_invalid") from exc
            if manifest.sprite_version_number not in CODEX_PET_VERSION_GEOMETRY:
                raise CodexRemotePetInvalidError("sprite_version_unsupported")
            sprite_relative = _validate_remote_sprite_path(
                manifest.spritesheet_path
            )
            remote_package_dir = PurePosixPath(remote_manifest_path).parent
            remote_sprite_path = str(remote_package_dir / sprite_relative)
            local_sprite_path = package_dir.joinpath(*sprite_relative.parts)
            local_sprite_path.parent.mkdir(parents=True, exist_ok=True)
            self._download_remote_file(
                host=host,
                remote_path=remote_sprite_path,
                local_path=local_sprite_path,
                maximum_bytes=_MAX_SPRITESHEET_BYTES,
            )
            load_custom_codex_pet(
                codex_home=codex_home,
                selected_avatar_id=selected_avatar_id,
                loaded_at=loaded_at,
            )
            digest = hashlib.sha256()
            digest.update(selected_avatar_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(manifest_bytes)
            digest.update(b"\0")
            digest.update(local_sprite_path.read_bytes())
            version_dir = self._selection_cache_dir(
                host_id=host_id,
                selected_avatar_id=selected_avatar_id,
            ) / digest.hexdigest()
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            if not version_dir.exists():
                os.replace(bundle_root, version_dir)
            return load_custom_codex_pet(
                codex_home=version_dir / "codex",
                selected_avatar_id=selected_avatar_id,
                loaded_at=loaded_at,
            )

    def _select_remote_manifest(
        self,
        *,
        host: str,
        pet_name: str,
    ) -> tuple[str, str, str]:
        """按 current pets 后 legacy avatars 探测一个普通且限长的 manifest。

        入参：host 和 pet name 均已校验。
        返回：``(family, filename, remote_path)``。
        错误处理：两处均不可安全读取时抛 ``manifest_unavailable``。
        副作用：最多执行两次只读 ``sftp ls -ln``。
        """

        choices = (("pets", "pet.json"), ("avatars", "avatar.json"))
        for family, filename in choices:
            remote_path = f".codex/{family}/{pet_name}/{filename}"
            try:
                self._remote_file_metadata(
                    host=host,
                    remote_path=remote_path,
                    maximum_bytes=_MAX_MANIFEST_BYTES,
                )
            except CodexRemotePetInvalidError:
                raise
            except CodexRemotePetMirrorError:
                continue
            return family, filename, remote_path
        raise CodexRemotePetMirrorError("manifest_unavailable")

    def _download_remote_file(
        self,
        *,
        host: str,
        remote_path: str,
        local_path: Path,
        maximum_bytes: int,
        metadata_checked: bool = False,
    ) -> None:
        """在预检普通文件和大小后下载一个明确路径并复核本地字节数。

        入参：host/remote path 已收敛，local path 位于 staging，maximum 是硬上限；
        ``metadata_checked`` 表示 manifest 选择阶段刚完成同等预检。
        返回：无。
        错误处理：SFTP 失败、竞态消失、非普通文件或超限抛稳定安全异常。
        副作用：必要时执行一次 metadata batch，再执行一次 get batch，只写 staging 文件。
        """

        if not metadata_checked:
            self._remote_file_metadata(
                host=host,
                remote_path=remote_path,
                maximum_bytes=maximum_bytes,
            )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        batch = (
            f"get {_quote_sftp_path(remote_path)} "
            f"{_quote_sftp_path(str(local_path))}\n"
        )
        completed = self._run_sftp(host=host, batch=batch)
        if completed.returncode != 0 or not local_path.is_file():
            raise CodexRemotePetMirrorError("download_failed")
        actual_size = local_path.stat().st_size
        if actual_size <= 0 or actual_size > maximum_bytes:
            raise CodexRemotePetInvalidError("download_size_invalid")

    def _remote_file_metadata(
        self,
        *,
        host: str,
        remote_path: str,
        maximum_bytes: int,
    ) -> _RemoteFileMetadata:
        """用 ``ls -ln`` 拒绝 symlink/目录并在下载前检查声明大小。

        入参：远端路径必须不含控制字符；maximum 是本次文件上限。
        返回：只包含安全大小的元数据。
        错误处理：无法解析、非普通文件、空文件或超限抛稳定错误。
        副作用：执行一次只读 SFTP batch，不写本地文件。
        """

        batch = f"ls -ln {_quote_sftp_path(remote_path)}\n"
        completed = self._run_sftp(host=host, batch=batch)
        if completed.returncode != 0:
            raise CodexRemotePetMirrorError("remote_file_unavailable")
        output = completed.stdout.decode("utf-8", errors="replace")
        match = next(
            (
                candidate
                for line in output.splitlines()
                if (candidate := _SFTP_MODE_PATTERN.match(line.strip())) is not None
            ),
            None,
        )
        if match is None:
            raise CodexRemotePetMirrorError("remote_metadata_unavailable")
        if not match.group("mode").startswith("-"):
            raise CodexRemotePetInvalidError("remote_file_not_regular")
        size = int(match.group("size"))
        if size <= 0 or size > maximum_bytes:
            raise CodexRemotePetInvalidError("remote_file_size_invalid")
        return _RemoteFileMetadata(size=size)

    def _run_sftp(
        self,
        *,
        host: str,
        batch: str,
    ) -> subprocess.CompletedProcess[bytes]:
        """运行一条无 shell、无写远端命令的短生命周期 SFTP batch。

        入参：host 已通过严格 alias 校验；batch 只由本模块生成。
        返回：``CompletedProcess``，stdout 仅在内部解析元数据。
        错误处理：启动/超时异常包装为不含 alias 的 ``sftp_failed``。
        副作用：启动系统 sftp；OpenSSH 可按该明确 alias 使用用户既有连接配置/凭据。
        """

        timeout_option = max(1, int(round(self._timeout_seconds)))
        argv = (
            "sftp",
            "-q",
            "-b",
            "-",
            "-oBatchMode=yes",
            f"-oConnectTimeout={timeout_option}",
            host,
        )
        try:
            return self._runner(
                argv,
                input=batch.encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexRemotePetMirrorError("sftp_failed") from exc

    def _load_latest_cached(
        self,
        *,
        host_id: str,
        selected_avatar_id: str,
        pet_name: str,
        loaded_at: datetime,
    ) -> CodexPetAsset | None:
        """从同一 host/selection 的内容版本中加载最近成功资产。

        入参：选择 ID 必须与目标 custom 宠物完全一致；pet name 已校验。
        返回：首个仍通过完整本地路径/图片校验的资产，无可用版本时 None。
        错误处理：单个损坏版本被跳过，不删除文件。
        副作用：只读最多现存版本目录；不联网、不修改缓存。
        """

        selection_dir = self._selection_cache_dir(
            host_id=host_id,
            selected_avatar_id=selected_avatar_id,
        )
        if not selection_dir.is_dir():
            return None
        candidates: list[tuple[int, Path]] = []
        try:
            entries = tuple(selection_dir.iterdir())
        except OSError:
            return None
        for path in entries:
            try:
                if path.is_symlink() or not path.is_dir():
                    continue
                candidates.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        for _modified_at, version_dir in candidates:
            try:
                return load_custom_codex_pet(
                    codex_home=version_dir / "codex",
                    selected_avatar_id=f"custom:{pet_name}",
                    loaded_at=loaded_at,
                )
            except (CodexPetLoadError, OSError, ValueError):
                continue
        return None

    def _selection_cache_dir(
        self,
        *,
        host_id: str,
        selected_avatar_id: str,
    ) -> Path:
        """为 host/selection 生成不暴露原始名称的稳定缓存目录。

        入参：host id 已通过缓存 key 校验；selection 是完整 custom ID。
        返回：cache root 下两个 SHA-256 短摘要组成的目录。
        错误处理：无。
        副作用：不创建目录。
        """

        host_digest = hashlib.sha256(host_id.encode("utf-8")).hexdigest()[:20]
        selection_digest = hashlib.sha256(
            selected_avatar_id.encode("utf-8")
        ).hexdigest()[:20]
        return self._cache_root / host_digest / selection_digest


def resolve_codex_remote_pet_cache_root(path: Path | None = None) -> Path:
    """解析远端宠物镜像缓存根目录。

    入参：显式 path 供测试/嵌入覆盖；为空时固定使用 Agent Deck Application Support。
    返回：展开用户目录后的 Path，不要求当前存在。
    错误处理：无。
    副作用：不创建目录，也绝不回退到 ``~/.codex``。
    """

    return (path or _DEFAULT_CACHE_ROOT).expanduser()


def _validate_cache_key(value: str, *, label: str) -> str:
    """校验只用于内存关联的 opaque key，防止进入异常缓存路径。

    入参：value 是 host id；label 仅用于参数错误文本。
    返回：去除首尾空白的值。
    错误处理：空值、分隔符或控制字符抛 ValueError。
    副作用：无。
    """

    normalized = value.strip()
    if _SAFE_CACHE_KEY_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"invalid remote pet {label}")
    return normalized


def _validate_remote_pet_name(value: str) -> str:
    """校验 custom 名称既不能逃逸目录，也不能触发 SFTP glob/control 语义。

    入参：value 是移除 ``custom:`` 后的名称。
    返回：原名称，保留合法 Unicode 和空格。
    错误处理：空值、点目录、路径分隔符、glob 或控制字符抛 ValueError。
    副作用：无。
    """

    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or PureWindowsPath(value).is_absolute()
        or "/" in value
        or "\\" in value
        or any(character in value for character in "*?[]")
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("invalid remote custom pet name")
    return value


def _validate_remote_sprite_path(value: str) -> PurePosixPath:
    """把 manifest sprite path 收敛为宠物目录内安全的 POSIX 相对路径。

    入参：value 来自已验证 manifest。
    返回：不含 ``.``/``..``、glob、反斜杠或控制字符的 PurePosixPath。
    错误处理：不安全时抛 ``CodexRemotePetInvalidError``。
    副作用：无。
    """

    windows = PureWindowsPath(value)
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
        or any(character in value for character in "*?[]")
        or any(ord(character) < 32 for character in value)
    ):
        raise CodexRemotePetInvalidError("spritesheet_path_invalid")
    return relative


def _quote_sftp_path(value: str) -> str:
    """为 OpenSSH sftp batch 引用一个已收敛的本地或远端路径。

    入参：value 不得包含 NUL、CR 或 LF；双引号和反斜杠会转义。
    返回：双引号包裹的 batch token。
    错误处理：控制字符抛 ``CodexRemotePetInvalidError``。
    副作用：无。
    """

    if any(character in value for character in "\0\r\n"):
        raise CodexRemotePetInvalidError("sftp_path_invalid")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _safe_error_code(error: BaseException) -> str:
    """把镜像异常压缩为不会泄露远端数据的稳定代码。

    入参：任意已捕获异常。
    返回：已知镜像异常的短消息，否则返回异常类型名。
    错误处理：无。
    副作用：不读取 traceback 或 stderr。
    """

    if isinstance(error, CodexRemotePetMirrorError) and str(error):
        return str(error)[:80]
    if isinstance(error, CodexPetLoadError):
        return "cached_asset_invalid"
    if isinstance(error, ValidationError):
        return "manifest_invalid"
    if isinstance(error, ValueError):
        return "content_invalid"
    return type(error).__name__[:80]


def _aware_now(value: datetime | None) -> datetime:
    """返回 timezone-aware 的当前或注入时间。

    入参：value 可为空；为空时取当前 UTC。
    返回：带时区 datetime。
    错误处理：naive datetime 抛 ValueError。
    副作用：未注入时读取系统时钟。
    """

    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("remote pet timestamps must be timezone-aware")
    return result
