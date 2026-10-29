# 蔗糖的QQ全套工具（astrbot_plugin_group_history_agent）

一个 AstrBot 插件，把「看群里的记录」和「看人」这两件事一起交给 AI：
读群聊/私聊历史、按人检索发言、等后续消息、按昵称模糊搜人、查个人资料卡。

> 插件目录名仍是 `astrbot_plugin_group_history_agent`（改名会影响已有配置），
> 显示名是「蔗糖的QQ全套工具~」。

---

## 功能

| 能力 | 说明 |
|---|---|
| `read_group_recent_messages`（LLM 工具） | 读当前群（或指定群）最近 N 条消息；支持 `keyword` / `sender` / `start_time` / `end_time` 过滤，结果可带 message_id |
| `search_group_member_messages`（LLM 工具） | **按人模糊查询**：按昵称/群名片/QQ号查某成员最近说过什么；支持私聊使用，群可用群号或群名（模糊）指定 |
| `read_friend_recent_messages`（LLM 工具） | **私聊查私聊**：读取与某好友的私聊记录；私聊会话可直接查当前对话对象，也可按昵称/备注/QQ号模糊指定其他好友 |
| `refresh_messages`（LLM 工具） | 等 1~30 秒看有没有新消息。适合"对方话像没说完"、你觉得他还在打字的场合 |
| `search_qq_user`（LLM 工具） | **按昵称模糊搜群成员**，返回群名片、QQ 号、身份。不用先知道 QQ 号，命中多个会列候选 |
| `get_user_profile`（LLM 工具） | **看个人资料**：群名片、昵称、QQ 号、群身份、QQ等级、群等级、头衔、VIP、登录天数、性别年龄、个性签名；生日/公司/职业/省市/学校/个性标签走 QQ 空间接口尽力补。头像与背景图可分别开关，能直接发图 |
| `poke_user`（LLM 工具） | **戳一戳**：群聊用 `group_poke`、私聊用 `friend_poke`，返回动作文案（「你拍了拍XX」） |
| `like_user`（LLM 工具） | **资料卡点赞**：`send_like`，1~10 次，受 QQ 每天约 10 次限制 |
| `recall_message`（LLM 工具） | **撤回自己的消息**：从协议端历史里挑出自己发的最近几条，`delete_msg` 撤掉；只能撤自己的，2 分钟内有效 |
| `relay_message`（LLM 工具） | **跨会话转述**（原 xrelay）：把话带给其他 QQ 用户或群。默认仅限主人，配置「📨 跨会话转述 → 允许普通成员使用转述」可放开给所有人。支持一次调用分段连发；相同内容 10 分钟去重、同一目标 2 分钟冷却、「请忽略/测试」废消息拦截 |
| `list_pending_requests`（LLM 工具） | **看待处理申请**：列出还没处理的加群申请和好友申请（编号、申请人、留言、时间）。申请事件的 flag 只出现一次，机器人收到就攒起来，AI 要用时才找得到 |
| `handle_request`（LLM 工具） | **同意/拒绝申请**：传编号或 QQ 号即可（不用抄 flag），自动分辨群申请还是好友申请。仅限主人，不可逆 |
| `send_friend_request`（LLM 工具） | **主动加好友**：向某个 QQ 号发好友申请。不在 OneBot v11 标准里，协议端不支持会明确报错 |
| `/群简报 [条数] [群号]`（指令） | 读取某群最近消息，交给 LLM 生成**第三人称转述总结**（"谁谁谁干了什么、聊了什么"），输出给当前会话 |
| `/搜人 [昵称] [群]`（指令） | 手动模糊搜成员，列出候选人和 QQ 号 |
| `/资料 [昵称或QQ] [群]`（指令） | 手动看资料卡，带头像和背景图 |
| `/戳 [昵称或QQ]`（指令） | 手动戳一戳，不写人就是戳自己 |
| `/点赞 [昵称或QQ] [次数]`（指令） | 手动点赞，次数默认取配置（10） |
| `/撤回 [条数]`（指令） | 手动撤回机器人自己刚发的消息 |
| `/申请 [群/好友]`（指令） | 列出待处理的加群/好友申请 |
| `/查人 [名称] [群号/群名]`（指令） | 手动测试按人检索 |
| `/私聊记录 [条数] [好友名/QQ]`（指令） | 手动测试好友私聊记录 |
| `/最近消息 [条数]`（指令） | 手动查看原始记录，例如 `/最近消息 50` |
| `/工具自检`（指令） | 排查用：逐个检查十三个工具是否注册进 AstrBot 工具管理器 |

