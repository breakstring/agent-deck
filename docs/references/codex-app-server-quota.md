# Codex App-Server Quota Notes

状态：参考记录，随 Codex app-server 协议变化动态更新

## 来源

Codex app-server 的 `account/rateLimits/read` 返回 `planType`、primary/secondary
quota window、credits 等信息。当前已观察到该接口使用行分隔 JSON-RPC 2.0：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"agent-deck","version":"0.1.0"}}}
{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}
```

`initialize` 需要 `clientInfo`。读取时不能假设下一行一定是目标响应，因为 app-server
可能先输出 notification，例如 `remoteControl/status/changed`；调用方必须持续读取到
目标 request id。

## PlanType

Codex 源码中的标准 PlanType 类型：

https://raw.githubusercontent.com/openai/codex/refs/heads/main/codex-rs/app-server-protocol/schema/typescript/PlanType.ts

当前文件记录的类型包括：

```text
free | go | plus | pro | prolite | team | self_serve_business_usage_based |
business | enterprise_cbp_usage_based | enterprise | edu | unknown
```

Agent Deck 当前展示映射分为两层：

- `short_label`：N4 Pro 底部虚拟 panel 的主标签，必须短，避免撑爆小屏布局。
- `display_name`：CLI/API/详情 panel 使用的完整展示名。

当前决策：

| planType | short_label | display_name | family |
| --- | --- | --- | --- |
| `free` | `Free` | `Free` | `free` |
| `go` | `Go` | `Go` | `go` |
| `plus` | `Plus` | `Plus` | `plus` |
| `pro` | `Pro` | `Pro` | `pro` |
| `prolite` | `ProLite` | `ProLite` | `pro` |
| `team` | `Team` | `Team` | `team` |
| `self_serve_business_usage_based` | `Biz` | `Business` | `business` |
| `business` | `Biz` | `Business` | `business` |
| `enterprise_cbp_usage_based` | `Ent` | `Enterprise` | `enterprise` |
| `enterprise` | `Ent` | `Enterprise` | `enterprise` |
| `edu` | `Edu` | `Education` | `education` |
| `unknown` | `Unknown` | `Unknown` | `unknown` |

`usage-based` 不在 N4 Pro 短标签中展示；如果需要解释，应放到详情 panel 或
CLI/API 的结构化字段里，而不是底部主视觉标签。
