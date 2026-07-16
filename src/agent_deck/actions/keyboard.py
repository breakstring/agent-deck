"""键盘快捷键的跨平台模型、校验与单任务调度。

本模块只定义硬件无关的快捷键合同，并把执行工作移交给可注入 executor。它不直接调用
macOS API，不读取前台应用，也不在硬件输入线程中等待；平台实现位于
``agent_deck.actions.macos_keyboard``。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY_HOLD_MILLISECONDS = 20
"""每个快捷键步骤按下主键后保持的固定毫秒数。"""

DEFAULT_SEQUENCE_GAP_MILLISECONDS = 100
"""GUI 新增序列步骤时写入前一步的默认间隔毫秒数。"""

MAX_SHORTCUT_DURATION_MILLISECONDS = 10_000
"""单次快捷键序列允许占用执行器的最大理论时长。"""

MAX_SHORTCUT_STEPS = 16
"""单个快捷键动作允许包含的最大步骤数。"""


class KeyboardModifier(StrEnum):
    """描述 macOS 常用快捷键修饰键。

    入参：值来自 GUI 或持久化 JSON，只接受 command/control/option/shift。
    返回：可直接 JSON 序列化的字符串枚举。
    错误处理：未知值由 Pydantic 拒绝。
    副作用：无。
    """

    COMMAND = "command"
    CONTROL = "control"
    OPTION = "option"
    SHIFT = "shift"


MODIFIER_ORDER = (
    KeyboardModifier.COMMAND,
    KeyboardModifier.CONTROL,
    KeyboardModifier.OPTION,
    KeyboardModifier.SHIFT,
)
"""快捷键修饰键的规范化和默认展示顺序。"""


SUPPORTED_KEY_CODES = frozenset(
    {
        *(f"Key{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        *(f"Digit{digit}" for digit in "0123456789"),
        "Backquote",
        "Minus",
        "Equal",
        "BracketLeft",
        "BracketRight",
        "Backslash",
        "Semicolon",
        "Quote",
        "Comma",
        "Period",
        "Slash",
        "Enter",
        "Escape",
        "Backspace",
        "Tab",
        "Space",
        "Insert",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        *(f"F{number}" for number in range(1, 21)),
        *(f"Numpad{digit}" for digit in "0123456789"),
        "NumpadDecimal",
        "NumpadMultiply",
        "NumpadAdd",
        "NumpadDivide",
        "NumpadEnter",
        "NumpadSubtract",
        "NumpadEqual",
        "NumLock",
    }
)
"""第一版可配置的 W3C ``KeyboardEvent.code`` 白名单。"""


class KeyboardShortcutStep(BaseModel):
    """描述快捷键序列中的一个物理按键或组合键步骤。

    入参：``key`` 使用 W3C ``KeyboardEvent.code``；可为 None 以表达纯修饰键步骤；
    ``modifiers`` 是同时按下的修饰键；``delay_after_ms`` 是整个步骤释放后的等待时间。
    返回：frozen、字段封闭且修饰键顺序稳定的步骤模型。
    错误处理：未知键码、空步骤、重复修饰键或超范围延迟会抛校验错误。
    副作用：无。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str | None = None
    modifiers: tuple[KeyboardModifier, ...] = ()
    delay_after_ms: int = Field(default=0, ge=0, le=2_000)

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, value: object) -> object:
        """清理并校验可选 W3C key code。

        入参：``value`` 是 API/JSON 中的 key 字段。
        返回：trim 后的白名单键码或 None。
        错误处理：空字符串归一为 None，未知字符串抛 ValueError，其他类型交给 Pydantic。
        副作用：无。
        """

        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            if normalized not in SUPPORTED_KEY_CODES:
                raise ValueError(f"unsupported keyboard code: {normalized}")
            return normalized
        return value

    @field_validator("modifiers", mode="before")
    @classmethod
    def _normalize_modifiers(cls, value: object) -> object:
        """去重并按固定顺序规范修饰键。

        入参：``value`` 是 iterable 修饰键字符串或枚举。
        返回：无重复、顺序稳定的修饰键 tuple。
        错误处理：未知值交给枚举转换并由 Pydantic 报错。
        副作用：无。
        """

        if value is None:
            return ()
        try:
            parsed = tuple(KeyboardModifier(item) for item in value)  # type: ignore[arg-type]
        except TypeError:
            return value
        if len(set(parsed)) != len(parsed):
            raise ValueError("keyboard shortcut modifiers must not repeat")
        return tuple(modifier for modifier in MODIFIER_ORDER if modifier in parsed)

    @model_validator(mode="after")
    def _validate_non_empty_step(self) -> KeyboardShortcutStep:
        """确保步骤至少包含主键或一个修饰键。

        入参：已完成字段解析的步骤。
        返回：合法步骤本身。
        错误处理：key 和 modifiers 同时为空时抛 ValueError。
        副作用：无。
        """

        if self.key is None and not self.modifiers:
            raise ValueError("keyboard shortcut step requires a key or modifier")
        return self


