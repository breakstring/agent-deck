# Agent Deck 状态型按键设计

本文记录 N4 Pro 主按键上展示 Codex 订阅、限额、token 和金额消耗的产品语义。它补充
`2026-06-24-agent-deck-key-surface-configuration-design.md`，只讨论状态型按键的内容与输入行为；
具体配置 GUI、renderer 和 schema 已按本文第一版口径接入；后续迭代仍以本文语义为边界。

## 背景

当前 Agent Deck 已经把 Codex quota 和 token/cost 用量画在 N4 Pro 底部 logical panel：

- `quota` panel 显示当前订阅、5 小时限制和周限制；未来可能出现月限制。
- `tokens` panel 显示 `today`、`week`、`month`、`all` 四个周期的金额和 token 总量，并在详情行展示
  input、output、reasoning 和 cache read。

主按键的物理尺寸和注意力模型不同于 touch bar。按键不适合承载多行报表，也不应该把 touch bar 变成
“当前按钮详情面板”。状态型按键应只表达一个一眼可读的主状态，并通过短按在有限状态之间切换。

## 设计原则

1. 状态型按键是用户可配置用途，不是系统强制占位。用户可以把任意 N4 Pro 主按键设成 App、URL、
   Agent slot、订阅限额或用量状态。
2. 按键只显示核心摘要。复杂明细仍留在 logical panel、桌面 GUI 或未来详情视图。
3. 短按只改变该按键自身的展示维度，不抢占 touch bar。
4. 一个按键只承担一种心智：订阅/限额按键回答“还能不能继续用”；用量按键回答“这个周期用了多少”。
5. 当数据源不可用时，按键保留用途身份并显示 stale/unavailable 状态，不清空成黑屏。

## 按键用途

### 订阅 / 限额状态

用途名称建议为 `quota_status`。

默认内容：

- 主身份：当前套餐短标签，例如 `ProLite`、`Biz`、`Ent`。
- 主数值：当前最紧张窗口的剩余比例。
- 周期角标：当前窗口，例如 `5h`、`week`、未来可能的 `month`。
- 状态颜色：根据剩余比例和是否耗尽表达正常、注意、危险。
- 底部左侧：复用 touch bar 的钥匙图标和可用 reset credit 数量，例如 `key icon + 2`。
- 底部右侧：当前窗口的重置时间。

“最紧张窗口”按剩余比例最小的可用窗口选择。当前 Codex app-server 的 primary/secondary 语义保持原样：

- primary：通常是 5 小时窗口。
- secondary：通常是周窗口。
- future：如果未来出现月窗口，作为同类窗口参与排序和切换。

按下行为：

- 短按：在可用 quota 窗口之间循环，例如 `auto/最紧张 -> 5h -> week -> month -> auto`。
- 长按：保留给手动刷新 quota；第一版如果硬件长按事件链不稳定，可暂不实现。

状态阈值建议：

- 剩余 `> 30%`：正常。
- 剩余 `10% - 30%`：注意。
- 剩余 `< 10%`：危险。
- 剩余 `0%` 或 app-server 明确返回 rate limit reached：耗尽。

注意：quota panel 目前展示的是剩余比例，即 `100 - used_percent`。按键应保持同一语义，不直接显示
app-server 原始 `usedPercent`，避免用户在 touch bar 和按键之间看到相反含义。

重置时间显示规则：

- 如果重置发生在本地当天，只显示时间，例如 `15:18`。
- 如果重置发生在其他日期，优先显示日期，例如 `07-14`；空间允许时可显示 `07-14 11:27`。
- 如果没有重置时间，显示 `--` 或隐藏右下角时间，不用占位长文案。

视觉规则：

- 5 小时窗口沿用 touch bar 的青色。
- 周窗口沿用 touch bar 的绿色。
- reset credit 沿用 touch bar 的金色。
- 不在 quota 按键底部绘制趋势曲线；该区域用于 reset credit 和 reset time。

### Token / 金额用量

用途名称建议为 `usage_summary`。

该用途把 token 和金额放在同一个按钮里。按钮只展示 logical panel 里 token/cost 区块左侧的两个主指标：

- Cost：当前周期金额。
- Total：当前周期总 token。

不展示以下细分项：

- input tokens
- output tokens
- reasoning output tokens
- cache read tokens
- model breakdown

周期固定与 touch bar 保持一致：

- `today`
- `week`
- `month`
- `all`

默认内容：

- 主数值：当前周期总 token，使用白色或接近白色，避免和 quota/金额语义混淆。
- 副数值：当前周期金额，使用 touch bar 里 reset credit 同系的金黄色。
- 周期角标：`day`、`week`、`month`、`all`。
- 数据身份：不再额外显示 `TOKENS` 字样；token 数值本身已经足够明确。
- 底部趋势：可选小型 sparkline，表达当前周期内的日用量走势。

