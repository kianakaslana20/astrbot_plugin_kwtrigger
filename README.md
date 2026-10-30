# 关键词触发器 astrbot_plugin_kwtrigger

一句话：**你说了触发词，插件就去干活，不管模型记不记得。**

## 为什么需要它

靠"提示词叮嘱模型"去做某件事，本质上是在赌模型这一轮记不记得。上下文一长、指令一多，它就开始丢。

典型症状：你反复强调"念文案的时候要发语音"，前两轮有语音，聊几轮就只剩文字了 —— 因为发语音的条件写在提示词里，而不是写进消息处理流程里。

这个插件把条件写进流程：**触发词出现在消息里 → 直接执行动作**。模型参与不参与都行，甚至可以完全不参与。

## 两条钩子

| 时机 | 钩子 | 做什么 |
| --- | --- | --- |
| 请求模型之前 | `on_llm_request` | 扫消息，命中规则就执行动作：直发语音（可顺手跳过模型）、追加系统提示、回固定内容、提示必须调用某个工具、投递指令 |
| 回复发出去之前 | `on_decorating_result` | 把模型这轮回复念出来：替换成语音，或文字 + 语音 |

`skip_llm: true` 的规则在请求阶段就 `stop_event()` 并返回 `True`，这一轮**根本不会调模型**：省钱、零延迟、绝不跑偏。

## 内置规则（`builtin_rules: true` 时生效）

**① 念文案（直发语音，不进模型）**
- 触发词：`念这段` `念一遍` `念一下` `念给我听` `读一下这段` `读一遍` `读给我听` `朗读` `念出来`
- 取触发词之后的原文 → 合成语音 → 发出去 → 跳过模型，只发语音，120 字一段

> `念一遍：床前明月光，疑是地上霜` → 只收到一条语音，念的就是后面那句诗。

**② 语音回复（念模型回复）**
- 触发词：`语音回复` `发语音` `用声音回` `出声说` `念你的回复`
- 正常让模型回答，回答完把文字合成语音，文字 + 语音一起发

## 安装

1. 把整个 `astrbot_plugin_kwtrigger` 目录放进 AstrBot 插件目录：

   ```
   <AstrBot 实例>/core/data/plugins/astrbot_plugin_kwtrigger/
   ```

   Windows 上通常是：

   ```
   C:\Users\<你的用户名>\.astrbot_launcher\instances\<实例 ID>\core\data\plugins\
   ```

2. 重载插件：Dashboard → 插件管理 → 重载（或重启 AstrBot）。
3. Dashboard → 插件管理 → 关键词触发器 → 配置（默认开箱可用，只需确认有可用的 TTS 提供商）。
4. 聊天里发 `/kw 列表` 看规则，发 `/kw 测试 念一遍：你好` 试匹配。

不需要装任何额外依赖（只用标准库 + AstrBot 内置 API）。

## 前置条件：TTS 提供商

语音由 AstrBot 的 TTS 提供商合成，和内置语音是同一条路：

- Dashboard → 服务提供商 → 新增/确认一个"文本转语音"提供商（如 `breeze_tts_api`、`openai_tts_api`、`edge_tts` 等）；
- 在「配置 → 服务提供商 → 语音转文字/文本转语音」里选成默认。

插件合成失败时不会发空消息，会按 `fallback_to_text` 回退成普通文字回复。

## 配置项

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | true | 总开关 |
| `dry_run` | bool | false | 干跑：只记日志，不发语音不改写 |
| `admin_only` | bool | true | 仅管理员触发（Dashboard 网页聊天自动视为操作者） |
| `builtin_rules` | bool | true | 是否加载内置的两条规则 |
| `rules` | template_list | [] | 自定义规则表 |
| `tts_timeout` | int | 120 | 单段合成超时（秒） |
| `max_chars` | int | 2000 | 单次合成字数上限，超了截断 |
| `fallback_to_text` | bool | true | 合成失败时回退成普通回复 |
| `dedupe_existing_record` | bool | true | 回复里已经有语音就不重复合成 |
| `blacklist_sessions` | list | [] | 这些会话不生效 |
| `whitelist_sessions` | list | [] | 只在这些会话生效（空 = 全部） |
| `debug` | bool | false | 调试日志（命中/未命中都打） |