class KeyboardShortcutSpec(BaseModel):
    """描述一次可执行的单键、组合键或有序快捷键序列。

    入参：``steps`` 必须有 1-16 项；最后一步不得再等待；步骤保持时间与间隔总和不得超过
    10 秒。
    返回：frozen、字段封闭的快捷键规格。
    错误处理：空序列、过长序列、末步延迟或总时长超限会抛校验错误。
    副作用：无。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: tuple[KeyboardShortcutStep, ...] = Field(
        min_length=1,
        max_length=MAX_SHORTCUT_STEPS,
    )

    @model_validator(mode="after")
    def _validate_sequence_timing(self) -> KeyboardShortcutSpec:
        """校验序列末步和理论总时长。

        入参：已解析的快捷键规格。
        返回：合法规格本身。
        错误处理：末步延迟非零或理论总时长超过 10 秒时抛 ValueError。
        副作用：无。
        """

        if self.steps[-1].delay_after_ms != 0:
            raise ValueError("last keyboard shortcut step delay_after_ms must be 0")
        duration_ms = len(self.steps) * KEY_HOLD_MILLISECONDS + sum(
            step.delay_after_ms for step in self.steps
        )
        if duration_ms > MAX_SHORTCUT_DURATION_MILLISECONDS:
            raise ValueError("keyboard shortcut sequence duration exceeds 10000 ms")
        return self


class ShortcutIconMode(StrEnum):
    """描述快捷键图标的来源模式。

    入参：``auto`` 使用组合键文字自动绘制；``custom`` 尝试读取上传资产。
    返回：可 JSON 序列化的字符串枚举。
    错误处理：未知值由 Pydantic 拒绝。
    副作用：无。
    """

    AUTO = "auto"
    CUSTOM = "custom"


class ShortcutIconSpec(BaseModel):
    """描述快捷键按键图标的自动或自定义选择。

    入参：``mode`` 是 auto/custom；custom 模式必须给出内容寻址 ``asset_id``。
    返回：frozen、字段封闭的图标规格。
    错误处理：custom 缺 asset_id、auto 携带 asset_id 或 id 格式非法时抛校验错误。
    副作用：无；不读取实际图片。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ShortcutIconMode = ShortcutIconMode.AUTO
    asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_asset_selection(self) -> ShortcutIconSpec:
        """校验图标模式与资产引用一致。

        入参：已解析的图标规格。
        返回：合法规格本身。
        错误处理：模式与 asset_id 不匹配时抛 ValueError。
        副作用：无。
        """

        if self.mode == ShortcutIconMode.CUSTOM and self.asset_id is None:
            raise ValueError("custom shortcut icon requires asset_id")
        if self.mode == ShortcutIconMode.AUTO and self.asset_id is not None:
            raise ValueError("auto shortcut icon must not include asset_id")
        return self


