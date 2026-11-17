# 礼貌性沉默（Polite Silence）

本插件是 [KitsuneiMomo/astrbot_plugin_polite_silence](https://github.com/KitsuneiMomo/astrbot_plugin_polite_silence) 的 MaiBot 移植版。它让 AI 在受到骚扰、谩骂或不当要求时，通过 `ignore_qq_user` 工具把对方加入沉默名单，插件随后在设定时长内忽略该用户的消息。

## 功能

- **工具化拒答**：模型直接调用 `ignore_qq_user` 工具，不再依赖提示词注入或 XML 标签解析。
- **静默拦截**：拒答期内，被拒答用户的普通消息在 `chat.receive.before_process` 阶段被中止，不进入聊天记录，也不会触发模型。
- **本地持久化**：拒答名单写入插件数据目录，插件重载或主程序重启后自动恢复。
- **作用域可选**：默认只影响触发拒答的那个聊天流；开启全局拒答后，该用户在所有群聊和私聊中都会被忽略。
- **白名单**：白名单用户不会被拒答，也不能通过工具或命令写入名单。
- **管理指令**：`/ignore` 手动拒答，`/ignorelist` 查看名单，`/unignore` 按 QQ 号或序号解除拒答。
- **时长收拢**：工具或管理员给出的时长都会被限制在配置的上下限之间；`-1` 表示永久拒答。
- **日志开关**：可在 WebUI 关闭插件的运行日志，只保留警告和错误。
- **可热更新**：修改配置后由主程序自动刷新，无需重载插件。

## 安装

1. 将本目录放入 MaiBot 的 `plugins/` 下。
2. 在 WebUI 插件页启用「礼貌性沉默」，或确认 `config.toml` 中 `[plugin].enabled = true`。
3. 主程序版本要求：MaiBot `1.3.x`，`maibot-plugin-sdk >= 2.6.0`。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `enabled` | bool | `true` | 插件总开关。 |
| `enable_log` | bool | `true` | 是否输出加载摘要、拦截记录与入站诊断日志。关闭后警告和错误仍会输出。 |
| `min_ignore_minutes` | int | `10` | 单次拒答的最短时长（分钟），工具省略时长时也使用该值。 |
| `max_ignore_minutes` | int | `1440` | 单次拒答的最长时长（分钟），默认 1 天。 |
| `whitelist_users` | list | `[]` | 不会被拒答的 QQ 号。也兼容英文逗号分隔字符串写法。 |
| `is_global_ignore` | bool | `false` | 开启后拒答在所有聊天中生效；关闭时仅限当前聊天流。 |

## 工作原理

| 能力 | 本移植版实现 |
| :--- | :--- |
| AI 主动拒答 | 注册核心工具 `ignore_qq_user`，由规划器在需要时调用 |
| 全局消息过滤 | `chat.receive.before_process` Hook 返回 `abort` |
| 管理员命令 | `permission="operator"` 的 `/ignore`、`/unignore`、`/ignorelist`，通过 `ctx.send.text` 回复 |

工具调用时，插件从工具上下文取得 `stream_id`、当前发言者与聊天流缓存；目标可以是 QQ 号，也可以是当前会话中发言者的昵称（插件用最近 50 条入站发言解析为 QQ 号）。使用 snowluma 适配器时，`user_info.user_id` 就是 QQ 号，因此拒答记录、白名单和命令目标都直接按 QQ 号匹配。

## 工具：`ignore_qq_user`

| 参数 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `target` | string | 否 | 要忽略的 QQ 号或当前会话用户昵称；留空表示当前发言者。 |
| `minutes` | integer | 否 | 忽略时长（分钟），`-1` 表示永久；留空或 `0` 使用 `min_ignore_minutes`。 |

工具对规划器直接可见（`core_tool`），无需先调用 `tool_search`。目标不是 QQ 号且无法从昵称解析时，工具会返回错误说明，不会写入名单。

## 管理指令

```text
/ignore 123456789        手动让 AI 忽略该 QQ 号，时长取 min_ignore_minutes
/ignore 123456789 60     手动忽略该 QQ 号 60 分钟（仍受上下限收拢）
/ignore 123456789 -1     永久忽略该 QQ 号
/ignorelist              查看当前拒答名单、作用域和剩余时长
/unignore 123456789      按 QQ 号解除该用户的全部拒答记录
/unignore 2              按 /ignorelist 显示的序号解除
```

`/ignore` 的目标必须是 QQ 号（纯数字，可带 `[]`）。分钟数必须是正整数或 `-1`：正整数会受 `min_ignore_minutes`/`max_ignore_minutes` 收拢，`-1` 表示永久且不受上下限约束，省略时使用 `min_ignore_minutes`。白名单用户和机器人自身不能被手动拒答。作用域与工具一致：`is_global_ignore` 开启时全局生效，否则仅限当前聊天流。拒答记录会立即写入插件数据目录，插件重载或主程序重启后自动恢复，可用 `/unignore` 随时解除。

三个命令均为操作员级别。请在 `config/bot_config.toml` 中配置：

```toml
[plugin]
permission = ["qq:你的QQ号"]
```

也可以通过 `[plugin].command_permissions` 为特定用户或聊天流补充放行规则。未被授权的用户执行命令时会被主程序拦截，不会进入模型链路。

拒答期内，精确匹配的 `/ignore`、`/unignore` 与 `/ignorelist` 会绕过拦截，交由主程序判定权限，确保管理员不会因为被 AI 拒答而无法自行解封。其他命令仍会被拦截。

## 与上游 AstrBot 版的差异

- 拒答决策从“提示词 + `<ignore>` 标签”改为模型工具调用，不再需要按概率注入提示。
- 作用域默认使用聊天流 `session_id`，而不是群号字符串；这样在多账号、多平台场景下不会串会话。全局拒答的行为不变。
- `whitelist_users` 改为列表类型，WebUI 可直接增删条目，同时兼容逗号分隔字符串。
- 管理命令使用 MaiBot 的操作员权限体系，不再依赖平台自带的管理员标识。

## 注意事项

- 拒答名单持久化在插件数据目录 `data/plugins/github.gyyxjsj.polite-silence/bans.json`（由 `ctx.paths.data_dir` 分配）。启动时自动恢复未过期记录并丢弃过期记录；文件损坏时记录警告并以空名单启动。
- 拒答作用域在写入时确定，之后修改 `is_global_ignore` 不会迁移已有记录；如需要改变作用域，请用 `/unignore` 解除后重新拒答。
- 被拒答用户的消息在入站阶段即被中止，因此不会出现在聊天记录里，模型也不会“看到”这些消息。
- 白名单只阻止新的拒答；修改白名单不会自动解除已经存在的记录，可用 `/unignore` 处理。
- **误判风险**：拒答决定来自模型调用，用户仍可能通过话术诱导模型误伤他人。建议把管理员和关键用户加入 `whitelist_users`，并适当调低 `max_ignore_minutes`。
- 多个实例共用同一插件数据目录时不会实时同步名单，后写入的实例会覆盖文件；请避免多个进程同时修改拒答名单。
- 上游 AstrBot 仓库未声明开源许可证。本仓库是面向 MaiBot 的移植实现，代码按 MIT 许可证发布（见 `LICENSE`），并在 README 顶部保留对上游作者 KitsuneiMomo 的署名；如上游作者有异议，请联系本仓库调整或下线。

## 开发与测试

纯逻辑位于 `helpers.py`，测试不依赖宿主运行时：

```bash
python -m pytest tests -q
```

建议使用主程序的虚拟环境运行，便于同时验证 SDK 集成。