会话 ID 格式 `平台:消息类型:会话号`，例如 `aiocqhttp:FriendMessage:1000000001`、`aiocqhttp:GroupMessage:123456789`。

## 自定义规则字段

| 字段 | 说明 |
| --- | --- |
| `name` | 规则名 |
| `enabled` | 单条开关 |
| `priority` | 越大越先算（只影响命中顺序） |
| `action` | `tts` / `prompt` / `reply` / `tool` / `cmd` |
| `match` | `any`（任一触发词）/ `all`（全部出现）/ `regex_only`（只认正则）/ `always`（逢消息必中） |
| `keywords` | 触发词列表，逗号、顿号、竖线分隔都认 |
| `regex` | 可选正则，与触发词是"或"的关系 |
| `tts_source` | `message` 念触发词后原文 / `whole` 念整条消息 / `reply` 念模型回复 |
| `delivery` | `voice_only` 只发语音 / `voice_and_text` 文字+语音（`tts_source=reply` 时生效） |
| `chunk` | 长文分段字数，0 = 不切 |
| `skip_llm` | 发完语音就跳过模型 |
| `prompt_text` | `action=prompt` 时追加到 system prompt 的内容 |
| `reply_text` | `action=reply` 时的固定回复，可用 `{payload}` 占位 |
| `tool_name` | `action=tool` 时要求本轮必须调用的工具 |
| `cmd` | `action=cmd` 时要投出去的指令 |
| `sessions` / `platforms` | 只在这些会话/平台生效 |
| `admin_only` | 这条规则单独限定管理员 |

示例：把"读一下这篇文章"也变成必发语音、且不经过模型：

```json
{
  "name": "读文章",
  "enabled": true,
  "priority": 180,
  "action": "tts",
  "match": "any",
  "keywords": ["读一下这篇文章", "念这篇"],
  "tts_source": "message",
  "delivery": "voice_only",
  "chunk": 150,
  "skip_llm": true
}
```

示例：命中"叫醒我"就给模型塞一句强制提示（不拦模型）：

```json
{
  "name": "叫醒",
  "enabled": true,
  "action": "prompt",
  "keywords": ["叫醒我"],
  "prompt_text": "这一轮必须用语音回复，并在开头称呼主人。"
}
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `/kw` 或 `/kw 状态` | 看总开关、干跑、内置规则、规则条数 |
| `/kw 列表` | 列出当前生效的规则与触发词 |
| `/kw 开` / `/kw 关` | 本会话临时开关（重启后回到配置值） |
| `/kw 干跑` | 切换干跑，验证规则会不会命中而不真的发语音 |
| `/kw 重载` | 重新读配置 |
| `/kw 测试 <文本>` | 试匹配，告诉你命中哪条、取到什么内容 |

`关键词`、`触发器` 是别名。

## 和其它语音插件的共存

某些插件（例如靠模型在正文里写 `语音：…` 标签做语音的）会在同一轮也塞一条语音。插件检测到消息链里已有 `Record` 就跳过重复合成（`dedupe_existing_record`），不会出现两条一模一样的语音。

想彻底不要那些随机语音，就在那个插件的配置里把触发概率调成 0，只保留本插件由触发词决定的语音。

## 排错顺序

1. `/kw 列表` —— 规则在不在；
2. `/kw 测试 念一遍：xxx` —— 命不命中、取到哪段；
3. 开 `debug` 看日志 —— 有没有 `[kw]`；
4. 日志出现「没有可用的 TTS 提供商」→ 去配置里启用 TTS 提供商；
5. 群里不触发 → `admin_only` 默认只认管理员，想放开就关掉它。

## 目录结构

```
astrbot_plugin_kwtrigger/
├── main.py            插件主体（钩子 / 规则引擎 / TTS 投递 / 命令）
├── metadata.yaml      插件元信息
├── _conf_schema.json  配置界面（含规则表模板）
└── README.md
```