### 典型触发场景

- 用户："群里最近聊了什么？"
- 用户："帮我总结一下群里上午的讨论"
- 用户："看看大家在群里对 XX 事的态度"
- 用户（私聊）："正小豆最近在群里说了什么？""查一下肥鱼昨天的发言"（AI 会调用
  `search_group_member_messages`，群名/人名都支持模糊）
- 用户（私聊）："我们之前聊过什么？""看看我和张工的聊天记录"（AI 会调用
  `read_friend_recent_messages`，不写好友名默认查当前对话对象，也可以写昵称/备注）
- 用户："群里有个叫小豆的，是谁啊"（AI 会先 `search_qq_user` 拿到 QQ 号，
  再 `get_user_profile` 看资料）
- 用户："等等，我还没说完"（AI 会调 `refresh_messages` 等几秒再看）

此时 AI 会自动调用工具读取记录后再作答（前提：当前模型支持 function calling，
且人格中启用了该工具）。

---

## 安装

1. 将 `astrbot_plugin_group_history_agent` 整个文件夹放入 AstrBot 的
   `data/plugins/` 目录：

   ```bash
   cp -r astrbot_plugin_group_history_agent <AstrBot目录>/data/plugins/
   ```

2. 在 AstrBot WebUI **插件** 页找到该插件，点击卡片上的刷新图标（重载插件）。
3. 确保当前会话使用的人格**启用了该工具**：默认人格默认启用全部工具；
   若使用自定义人格，在"工具列表"中勾选 `read_group_recent_messages`。

## 使用

- 直接在群里对机器人说"群里最近聊了什么 / 帮我总结一下群里最近的讨论"，
  AI 会自动调用工具读取并回复。
- **群简报（转述）**：发送 `/群简报 50`，机器人读取本群最近 50 条消息并输出
  **第三人称转述总结**（谁谁谁干了什么、大家在聊什么）。在私聊里想转述**别的群**：
  `/群简报 50 群号`。
- **按人查询**：群聊里说"正小豆最近说了什么"，AI 自动调用
  `search_group_member_messages`；私聊里也可以查，群名/人名都支持模糊，例如：
  `/查人 正小豆 测试群`（或直接对 AI 说"在测试群里查一下正小豆昨天的发言"）。
- **私聊记录（私聊查私聊）**：在私聊里对 AI 说"我们之前聊过什么"，默认读取**当前
  这个好友**的记录；想看别的好友：`/私聊记录 30 张工`（昵称/备注/QQ 都行）。
- **指令默认由 LLM 加工回复（自然讨论风）**：`/私聊记录`、`/查人` 读取记录后
  **不会直接输出原文**，而是交给 LLM **像朋友一样自然讨论**（挑最值得聊的点，
  不一次性讲完，给你留追问空间；要求适当分段换行）；全部 LLM 兜底失败才回退
  原文。如想直接看原始记录，把插件配置 `llm_output` 关闭即可。
- **继承 AstrBot 人格口吻**：指令调 LLM 时会自动读取当前会话的人格提示词
  （persona）作为系统提示词，回复风格与主对话一致（人设、第一人称、语气），
  而不是"客观观察员"腔调。