class KeyboardShortcutPermissionRequesterKind(StrEnum):
    """描述 macOS 权限请求进程使用的身份形态。

    入参：``app_bundle`` 表示打包 App 内的稳定执行文件；``development_runtime`` 表示
    Python 等开发运行时；``unknown`` 用于无法识别的注入 executor。
    返回：可 JSON 序列化的字符串枚举。
    错误处理：未知值由 Pydantic 拒绝。
    副作用：无。
    """

    APP_BUNDLE = "app_bundle"
    DEVELOPMENT_RUNTIME = "development_runtime"
    UNKNOWN = "unknown"


class KeyboardShortcutPermissionRequester(BaseModel):
    """描述实际调用 macOS 辅助功能权限 API 的当前 daemon 进程。

    入参：身份形态、面向用户的进程名、可选执行文件路径、身份是否适合长期授权和说明。
    返回：frozen 权限请求者快照，供 status/UI 解释“权限给谁”。
    错误处理：空名称或非法字段由 Pydantic 报告。
    副作用：模型自身无副作用；不读取系统权限数据库。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: KeyboardShortcutPermissionRequesterKind
    display_name: str = Field(min_length=1)
    executable_path: str | None = None
    stable_identity: bool
    note: str = Field(min_length=1)


class KeyboardShortcutCapability(BaseModel):
    """描述当前平台是否支持键盘事件以及权限状态。

    入参：平台、支持状态、当前授权、是否可显式请求/打开设置、当前请求进程和诊断消息。
    返回：frozen capability 快照，可直接用于 status/UI。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身无副作用。
    """

    model_config = ConfigDict(frozen=True)

    platform: str
    supported: bool
    permission_granted: bool
    can_request_permission: bool
    can_open_system_settings: bool = False
    permission_requester: KeyboardShortcutPermissionRequester | None = None
    message: str


class KeyboardShortcutRunStatus(StrEnum):
    """描述一次底层快捷键执行的终态。

    入参：由平台 executor 返回。
    返回：稳定字符串状态。
    错误处理：未知值由 Pydantic 拒绝。
    副作用：无。
    """

    SUCCEEDED = "succeeded"
    PERMISSION_REQUIRED = "permission_required"
    TARGET_UNAVAILABLE = "target_unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class KeyboardShortcutRunResult(BaseModel):
    """保存平台 executor 的一次同步执行结果。

    入参：终态、固定目标 PID 和面向诊断的消息。
    返回：frozen 结果模型；``succeeded`` 只表示事件已投递，不保证目标应用消费。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身无副作用。
    """

    model_config = ConfigDict(frozen=True)

    status: KeyboardShortcutRunStatus
    target_pid: int | None = Field(default=None, gt=0)
    message: str


class KeyboardShortcutExecutor(Protocol):
    """定义可注入的平台快捷键执行器合同。

    入参：实现必须提供只读 capability、显式权限请求和同步 execute。
    返回：由各方法返回 capability 或 run result。
    错误处理：实现可抛异常，scheduler 会收敛为 failed job。
    副作用：具体实现可能调用系统 API 并投递物理键事件。
    """

    def capability(self) -> KeyboardShortcutCapability:
        """返回只读权限与平台支持快照，不触发系统授权弹窗。"""

    def request_permission(self) -> KeyboardShortcutCapability:
        """显式请求系统权限并返回请求后的 capability。"""

    def execute(self, shortcut: KeyboardShortcutSpec) -> KeyboardShortcutRunResult:
        """同步向执行开始时固定的目标应用投递完整快捷键序列。"""


class KeyboardShortcutJobStatus(StrEnum):
    """描述 scheduler 内一个已接收任务的生命周期状态。

    入参：由 scheduler 内部维护。
    返回：可序列化字符串枚举。
    错误处理：无业务错误。
    副作用：无。
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PERMISSION_REQUIRED = "permission_required"
    TARGET_UNAVAILABLE = "target_unavailable"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class KeyboardShortcutJobSnapshot(BaseModel):
    """描述一个快捷键后台任务的只读快照。

    入参：任务 id、来源键位、快捷键规格、时间戳、状态和执行诊断。
    返回：frozen JSON-safe 模型，供 status 和 interaction action 使用。
    错误处理：时间或字段类型非法由 Pydantic 报告。
    副作用：无。
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    source: str
    key_index: int
    shortcut: KeyboardShortcutSpec
    submitted_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: KeyboardShortcutJobStatus
    target_pid: int | None = None
    message: str


class KeyboardShortcutSubmissionStatus(StrEnum):
    """描述非排队 scheduler 对一次提交的即时答复。

    入参：accepted、busy 或 closed。
    返回：稳定字符串枚举。
    错误处理：无业务错误。
    副作用：无。
    """

    ACCEPTED = "accepted"
    BUSY = "busy"
    CLOSED = "closed"


class KeyboardShortcutSubmission(BaseModel):
    """描述硬件输入线程收到的快捷键提交结果。

    入参：是否接受、即时状态、可选 job id 和消息。
    返回：frozen JSON-safe 模型；调用方无需等待物理键序列结束。
    错误处理：字段非法由 Pydantic 报告。
    副作用：模型自身无副作用。
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    status: KeyboardShortcutSubmissionStatus
    job_id: str | None = None
    message: str


