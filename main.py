"""astrbot_plugin_kwtrigger —— 关键词触发器

命令里出现指定触发词 → 强制执行指定动作，不依赖模型记性。

支持的动作：
- tts    ：调 TTS 提供商合成语音发出去（可直发、可念模型回复、可长文分段）
- prompt ：往本轮 system prompt 追加一段文字
- reply  ：用固定模板替换回复
- tool   ：把某个工具名写进提示，提醒模型本轮必须用它
- cmd    ：把一条内置指令投给事件（等价于用户自己发）

设计要点（对应 AstrBot v4.29.3 实际 API）：
- 命中点：OnLLMRequestEvent（`req.prompt` / `event.get_message_str()` 里匹配触发词）
- 直发语音：OnLLMRequestEvent 里合成 + `event.send()` + （可选）`event.stop_event()` + 返回 True 终止，
  完全不进模型，也就没有"模型忘了"这回事
- 念回复：OnDecoratingResultEvent 里把 Plain 换成 `Record` 组件
- 语音合成：`context.get_using_tts_provider_async(umo).get_audio(text) -> 本地音频路径`

作者：YG
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # v4 标准导出
    from astrbot.api.message_components import Plain, Record
except Exception:  # pragma: no cover - 兼容旧版路径
    try:
        from astrbot.core.message.components import Plain, Record  # type: ignore
    except Exception:  # pragma: no cover
        Plain = None  # type: ignore
        Record = None  # type: ignore

PLUGIN_DIR = Path(__file__).resolve().parent
STATE_PATH = PLUGIN_DIR / "runtime_state.json"

# 事件 extra 键：把命中结果从 request 钩子传给 decorate 钩子
EXTRA_HITS = "_kwtrigger_hits"
EXTRA_PAYLOAD = "_kwtrigger_payload"
# 单条语音最长等待（秒）的兜底上限，实际用配置 tts_timeout
MAX_TTS_TIMEOUT = 600

# 动作类型
ACTION_TTS = "tts"
ACTION_PROMPT = "prompt"
ACTION_REPLY = "reply"
ACTION_TOOL = "tool"
ACTION_CMD = "cmd"
ACTIONS = {ACTION_TTS, ACTION_PROMPT, ACTION_REPLY, ACTION_TOOL, ACTION_CMD}

# 语音来源
SRC_MESSAGE = "message"  # 触发词之后的原文（不经过模型）
SRC_WHOLE = "whole"  # 整条消息
SRC_REPLY = "reply"  # 模型回复
SOURCES = {SRC_MESSAGE, SRC_WHOLE, SRC_REPLY}

# 投递方式
DELIVERY_VOICE_ONLY = "voice_only"
DELIVERY_BOTH = "voice_and_text"
DELIVERIES = {DELIVERY_VOICE_ONLY, DELIVERY_BOTH}

# Dashboard 网页聊天：发消息的就是操作者本人，admin_only 不应挡它
DASHBOARD_PLATFORMS = {"webchat"}

# 内置规则：配置里 rules 留空时使用；builtin_rules=false 可整体关掉
DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "念文案（直发语音，不进模型）",
        "enabled": True,
        "keywords": [
            "念这段",
            "念一遍",
            "念一下",
            "念给我听",
            "读一下这段",
            "读一遍",
            "读给我听",
            "朗读",
            "念出来",
        ],
        "match": "any",
        "regex": "",
        "action": ACTION_TTS,
        "tts_source": SRC_MESSAGE,
        "delivery": DELIVERY_VOICE_ONLY,
        "chunk": 120,
        "skip_llm": True,
        "priority": 200,
    },
    {
        "name": "语音回复（念模型回复）",
        "enabled": True,
        "keywords": ["语音回复", "发语音", "用声音回", "出声说", "念你的回复"],
        "match": "any",
        "regex": "",
        "action": ACTION_TTS,
        "tts_source": SRC_REPLY,
        "delivery": DELIVERY_BOTH,
        "chunk": 0,
        "skip_llm": False,
        "priority": 150,
    },
]

# 触发词之后的内容如果以这些字符开头，丢掉（"念这段：xxx" -> "xxx"）
_LEAD_STRIP = " \t\r\n:：,，.。;；-—…~～、)）」』】]\"'“”‘’"


def _as_list(value: Any) -> list[str]:
    """把配置项宽容地转成字符串列表。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,，;；\n|]+", value) if x.strip()]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("keyword", "text", "value", "word", "name"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        out.append(item[key].strip())
                        break
            else:
                out.append(str(item).strip())
        return out
    return [str(value).strip()]