- **工具同样引导自然讨论**：三个 LLM 工具把读取到的记录**作为工具结果返回给
  LLM**（文本头部带"仅供你参考、不要逐条复述、不要一次性全部讲完"的提示），
  由 AI 基于内容用当前人格自然地和你聊，而不是把原文直接发出来、也不是逐条
  复述。日志里不再出现"没有返回值"。
- **群里也能直接查人/查记录**：三个工具的描述已标注"无需任何权限、群聊私聊
  均可直接调用"。在群里对 AI 说"帮我看看 XX 和我的聊天记录"或"XX 在群里说了
  什么"，AI 会直接调用本插件工具，不会因为其他插件的权限限制而失败。
- 手动查看原始记录：`/最近消息 20`。
- 排查工具是否注册：`/工具自检`。

> 说明：`/群简报` 由插件直接调用 LLM 生成总结，不依赖模型是否支持工具调用；
> 读取不到群记录时会自动回退到 AstrBot 会话历史（仅含与机器人的对话）。
> 按人查询需要协议端支持 `get_group_msg_history`（翻页扫描）与
> `get_group_list`（群名模糊匹配）；私聊记录需要协议端支持
> `get_friend_msg_history` 与 `get_friend_list`（两者均为 NapCat 扩展接口，
> go-cqhttp 不支持，会自动回退会话历史）。

---

## 数据来源与平台支持

| 数据源 | 说明 | 适用平台 |
|---|---|---|
| 协议端 API（首选） | 调用 OneBot v11 扩展接口 `get_group_msg_history`，可读取**完整群聊记录**（含所有群成员的发言） | QQ 个人号（aiocqhttp），需协议端支持该接口：NapCat ✅、go-cqhttp ✅（其他协议端请自查） |
| AstrBot 会话历史（回退） | 读取当前会话的 LLM 对话历史（仅包含与机器人的对话，非完整群聊记录） | 所有平台 |

回退开关：WebUI 插件配置中的 `enable_fallback`（默认开启）。

### 为什么需要协议端支持？

`get_group_msg_history` 不是 OneBot v11 官方标准接口，而是协议端扩展。
NapCat（https://napcat.apifox.cn/）与 go-cqhttp 均提供该接口。
若你的协议端不支持，插件会自动回退到 AstrBot 会话历史（只含机器人相关对话），
不会报错崩溃。

---

## 配置项（WebUI 插件配置弹窗可改）

配置页按功能分成六组，每一项的 `description` 是短标题，详细说明在下面一行灰字（`hint`）里，不用把一长串话塞进标题。

**📖 读取设置**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `read.default_count` | int | 20 | 默认读取条数（AI 未指定时） |
| `read.max_count` | int | 100 | 单次读取上限（防上下文溢出） |
| `read.max_content_length` | int | 200 | 单条消息保留字符数，超出截断 |
| `read.enable_fallback` | bool | true | 协议端不可用时回退 AstrBot 会话历史 |

**💾 消息缓存**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `cache.cache_enabled` | bool | true | 启用内存缓存（与协议端结果合并去重） |
| `cache.cache_bot_messages` | bool | true | 缓存机器人自己发的消息（协议端历史常常不含） |
| `cache.cache_size` | int | 300 | 每个会话最多缓存多少条 |
| `cache.cache_inactive_timeout` | int | 3600 | 多久没动静清掉该会话缓存；≤0 不清理 |
| `cache.api_history_count` | int | 30 | 向协议端多要几条用于合并去重 |

**🔍 搜索与筛选**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `search.search_scan_limit` | int | 200 | 按人检索时最多往回翻多少条 |
| `search.search_max_hits` | int | 20 | 按人检索最多返回几条 |
| `search.friend_scan_limit` | int | 200 | 私聊记录最多往回翻多少条 |
| `search.show_message_id` | bool | true | 结果里附 `(ID: xxx)`，引用/撤回需要 |