按下行为：

- 短按：循环周期 `today -> week -> month -> all -> today`。
- 长按：保留给手动刷新 token/cost；第一版可暂不实现。

周期选择应作为运行态状态保留。也就是说，用户短按切到 `week` 后，按钮继续显示 week，直到用户再次切换、
daemon 重启或配置被重新应用。是否持久化最后周期可后续在 GUI 设置中决定。

预算语义：

- 没有用户预算时，token/cost 不强行使用红黄绿风险语义，因为系统不知道 `$28` 或 `39M tokens` 对用户
  是否偏高。
- 如果未来支持用户配置预算，可按预算使用比例显示注意/危险色。
- 没有预算时，颜色只表达数据类别和周期，不表达风险。

趋势 sparkline 规则：

- `today`：如果没有小时级数据，退化为最近 7 天日用量趋势；如果未来有小时级数据，再改为当天小时趋势。
- `week`：显示当前 ISO 周内逐日 token 或 cost 趋势，缺失日期按 0 补齐。
- `month`：显示当月逐日趋势，缺失日期按 0 补齐；点太多时可采样或压缩。
- `all`：主数值仍显示 all 总量，但 sparkline 不压缩全部历史；默认显示最近 30 天日用量趋势。
- 如果有效数据点少于 2 个，sparkline 退化为短横线或隐藏，避免给用户错误趋势暗示。
- 第一版 sparkline 默认使用 total tokens；如果后续用户更关心费用，可在配置中切换为 cost。

## 与 touch bar 的关系

状态型按键不主动切换 touch bar 内容。

原因：

- touch bar 已经承担 quota/tokens/message 等 logical panel 的独立切换。
- 如果每次按键都把 touch bar 变成“当前按钮详情”，用户需要理解两个焦点：当前硬件按键焦点和当前
  logical panel 焦点，复杂度会显著上升。
- 主按键应自洽：按下后改变自身状态，用户能立即从该按键看见结果。

如果未来要联动 touch bar，应作为可选高级行为，例如“短按切换按键周期，双击打开详情 panel”。第一版不做。

## 配置模型草案

后续配置 schema 可以在现有 key kind 基础上增加两个用途：

```text
kind: quota_status
quota_window: auto | primary | secondary | month

kind: usage_summary
usage_period: today | week | month | all
usage_primary_metric: cost | tokens
budget_usd: optional
budget_tokens: optional
```

第一版可以只落：

```text
kind: quota_status
quota_window: auto

kind: usage_summary
usage_period: today
```

周期切换先存在 daemon runtime，不必立即进入持久化配置。

## Runtime 缓存策略

第一版实现把数据缓存和图片缓存分层处理：

- quota 和 token/cost 数据继续复用 touch bar 面板使用的 daemon 快照。
- token/cost 的 `ccusage` 读取只由 daemon poller 按间隔刷新；按键按下和旋钮切换不触发 ccusage。
- 状态型按键图片使用 runtime 内存缓存，缓存 key 包含数据快照指纹和当前窗口/周期。
- 配置保存、quota/token 快照更新、状态键按下切换后，会按当前 layout 预热需要的 112x112 图片。
- 真实 N4 Pro renderer 每轮下发时优先从图片缓存取图；缓存 miss 才同步渲染一张内存图。
- 图片缓存有容量上限，避免 daemon 长时间运行时旧快照图片无限增长。

这意味着用户按下 `quota_status` 或 `usage_summary` 时，只改变该 key 的运行态展示维度；
不会抢占 touch bar，不会刷新外部数据，也不会把临时周期切换写回用户配置文件。

## 当前真实数据视觉样例基线

2026-07-09 本机 daemon 当前读到的数据可作为视觉样例基线：

```text
Quota:
Plan: ProLite
5h remaining: 51%
week remaining: 42%
reset credits: 2
5h reset: 15:18
week reset: 07-14

Usage:
today: 40.4M tokens, $29.56
week: 402.5M tokens, $331.45
month: 579.6M tokens, $485.12
all: 15.96B tokens, $9063.23
```

视觉样例应重点覆盖：

- `quota_status` 的 ProLite + 5h/week 两种窗口。
- `usage_summary` 的 day/week/month/all 四种周期。
- quota 底部 reset credit 与 reset time 的信息分配。
- usage 底部趋势 sparkline 的有数据、缺数据和数据不完整状态。
- 无预算状态下的中性用量色。
- quota 注意/危险状态与 usage 中性状态的区别。

## 非目标

- 不在主按键上显示 token 细分项。
- 不把 token 和金额拆成两个强制按键；用户未来可以通过配置放多个 usage_summary，但第一版默认一个按钮即可。
- 不让状态型按键默认打开浏览器账单页。
- 不做双击、三击等隐藏交互。
- 不把 touch bar 作为状态型按键的详情弹窗。