@dataclass
class _MutableKeyboardShortcutJob:
    """保存 worker 执行期间可变的内部任务状态。

    入参：任务上下文和初始 queued 状态。
    返回：仅供 scheduler 加锁读写的内部对象。
    错误处理：不主动校验；外部快照由 Pydantic 校验。
    副作用：worker 会原地更新状态和时间戳。
    """

    job_id: str
    source: str
    key_index: int
    shortcut: KeyboardShortcutSpec
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status: KeyboardShortcutJobStatus
    target_pid: int | None
    message: str


class KeyboardShortcutScheduler:
    """用单 worker、零等待队列执行快捷键任务。

    入参：可注入 ``KeyboardShortcutExecutor`` 和 recent history 上限。
    返回：scheduler；``submit`` 在已有任务运行时立即返回 busy。
    错误处理：executor 异常被记录为 failed job；关闭后提交返回 closed。
    副作用：首次接受任务时创建一个后台线程，并在该线程调用平台 executor。
    """

    def __init__(
        self,
        executor: KeyboardShortcutExecutor,
        *,
        recent_limit: int = 20,
    ) -> None:
        """初始化单任务快捷键调度器。

        入参：``executor`` 是同步平台实现；``recent_limit`` 是保留终态任务数量。
        返回：无显式返回值。
        错误处理：非正 recent_limit 抛 ValueError。
        副作用：创建惰性线程池对象，但此时不启动 worker 线程。
        """

        if recent_limit <= 0:
            raise ValueError("recent_limit must be positive")
        self._executor = executor
        self._recent_limit = recent_limit
        self._pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agent-deck-keyboard",
        )
        self._lock = Lock()
        self._active: _MutableKeyboardShortcutJob | None = None
        self._recent: list[_MutableKeyboardShortcutJob] = []
        self._closed = False

    def submit(
        self,
        shortcut: KeyboardShortcutSpec,
        *,
        source: str,
        key_index: int,
    ) -> KeyboardShortcutSubmission:
        """立即接受一个任务或在执行器忙时拒绝。

        入参：已校验 shortcut、输入来源和物理布局 key index。
        返回：accepted 含 job id；执行中不排队而返回 busy；关闭后返回 closed。
        错误处理：线程池意外拒绝任务时恢复 active 状态并重新抛异常。
        副作用：接受时把任务提交到唯一 worker 线程。
        """

        with self._lock:
            if self._closed:
                return KeyboardShortcutSubmission(
                    accepted=False,
                    status=KeyboardShortcutSubmissionStatus.CLOSED,
                    message="keyboard shortcut scheduler is closed",
                )
            if self._active is not None:
                return KeyboardShortcutSubmission(
                    accepted=False,
                    status=KeyboardShortcutSubmissionStatus.BUSY,
                    message="another keyboard shortcut is still running",
                )
            job = _MutableKeyboardShortcutJob(
                job_id=str(uuid4()),
                source=source,
                key_index=key_index,
                shortcut=shortcut,
                submitted_at=datetime.now(UTC),
                started_at=None,
                finished_at=None,
                status=KeyboardShortcutJobStatus.QUEUED,
                target_pid=None,
                message="queued for keyboard shortcut worker",
            )
            self._active = job
            try:
                self._pool.submit(self._run_job, job)
            except Exception:
                self._active = None
                raise
        return KeyboardShortcutSubmission(
            accepted=True,
            status=KeyboardShortcutSubmissionStatus.ACCEPTED,
            job_id=job.job_id,
            message="keyboard shortcut accepted",
        )

    def capability(self) -> KeyboardShortcutCapability:
        """读取平台 capability 且不触发系统权限弹窗。

        入参：无。
        返回：executor 的当前 capability。
        错误处理：executor 异常按原样传播。
        副作用：可能调用只读系统权限检查。
        """

        return self._executor.capability()

    def request_permission(self) -> KeyboardShortcutCapability:
        """在显式 UI 请求路径调用平台授权 API。

        入参：无。
        返回：授权请求完成后的 capability。
        错误处理：executor 异常按原样传播。
        副作用：macOS 实现可能显示辅助功能授权提示。
        """

        return self._executor.request_permission()

    def diagnostics(self) -> dict[str, object]:
        """返回 capability、当前任务和近期终态任务。

        入参：无。
        返回：可 JSON 序列化前再经通用 model dumper 处理的 dict。
        错误处理：capability 检查异常按原样传播。
        副作用：只读 scheduler 状态和系统授权状态，不触发授权弹窗。
        """

        with self._lock:
            active = self._snapshot(self._active) if self._active is not None else None
            recent = [self._snapshot(job) for job in reversed(self._recent)]
        return {
            "capability": self.capability(),
            "active": active,
            "recent": recent,
        }

    def close(self) -> None:
        """停止接受新任务并等待当前最多 10 秒的序列结束。

        入参：无。
        返回：无显式返回值。
        错误处理：重复关闭安全无操作；worker 异常已在任务内部收敛。
        副作用：关闭线程池并等待正在投递的快捷键完成。
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _run_job(self, job: _MutableKeyboardShortcutJob) -> None:
        """在唯一 worker 中执行任务并归档结果。

        入参：当前 active job。
        返回：无显式返回值。
        错误处理：executor 任意异常转为 failed 状态和可读消息。
        副作用：调用平台 executor，随后更新 active/recent 内存状态。
        """

        with self._lock:
            job.started_at = datetime.now(UTC)
            job.status = KeyboardShortcutJobStatus.RUNNING
            job.message = "keyboard shortcut is running"
        try:
            result = self._executor.execute(job.shortcut)
            final_status = KeyboardShortcutJobStatus(result.status.value)
            target_pid = result.target_pid
            message = result.message
        except Exception as exc:  # noqa: BLE001 - 后台动作异常必须收敛到 status。
            final_status = KeyboardShortcutJobStatus.FAILED
            target_pid = None
            message = f"keyboard shortcut executor failed: {exc}"
        with self._lock:
            job.finished_at = datetime.now(UTC)
            job.status = final_status
            job.target_pid = target_pid
            job.message = message
            self._recent.append(job)
            if len(self._recent) > self._recent_limit:
                del self._recent[: len(self._recent) - self._recent_limit]
            if self._active is job:
                self._active = None

    @staticmethod
    def _snapshot(job: _MutableKeyboardShortcutJob) -> KeyboardShortcutJobSnapshot:
        """把加锁读取的可变 job 复制为 frozen 快照。

        入参：内部 job。
        返回：``KeyboardShortcutJobSnapshot``。
        错误处理：内部状态非法时由 Pydantic 报告。
        副作用：无。
        """

        return KeyboardShortcutJobSnapshot(
            job_id=job.job_id,
            source=job.source,
            key_index=job.key_index,
            shortcut=job.shortcut,
            submitted_at=job.submitted_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            status=job.status,
            target_pid=job.target_pid,
            message=job.message,
        )