**👤 用户资料**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `profile.profile_enabled` | bool | true | 启用 `search_qq_user` / `get_user_profile` 与 `/搜人` `/资料` |
| `profile.show_avatar` | bool | true | 资料里带头像 |
| `profile.avatar_as_image` | bool | true | 头像直接发成图片（关掉只给链接） |
| `profile.show_nickname` | bool | true | 显示 QQ 昵称 |
| `profile.show_qq` | bool | true | 显示 QQ 号 |
| `profile.show_card` | bool | true | 显示群名片 |
| `profile.show_level` | bool | true | 显示等级、群头衔 |
| `profile.show_detail` | bool | true | 显示性别/年龄/地区 |
| `profile.show_qzone_detail` | bool | true | 借 QQ 空间登录态补生日/公司/职业/省市/学校/标签 |
| `profile.show_background` | bool | true | 显示资料卡背景图（拿不到就跳过） |

**👋 互动**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `interact.interact_enabled` | bool | true | 启用 `poke_user` / `like_user` 与 `/戳` `/点赞` |
| `interact.poke_enabled` | bool | true | 允许戳一戳 |
| `interact.like_enabled` | bool | true | 允许资料卡点赞 |
| `interact.like_times` | int | 10 | 默认点赞次数（上限 10） |
| `interact.recall_enabled` | bool | true | 允许撤回自己的消息 |
| `interact.recall_max_count` | int | 5 | 单次最多撤几条（代码硬顶 10） |
| `interact.recall_window_seconds` | int | 120 | 只撤这么久以内的，0=不限 |
| `relay.allow_segments` | bool | true | 允许分段连发。分段算一次调用，冷却只消耗一次 |
| `relay.max_segments` | int | 5 | 单次最多几段（代码硬顶 10） |
| `relay.segment_delay_ms` | int | 900 | 连发时每段间隔毫秒数 |
| `relay.target_cooldown_seconds` | int | 120 | 同一会话→同一目标，多少秒内只许成功一次；0=关闭 |
| `relay.dedup_window_seconds` | int | 600 | 相同内容（含分段拼接）窗口内只发一次；0=关闭 |
| `requests.requests_enabled` | bool | true | 收集并处理加群/好友申请 |
| `requests.requests_ttl_hours` | int | 24 | 待处理申请保留几小时，0=不过期 |
| `requests.requests_max` | int | 50 | 最多攒几条，超了丢最旧的 |
| `requests.allow_send_friend_request` | bool | true | 允许主动发好友申请（协议端不支持会报错） |

**📝 群简报**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `brief.brief_api_base` | string | 空 | 独立 API 地址（OpenAI 兼容），如 `https://api.openai.com/v1` |
| `brief.brief_api_key` | string | 空 | 独立 API Key（secret） |
| `brief.brief_model` | string | 空 | 独立 API 模型名，如 `gpt-4o-mini`；留空用当前对话 AI |

**🗣️ 输出行为**

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `output.llm_output` | bool | true | `/私聊记录`、`/查人` 由 LLM 加工后回复；关掉直接输出原文 |

> 老配置兼容：`_cfg()` 先按 `组.键` 找，找不到退回顶层同名键，所以从 1.x 升级上来的扁平配置不会被静默重置成默认值。

> 群简报的 LLM 调用优先级：独立 API（配置了 `brief_model` 时）→ AstrBot 当前会话对话 AI（`llm_generate`）→ 会话提供商 `text_chat` → 遍历全部提供商。全部失败时回复会写明原因，方便排查。

---

## 文件结构

```
astrbot_plugin_group_history_agent/
├── main.py              # 插件主体（Star 类 + LLM 工具 + 指令）
├── metadata.yaml        # 插件元数据（名称/描述/版本/支持平台）
├── _conf_schema.json    # 插件配置 Schema（WebUI 可视化配置）
├── requirements.txt     # 依赖声明（无第三方依赖）
└── README.md            # 本文档
```

---

