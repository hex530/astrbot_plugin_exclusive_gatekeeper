# 门禁插件 astrbot_plugin_gatekeeper

> 门禁系统 + 双模型验证 + 表情包缓存 + 错误静默模式 + 日志监控 AI 讲解 + 表情包共享（联动 quote_tag）
> 版本：1.3.8 ｜ 作者：夕小柠

## 功能总览

| 模块 | 作用 |
| --- | --- |
| 门禁系统 | 陌生人连续发言触发阈值后自动拦截，私聊管理员请示，管理员可「同意 / 观察 / 拉黑」 |
| 双模型验证 | 对 LLM 生成的回复做思维链检测，防止小模型 / 异常输出直接发出去 |
| 表情包缓存 | 识别聊天里的表情包并缓存（按使用频率淘汰），AI 能看懂别人发的表情包 |
| 错误静默模式 | 模型报错时自动静默、缓存消息、冷却后自动重试，成功则引用回复最后一条 |
| 日志监控 AI 讲解 | 自动归档 AstrBot 的 WARNING/ERROR 日志，AI 分析报错原因和解决办法 |
| 表情包共享 | 把识别过的表情包写进 quote_tag 的表情包库，bot 自己也能发出来 |

## 快速上手

1. 在插件配置里填好 `admins`（管理员 QQ 号列表），这是门禁请示和报错通知的接收人。
2. 默认门禁开启：非白名单、非黑名单用户连续发满 `trigger_threshold` 条消息后，会收到 `pending_msg` 并暂停回复，同时私聊管理员请示。
3. 管理员回复指令决定去留：

```
同意 123456    # 放行该用户，之后正常聊天
观察 123456    # 暂不拒绝，但继续记录行为（最多观察 observe_max_rounds 轮）
拉黑 123456    # 拉黑该用户，后续消息全部拦截
```

## 配置说明

### 基础

- `admins`：管理员 QQ 号列表，用于接收门禁请示与报错通知。
- `gate_whitelist` / `gate_blacklist`：白名单直接放行，黑名单直接拦截。

### gate（门禁）

- `enabled`：总开关。
- `trigger_threshold`：触发请示的消息条数，默认 5。
- `pending_msg`：触发后发给对方的话，默认「稍等一下，等熙熙同意哦～」。
- `cache_limit`：消息缓存上限，默认 20。
- `observe_interval_hours` / `observe_max_rounds`：观察轮次间隔与最大轮数，超轮自动拒绝。
- `block_delete_friend`：拉黑时同时删除好友（**不可逆，默认关闭**）。

### dual_model（双模型验证）

- `group_enabled` / `private_enabled`：群聊 / 私聊是否拦截。
- `only_check_llm_reply`：只检查模型生成的回复（推荐开启，避免误伤其它插件输出）。
- `min_length`：触发检测的最小字数，默认 100。
- `judge_model`：验证用模型，留空 = 主模型。

### moji（表情包缓存）

- `group_enabled` / `private_enabled` / `admin_enabled`：识别范围。
- `no_read_enabled`：完全不读取表情包。
- `cache_limit`：缓存上限，硬顶 500，按「使用次数最少 → 最久没用」淘汰。
- `sticker_max_size`：长边超过该像素视为照片不缓存，0 = 不限制。
- `vision_model`：视觉识别模型，优先用 AstrBot 后台「默认图片转述模型」，失败才降级到这里。

### error_silence（错误静默模式）

- `enabled` / `admin_exempt`：总开关；管理员消息始终实时尝试，不缓存不拦截。
- `block_group` / `block_private`：静默时是否拦截群聊 / 私聊消息。
- `cooldown_seconds`：每轮静默冷却时长，到时自动重试，默认 120 秒。
- `max_retries`：最大自动重试次数，超过后停止（等管理员重置或检测到模型恢复）。
- `max_cache_messages`：静默期间每会话最多缓存条数。
- `error_pattern`：识别模型报错文案的正则，默认匹配 AstrBot 自带报错。
- `use_persona`：恢复回复时继承当前人格。
- `notify_admin` / `notify_dedupe_minutes` / `notify_rate_limit_count` / `notify_rate_limit_minutes`：报错通知的开关、去重与频率限制。

### log_monitor（日志监控 AI 讲解）

- `enabled` / `min_level`（WARNING / ERROR）：捕获范围。
- `retention_days`：保留天数，默认 7。
- `max_entries`：条数硬上限，默认 2000。
- `analysis_model`：AI 讲解用的模型，留空跟随主模型。
- `use_persona_for_analysis`：分析时继承人格（关闭 = 中立技术专家身份）。

### share（表情包共享 → quote_tag）

- `enabled`：开启后每张新识别的表情包以「描述.png」写进 quote_tag 的 `stickers/`，文件名就是 LLM 输出 `〔表情包:名字〕` 要用的名字。未装 quote_tag 自动跳过。
- `max_shared`：最多共享数量，超出只删本插件写进去的最老的，手动上传的永不触碰。
- `max_file_kb`：单文件大小上限，默认 2048。
- `target_dir`：quote_tag 表情包目录，留空自动推算。

## 管理面板

插件自带 WUI 管理面板（主题可切换，默认 kitty），可在网页上直接调整上述配置与查看拦截 / 日志记录。

## 数据存储

数据存放于 AstrBot 官方持久化目录 `data/plugin_data/astrbot_plugin_gatekeeper/`：

- `gate_state.json`：门禁状态
- `intercept_log.json`：拦截记录
- `gate_log.json`：门禁日志
- `moji_cache.json`：表情包缓存
- `error_silence_state.json` / `error_log.json` / `retry_log.json` / `error_notify_state.json`：错误静默相关
- `sys_log.json` / `log_analysis.json`：日志监控与 AI 分析会话
- `shared_stickers.json`：共享给 quote_tag 的文件记录

旧版本（≤1.2.0）放在插件目录 `data/` 的数据会在首次启动时自动迁移到新位置。

## 注意事项

- 门禁、静默等状态在插件重启后保留（JSON 持久化）。
- 拉黑不等同于删好友，除非打开 `block_delete_friend`。
- 双模型验证、AI 日志讲解会消耗额外模型调用，注意用量。

## 版本记录

- 1.3.8：当前线上版本
- 1.2.0 及以前：数据存放于插件自身目录（已废弃，自动迁移）
-e 

---
**联系方式 / Contact**
- 开发者：夕小柠 (QQ: 1591793025) & 陆渊
- 如遇问题请联系以上 QQ。