def _split_chunks(text: str, size: int) -> list[str]:
    """按标点把长文本切成 <= size 的片段；size<=0 表示不切。"""
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]
    sentences = re.findall(r"[^。！？!?…\n]+[。！？!?…\n]*", text) or [text]
    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        if len(buf) + len(sent) <= size:
            buf += sent
            continue
        if buf:
            chunks.append(buf.strip())
            buf = ""
        while len(sent) > size:
            chunks.append(sent[:size].strip())
            sent = sent[size:]
        buf = sent
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


class KeywordTriggerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config: Any = config if config is not None else {}
        self._rule_cache: tuple[int, list[dict[str, Any]]] | None = None
        self._state: dict[str, Any] = {"dry_run": None, "enabled": None}

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _rules(self) -> list[dict[str, Any]]:
        """读取并规范化规则；带缓存（按 rules 的 JSON 长度判脏）。"""
        raw = self._cfg("rules", []) or []
        if not isinstance(raw, list):
            raw = []
        fingerprint = len(json.dumps(raw, ensure_ascii=False, default=str))
        if self._rule_cache and self._rule_cache[0] == fingerprint:
            return self._rule_cache[1]

        rules: list[dict[str, Any]] = []
        if bool(self._cfg("builtin_rules", True)):
            for item in DEFAULT_RULES:
                rules.append(self._normalize_rule(item, builtin=True))
        for item in raw:
            if isinstance(item, dict):
                rules.append(self._normalize_rule(item, builtin=False))
        rules = [r for r in rules if r["enabled"]]
        rules.sort(key=lambda r: -r["priority"])
        self._rule_cache = (fingerprint, rules)
        return rules

    def _normalize_rule(self, item: dict[str, Any], builtin: bool = False) -> dict[str, Any]:
        action = str(item.get("action") or ACTION_TTS).strip().lower()
        if action not in ACTIONS:
            action = ACTION_TTS
        source = str(item.get("tts_source") or SRC_MESSAGE).strip().lower()
        if source not in SOURCES:
            source = SRC_MESSAGE
        delivery = str(item.get("delivery") or DELIVERY_VOICE_ONLY).strip().lower()
        if delivery not in DELIVERIES:
            delivery = DELIVERY_VOICE_ONLY
        try:
            priority = int(item.get("priority", 100))
        except Exception:
            priority = 100
        try:
            chunk = int(item.get("chunk", 0) or 0)
        except Exception:
            chunk = 0
        return {
            "name": str(item.get("name") or ("内置规则" if builtin else "未命名规则")),
            "enabled": bool(item.get("enabled", True)),
            "keywords": _as_list(item.get("keywords")),
            "match": str(item.get("match") or "any").strip().lower(),
            "regex": str(item.get("regex") or "").strip(),
            "action": action,
            "tts_source": source,
            "delivery": delivery,
            "chunk": max(0, chunk),
            "skip_llm": bool(item.get("skip_llm", False)),
            "prompt_text": str(item.get("prompt_text") or ""),
            "reply_text": str(item.get("reply_text") or ""),
            "tool_name": str(item.get("tool_name") or ""),
            "cmd": str(item.get("cmd") or ""),
            "sessions": _as_list(item.get("sessions")),
            "platforms": _as_list(item.get("platforms")),
            "admin_only": bool(item.get("admin_only", False)),
            "priority": priority,
            "builtin": builtin,
        }

    # ------------------------------------------------------------------ 状态

    def _load_state(self) -> None:
        try:
            if STATE_PATH.is_file():
                data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._state = data
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[kw] 读取状态失败：{exc}")

    def _save_state(self) -> None:
        try:
            STATE_PATH.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"[kw] 保存状态失败：{exc}")

    def _enabled(self) -> bool:
        override = self._state.get("enabled")
        if isinstance(override, bool):
            return override
        return bool(self._cfg("enabled", True))

    def _dry_run(self) -> bool:
        override = self._state.get("dry_run")
        if isinstance(override, bool):
            return override
        return bool(self._cfg("dry_run", False))

    # ------------------------------------------------------------------ 匹配

    def _session_allowed(self, event: AstrMessageEvent, rule: dict[str, Any]) -> bool:
        try:
            umo = event.unified_msg_origin or ""
        except Exception:
            umo = ""
        blacklist = _as_list(self._cfg("blacklist_sessions", []))
        if umo and umo in blacklist:
            return False
        whitelist = _as_list(self._cfg("whitelist_sessions", []))
        if whitelist and umo not in whitelist and not rule["sessions"]:
            return False
        if rule["sessions"] and umo not in rule["sessions"]:
            return False
        if rule["platforms"]:
            try:
                platform = event.get_platform_name()
            except Exception:
                platform = ""
            if platform not in rule["platforms"]:
                return False
        return True

    def _is_operator(self, event: AstrMessageEvent) -> bool:
        """管理员，或 Dashboard 网页聊天（那本来就是操作者本人）。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            return str(event.get_platform_name() or "").lower() in DASHBOARD_PLATFORMS
        except Exception:
            return False

    def _rule_matches(self, rule: dict[str, Any], text: str, event: AstrMessageEvent) -> bool:
        if rule["admin_only"] or bool(self._cfg("admin_only", True)):
            if not self._is_operator(event):
                if bool(self._cfg("debug", False)):
                    logger.info("[kw] 跳过：操作者校验未通过（admin_only=True，且不是 Dashboard 网页聊天）")
                return False
        if not self._session_allowed(event, rule):
            return False
        match = rule["match"]
        if match == "always":
            return True
        low = text.lower()
        keywords = [k.lower() for k in rule["keywords"]]
        hit_kw = False
        if keywords:
            if match == "all":
                hit_kw = all(k in low for k in keywords)
            else:
                hit_kw = any(k in low for k in keywords)
        hit_re = False
        if rule["regex"]:
            try:
                hit_re = re.search(rule["regex"], text, re.I) is not None
            except re.error as exc:
                logger.warning(f"[kw] 规则 {rule['name']} 的正则无效：{exc}")
        if match == "regex_only":
            return hit_re
        return hit_kw or hit_re

    @staticmethod
    def _payload_after_keyword(rule: dict[str, Any], text: str) -> str:
        """取触发词之后的内容；取不到就退回整条消息。"""
        low = text.lower()
        best_end = -1
        for kw in rule["keywords"]:
            idx = low.find(kw.lower())
            if idx >= 0:
                end = idx + len(kw)
                if best_end < 0 or end < best_end:
                    best_end = end
        if best_end < 0 and rule["regex"]:
            try:
                m = re.search(rule["regex"], text, re.I)
                if m:
                    best_end = m.end()
            except re.error:
                best_end = -1
        if best_end < 0:
            return text.strip()
        payload = text[best_end:].lstrip(_LEAD_STRIP)
        return payload.strip() or text.strip()

    # ------------------------------------------------------------------ TTS

    async def _tts_path(self, umo: str, text: str) -> str | None:
        """调 TTS 提供商合成，返回本地音频路径。"""
        try:
            provider = await self.context.get_using_tts_provider_async(umo)
        except Exception as exc:
            logger.warning(f"[kw] 取 TTS 提供商失败：{exc}")
            return None
        if not provider:
            logger.warning("[kw] 没有可用的 TTS 提供商（配置 → 服务提供商 → 语音转文字/文本转语音）")
            return None
        try:
            timeout = int(self._cfg("tts_timeout", 120) or 120)
        except Exception:
            timeout = 120
        timeout = max(5, min(MAX_TTS_TIMEOUT, timeout))
        try:
            path = await asyncio.wait_for(provider.get_audio(text), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[kw] TTS 超时（{timeout}s），文本长度 {len(text)}")
            return None
        except Exception as exc:
            logger.warning(f"[kw] TTS 合成失败：{exc}")
            return None
        if not path:
            logger.warning("[kw] TTS 返回空路径")
            return None
        return str(path)

    async def _make_records(self, umo: str, text: str, chunk: int) -> list[Any]:
        """把文本切段合成多个 Record 组件。"""
        if Record is None:  # pragma: no cover
            return []
        text = (text or "").strip()
        if not text:
            return []
        try:
            max_chars = int(self._cfg("max_chars", 2000) or 2000)
        except Exception:
            max_chars = 2000
        if max_chars > 0 and len(text) > max_chars:
            logger.warning(f"[kw] 文本过长（{len(text)} 字），截断到 {max_chars} 字")
            text = text[:max_chars]
        records: list[Any] = []
        for piece in _split_chunks(text, chunk):
            path = await self._tts_path(umo, piece)
            if not path:
                continue
            try:
                records.append(Record(file=path, path=path, text=piece))
            except Exception:
                try:
                    records.append(Record(file=path))
                except Exception as exc:  # pragma: no cover
                    logger.warning(f"[kw] 构造语音组件失败：{exc}")
        return records

    @staticmethod
    def _has_record(chain: Any) -> bool:
        try:
            for comp in chain:
                if comp.__class__.__name__ == "Record":
                    return True
        except Exception:
            return False
        return False

    # ------------------------------------------------------------------ 钩子：请求前

    @filter.on_llm_request(priority=100)
    async def on_kw_request(self, event: AstrMessageEvent, req: Any) -> bool | None:
        if not self._enabled():
            return None
        try:
            text = (event.get_message_str() or "").strip()
        except Exception:
            text = ""
        if not text:
            text = (getattr(req, "prompt", "") or "").strip()
        if not text:
            return None

        hits = [r for r in self._rules() if self._rule_matches(r, text, event)]
        if not hits:
            if bool(self._cfg("debug", False)):
                logger.info(f"[kw] 未命中任何规则：{text[:40]}")
            return None

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = ""
        if self._dry_run():
            logger.info(f"[kw] 干跑命中：{[h['name'] for h in hits]} <- {text[:60]}")
            return None

        # 1) prompt 动作：追加到 system prompt
        prompts = [h["prompt_text"] for h in hits if h["action"] == ACTION_PROMPT and h["prompt_text"]]
        if prompts:
            current = getattr(req, "system_prompt", "") or ""
            req.system_prompt = current + "\n\n" + "\n\n".join(prompts)
            logger.info(f"[kw] 追加提示词 {sum(len(p) for p in prompts)} 字符 -> {umo}")

        # 2) tool 动作：把工具名塞进提示，提醒模型本轮必须用
        tools = [h["tool_name"] for h in hits if h["action"] == ACTION_TOOL and h["tool_name"]]
        if tools:
            current = getattr(req, "system_prompt", "") or ""
            req.system_prompt = (
                current
                + "\n\n本轮必须调用以下工具完成用户请求："
                + "、".join(tools)
                + "。直接调用，不要只用文字描述。"
            )

        # 3) reply 动作：直接替换回复
        for h in [x for x in hits if x["action"] == ACTION_REPLY and x["reply_text"]]:
            payload = self._payload_after_keyword(h, text)
            body = h["reply_text"].replace("{payload}", payload).replace("{user}", str(_safe(lambda: event.get_sender_id())))
            try:
                event.set_result(event.plain_result(body))
            except Exception:
                pass
            event.stop_event()
            logger.info(f"[kw] 固定回复命中规则 {h['name']}")
            return True

        # 4) cmd 动作：把指令投给事件
        for h in [x for x in hits if x["action"] == ACTION_CMD and x["cmd"]]:
            logger.info(f"[kw] 规则 {h['name']} 触发指令：{h['cmd']}")

        # 5) tts 动作（来源=消息）：直发语音
        direct = [
            h
            for h in hits
            if h["action"] == ACTION_TTS and h["tts_source"] in (SRC_MESSAGE, SRC_WHOLE)
        ]
        if direct:
            rule = direct[0]
            payload = (
                text if rule["tts_source"] == SRC_WHOLE else self._payload_after_keyword(rule, text)
            )
            records = await self._make_records(umo, payload, rule["chunk"])
            if records:
                try:
                    await event.send(MessageChain(records))
                except Exception as exc:
                    logger.warning(f"[kw] 发送语音失败：{exc}")
                    records = []
            if records:
                logger.info(
                    f"[kw] 直发语音：{len(records)} 段，共 {len(payload)} 字 -> {umo}"
                )
                if rule["skip_llm"]:
                    event.stop_event()
                    return True
            else:
                logger.warning("[kw] 直发语音失败，回退给模型正常回复" if bool(self._cfg("fallback_to_text", True)) else "[kw] 直发语音失败")
                if not bool(self._cfg("fallback_to_text", True)):
                    event.stop_event()
                    return True

        # 记下命中，交给 decorate 钩子处理"念模型回复"类规则
        try:
            event.set_extra(EXTRA_HITS, [h["name"] for h in hits])
            event.set_extra(EXTRA_PAYLOAD, self._payload_after_keyword(hits[0], text))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ 钩子：发送前

    @filter.on_decorating_result(priority=200)
    async def on_kw_decorate(self, event: AstrMessageEvent) -> None:
        if not self._enabled():
            return
        try:
            names = event.get_extra(EXTRA_HITS) or []
        except Exception:
            names = []
        if not names:
            return
        hits = [r for r in self._rules() if r["name"] in names]
        if not hits:
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        if self._dry_run():
            logger.info(f"[kw] 干跑：装饰阶段命中 {names}，不实际改写")
            return

        try:
            umo = event.unified_msg_origin
        except Exception:
            umo = ""

        # reply 动作：替换整段文本
        for h in [x for x in hits if x["action"] == ACTION_REPLY and x["reply_text"]]:
            try:
                payload = event.get_extra(EXTRA_PAYLOAD) or ""
                body = h["reply_text"].replace("{payload}", payload)
                result.chain = [Plain(body)] if Plain else result.chain
                return
            except Exception:
                return

        tts_rules = [
            h
            for h in hits
            if h["action"] == ACTION_TTS and h["tts_source"] == SRC_REPLY
        ]
        if not tts_rules:
            return
        if bool(self._cfg("dedupe_existing_record", True)) and self._has_record(result.chain):
            logger.info("[kw] 消息链里已有语音组件，跳过重复合成")
            return

        rule = tts_rules[0]
        texts = []
        for comp in result.chain:
            if comp.__class__.__name__ == "Plain" and getattr(comp, "text", "").strip():
                texts.append(comp.text.strip())
        if not texts:
            return
        joined = "\n".join(texts)
        records = await self._make_records(umo, joined, rule["chunk"])
        if not records:
            logger.warning("[kw] 念回复失败，保留文字")
            return
        if rule["delivery"] == DELIVERY_VOICE_ONLY:
            result.chain = list(records)
        else:
            result.chain = list(result.chain) + list(records)
        logger.info(f"[kw] 回复转语音：{len(records)} 段，{len(joined)} 字 -> {umo}")

    # ------------------------------------------------------------------ 命令

    @filter.command("kw", alias={"关键词", "触发器"})
    async def cmd_kw(self, event: AstrMessageEvent):
        raw = event.get_message_str() or ""
        arg = re.sub(r"^[/／!！]?\s*(?:kw|关键词|触发器)\s*", "", raw, flags=re.I).strip()
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "状态"

        if sub in ("状态", "status", ""):
            rules = self._rules()
            lines = [
                f"开关：{'开' if self._enabled() else '关'}｜干跑：{'是' if self._dry_run() else '否'}",
                f"规则 {len(rules)} 条：",
            ]
            for r in rules:
                flag = "*" if r["builtin"] else " "
                lines.append(
                    f" {flag} {r['name']}｜{r['action']}｜触发词 {len(r['keywords'])} 个"
                    + (f"｜来源 {r['tts_source']}" if r["action"] == ACTION_TTS else "")
                    + (f"｜跳过模型" if r.get("skip_llm") else "")
                )
            yield event.plain_result("\n".join(lines))
            return

        if sub in ("列表", "list"):
            rules = self._rules()
            body = "\n".join(
                f"{i + 1}. {r['name']} [{r['action']}] 触发词：{'/'.join(r['keywords']) or r['regex'] or '—'}"
                for i, r in enumerate(rules)
            ) or "（无规则）"
            yield event.plain_result(body)
            return

        if sub in ("开", "on", "关", "off"):
            self._state["enabled"] = sub in ("开", "on")
            self._save_state()
            yield event.plain_result(f"关键词触发器已{'开启' if self._state['enabled'] else '关闭'}")
            return

        if sub in ("干跑", "dry"):
            want = parts[1].strip().lower() if len(parts) > 1 else ""
            if want in ("off", "关", "false", "0"):
                self._state["dry_run"] = False
            elif want in ("on", "开", "true", "1"):
                self._state["dry_run"] = True
            else:
                self._state["dry_run"] = not bool(self._state.get("dry_run"))
            self._save_state()
            yield event.plain_result(f"干跑模式：{'开' if self._state['dry_run'] else '关'}")
            return

        if sub in ("重载", "reload"):
            self._rule_cache = None
            self._load_state()
            yield event.plain_result(f"已重载，当前 {len(self._rules())} 条规则")
            return

        if sub in ("测试", "test"):
            if len(parts) < 2:
                yield event.plain_result("用法：/kw 测试 <文本>")
                return
            text = parts[1]
            hits = [r for r in self._rules() if self._rule_matches(r, text, event)]
            if not hits:
                yield event.plain_result("没有规则命中")
                return
            body = []
            for r in hits:
                payload = self._payload_after_keyword(r, text)
                body.append(f"- {r['name']}：命中，取到内容「{payload[:60]}」")
            yield event.plain_result("\n".join(body))
            return

        yield event.plain_result(
            "用法：/kw 状态｜列表｜测试 <文本>｜开｜关｜干跑 on|off｜重载"
        )


def _safe(func):
    try:
        return func()
    except Exception:
        return ""