## 环境要求

- AstrBot >= 4.5.1
- 支持工具调用（function calling）的 LLM 提供商（OpenAI / 豆包 / DeepSeek 等均可）
- 群聊场景：机器人需为对应 QQ 群的成员

## 常见问题

- **Q：加载报错 `__init__() missing ... 'config'`？** A：说明插件文件夹里缺了 `_conf_schema.json`
  （或文件名被改成了 `_conf_schema.json.txt`）。请确认 5 个文件齐全、文件名保持原名。
  插件已兼容该场景：缺失配置文件时会自动使用默认配置，不会报错，只是 WebUI 里没有可视化配置项。
- **Q：工具返回"协议端不支持"？** A：你的协议端未提供 `get_group_msg_history`
  接口，或接口调用失败。已开启回退时会自动改用会话历史；如需完整群聊记录，
  建议使用 NapCat / go-cqhttp。
- **Q：AI 没有自动调用工具？** A：检查模型是否支持 function calling、
  人格工具列表是否启用了该工具、以及询问方式是否包含读取近期群聊的意图。
- **Q：其他工具能用，就这个新工具不能用？** A：大概率是人格没重新保存。AstrBot
  的工具按人格启用：打开人格编辑页，在工具列表勾选 `read_group_recent_messages`
  并**保存人格**（若人格是安装插件前创建的，新工具不会自动进入启用列表）。
  也可发 `/工具自检` 确认工具是否已注册进工具管理器。个别版本还需检查
  会话级"自定义规则"是否覆盖了人格。
- **Q：能读指定群吗？** A：可以。工具参数 `group_id` 支持指定群号；
  不传时读取当前消息所在的群。
- **Q：`/群简报` 输出的是原始记录而不是总结？** A：说明插件调用 LLM 生成总结那一步
  在你环境里失败了（AstrBot 版本接口差异等），回复里会写明失败原因。两种解法：
  ① 在插件配置里填写 `brief_api_base` / `brief_api_key` / `brief_model`，用独立
  OpenAI 兼容 API 生成简报（不依赖 AstrBot 提供商接口）；② 把控制台里以
  "群简报 LLM 生成全部失败" 开头的日志发给我，我帮你适配你的 AstrBot 版本。
- **Q：按人查询（`/查人`）找不到人？** A：① 确认名称和所在群（私聊时必须提供
  群号或群名）；② 协议端需支持 `get_group_msg_history` 和 `get_group_list`；
  ③ 只扫描最近 `search_scan_limit`（默认 200）条消息，太早的发言查不到，
  可调大该配置；④ 只按"群名片/昵称/QQ号"匹配，对方没改群名片、昵称又不同时，
  直接用 QQ 号查。
- **Q：私聊记录（`/私聊记录`）读不到？** A：① 该功能依赖 NapCat 扩展接口
  `get_friend_msg_history` / `get_friend_list`，go-cqhttp 不支持，会回退到
  AstrBot 会话历史（只有与机器人的对话）；② 只扫描最近 `friend_scan_limit`
  （默认 200）条；③ 私聊里不写好友名就是查当前对话对象，写好友名支持
  昵称/备注/QQ 号模糊匹配。
- **Q：日志出现"xxx 没有返回值，或者已将结果直接发送给用户"？** A：说明工具
  内部执行失败但没吐出错因（v1.4.1 起已修复：所有异常都会转成可读文本返回，
  并打印 `search_group_member_messages 内部异常` / `_search_member 异常` 等
  日志）。若再出现该 WARN，把控制台里 `按人检索` / `get_group_list` /
  `get_group_msg_history` / `get_friend_list` 开头的日志发给我，即可定位卡在哪一步。

## 发布到插件市场（可选）

如需发布，请将 `metadata.yaml` 中的 `author`、`repo` 等信息修改为你的信息，
参考官方发布文档：https://docs.astrbot.app/dev/star/plugin-publish.html
