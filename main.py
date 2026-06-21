"""
astrbot_plugin_gatekeeper v1.1.0
门禁插件 · 门禁系统 + 双模型验证 + 表情包缓存
配置同步使用官方 self.config / self.config.save_config() 方式
"""

import asyncio
import base64
import hashlib
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger
import astrbot.api.message_components as Comp

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

import json

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
    return default

def _save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)

GATE_FILE       = DATA_DIR / "gate_state.json"
MOJI_CACHE_FILE = DATA_DIR / "moji_cache.json"
INTERCEPT_FILE  = DATA_DIR / "intercept_log.json"
GATE_LOG_FILE   = DATA_DIR / "gate_log.json"


# ══════════════════════════════════════════════════════════════════════════════
# 数据层辅助
# ══════════════════════════════════════════════════════════════════════════════

def _load_gate()       -> dict: return _load_json(GATE_FILE, {})
def _save_gate(d)            : _save_json(GATE_FILE, d)
def _load_intercepts() -> list: return _load_json(INTERCEPT_FILE, [])
def _save_intercepts(d)      : _save_json(INTERCEPT_FILE, d)
def _load_gate_log()   -> list: return _load_json(GATE_LOG_FILE, [])
def _save_gate_log(d)        : _save_json(GATE_LOG_FILE, d)
def _load_moji()       -> dict: return _load_json(MOJI_CACHE_FILE, {})
def _save_moji(d)            : _save_json(MOJI_CACHE_FILE, d)

def _prune_month(lst: list) -> list:
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    return [x for x in lst if x.get("time", "") >= cutoff]

def _append_intercept(uid, name, original, generated):
    logs = _prune_month(_load_intercepts())
    logs.append({"uid": uid, "name": name, "original": original,
                 "generated": generated, "time": datetime.now().isoformat()})
    _save_intercepts(logs)

def _append_gate_log(uid, name, action, detail=""):
    logs = _prune_month(_load_gate_log())
    logs.append({"uid": uid, "name": name, "action": action,
                 "detail": detail, "time": datetime.now().isoformat()})
    _save_gate_log(logs)

def _moji_evict(cache: dict, limit: int) -> dict:
    if len(cache) <= limit:
        return cache
    sorted_keys = sorted(cache, key=lambda k: (cache[k]["use_count"], cache[k]["last_used"]))
    for k in sorted_keys[:len(cache) - limit]:
        del cache[k]
    return cache


# ══════════════════════════════════════════════════════════════════════════════
# 插件主体
# ══════════════════════════════════════════════════════════════════════════════

@register(
    "astrbot_plugin_gatekeeper",
    "夕小柠",
    "门禁插件 · 门禁系统 + 双模型验证 + 表情包缓存",
    "1.1.0",
)
class GatekeeperPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._observe_task: Optional[asyncio.Task] = None
        self._setup_routes()
        self._observe_task = asyncio.create_task(self._observe_loop())

    # ── 配置快捷读取 ──────────────────────────────────────────────────────────

    def _cfg(self, *keys, default=None):
        """从 self.config 按路径读取，支持嵌套 key"""
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k, default)
            else:
                return default
        return val if val is not None else default

    def _admins(self):
        return [str(a) for a in self.config.get("admins", [])]

    # ── WebUI 路由 ────────────────────────────────────────────────────────────

    def _setup_routes(self):
        P = "astrbot_plugin_gatekeeper"
        self.context.register_web_api(f"/{P}/config",        self._api_get_config,    ["GET"],    "获取配置")
        self.context.register_web_api(f"/{P}/config",        self._api_save_config,   ["POST"],   "保存配置")
        self.context.register_web_api(f"/{P}/gate_state",    self._api_gate_state,    ["GET"],    "门禁状态")
        self.context.register_web_api(f"/{P}/gate_action",   self._api_gate_action,   ["POST"],   "门禁操作")
        self.context.register_web_api(f"/{P}/intercept_log", self._api_intercept_log, ["GET"],    "拦截记录")
        self.context.register_web_api(f"/{P}/gate_log",      self._api_gate_log,      ["GET"],    "门禁日志")
        self.context.register_web_api(f"/{P}/moji_cache",    self._api_moji_get,      ["GET"],    "表情包缓存")
        self.context.register_web_api(f"/{P}/moji_clear",    self._api_moji_clear,    ["POST"],   "清空缓存")
        self.context.register_web_api(f"/{P}/gate_log_clear",      self._api_gate_log_clear,      ["POST"], "清空门禁记录")
        self.context.register_web_api(f"/{P}/intercept_log_clear", self._api_intercept_log_clear, ["POST"], "清空拦截记录")
        self.context.register_web_api(f"/{P}/gate_state_clear",    self._api_gate_state_clear,    ["POST"], "清除待处理记录")


    # ── API 处理函数 ───────────────────────────────────────────────────────────

    async def _api_get_config(self):
        from quart import jsonify
        return jsonify(dict(self.config))

    async def _api_save_config(self):
        from quart import request, jsonify
        try:
            body = await request.get_json(force=True, silent=True) or {}
            # 兼容 { config: {...} } 和裸 dict 两种格式
            data = body.get("config", body)
            for k, v in data.items():
                self.config[k] = v
            self.config.save_config()
            return jsonify({"ok": True, "success": True})
        except Exception as e:
            logger.error(f"[Gatekeeper] 保存配置失败: {e}")
            return jsonify({"ok": False, "msg": str(e)}), 500

    async def _api_gate_state(self):
        from quart import jsonify
        return jsonify(_load_gate())

    async def _api_gate_action(self):
        from quart import request, jsonify
        body = await request.get_json()
        await self._handle_admin_decision(str(body.get("uid", "")), body.get("action", ""))
        return jsonify({"ok": True})

    async def _api_intercept_log(self):
        from quart import jsonify
        return jsonify(_load_intercepts())

    async def _api_gate_log(self):
        from quart import jsonify
        return jsonify(_load_gate_log())

    async def _api_moji_get(self):
        from quart import jsonify
        return jsonify(_load_moji())

    async def _api_moji_clear(self):
        from quart import jsonify
        _save_moji({})
        return jsonify({"ok": True})

    async def _api_gate_log_clear(self):
        from quart import jsonify
        _save_gate_log([])
        return jsonify({"status": "ok"})

    async def _api_intercept_log_clear(self):
        from quart import jsonify
        _save_intercepts([])
        return jsonify({"status": "ok"})

    async def _api_gate_state_clear(self):
        """清除所有待处理/观察中状态的记录（不影响已生效的白名单/黑名单）"""
        from quart import jsonify
        gate = _load_gate()
        gate = {uid: info for uid, info in gate.items() if info.get("status") not in ("pending", "observe")}
        _save_gate(gate)
        return jsonify({"status": "ok"})


    # ── 消息入口 ──────────────────────────────────────────────────────────────


    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        uid      = str(event.get_sender_id())
        is_admin = uid in self._admins()
        is_group = bool(event.get_group_id())  # 官方判断方式：group_id 非空即群聊

        # 1. 表情包缓存预处理
        await self._moji_preprocess(event, is_group, is_admin)

        # 2. 门禁（只针对私聊陌生人，群聊从不触发门禁）
        if not is_group and self._cfg("gate", "enabled", default=True) and not is_admin:
            blocked = await self._gate_check(event, uid, self.context)
            if blocked:
                event.stop_event()
                return

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """双模型验证：回复生成后检查是否思维链泄露（官方 hook）"""
        uid      = str(event.get_sender_id())
        is_admin = uid in self._admins()
        is_group = bool(event.get_group_id())  # 官方判断方式：group_id 非空即群聊

        if is_admin:
            return
        if is_group and not self._cfg("dual_model", "group_enabled", default=True):
            return
        if not is_group and not self._cfg("dual_model", "private_enabled", default=True):
            return

        # 通过官方方式获取结果链
        result = event.get_result()
        if result is None:
            return

        chain = result.chain or []
        text  = "".join(getattr(c, "text", "") for c in chain)

        min_len = self._cfg("dual_model", "min_length", default=100)
        if len(text) < min_len:
            return

        judge_model = self._cfg("dual_model", "judge_model", default="")
        leaked = await self._judge_chain_leak(text, judge_model)
        if leaked:
            short_reply = await self._gen_short_reply(text, judge_model)
            name = getattr(event, "sender_name", uid)
            _append_intercept(uid, name, text, short_reply)
            result.chain = [Comp.Plain(short_reply)]
            logger.info(f"[Gatekeeper] 拦截思维链泄露 uid={uid}")

    # ── 门禁逻辑 ──────────────────────────────────────────────────────────────

    async def _gate_check(self, event: AstrMessageEvent, uid: str, context: Context) -> bool:
        wl = [str(x) for x in self.config.get("gate_whitelist", [])]
        bl = [str(x) for x in self.config.get("gate_blacklist", [])]
        if uid in wl: return False
        if uid in bl: return True

        gate     = _load_gate()
        name     = getattr(event, "sender_name", uid)
        msg_text = event.message_str or ""
        now      = time.time()
        g        = self.config.get("gate", {})

        if uid not in gate:
            gate[uid] = {
                "status": "pending", "name": name,
                "messages": [msg_text], "notified_admin": False,
                "observe_round": 0, "next_ask_at": 0, "created_at": now,
            }
            _save_gate(gate)
            asyncio.create_task(self._notify_admin(uid, name, gate[uid]["messages"], context))
            gate[uid]["notified_admin"] = True
            _save_gate(gate)
            return True

        info   = gate[uid]
        status = info["status"]
        if status == "allowed": return False
        if status == "blocked": return True

        # 缓存消息
        msgs  = info.get("messages", [])
        limit = g.get("cache_limit", 20)
        if len(msgs) < limit:
            msgs.append(msg_text)
        info["messages"] = msgs
        info["name"]     = name

        # 超阈值 → 请示消息
        threshold = g.get("trigger_threshold", 5)
        if len(msgs) >= threshold:
            pending_msg = g.get("pending_msg", "稍等一下，等熙熙同意哦～")
            try:
                await event.send(MessageChain().message(pending_msg))
            except Exception as e:
                logger.warning(f"[Gatekeeper] 发送请示消息失败: {e}")

        _save_gate(gate)
        return True

    async def _notify_admin(self, uid: str, name: str, messages: list, context: Context):
        admins = self._admins()
        if not admins:
            logger.warning("[Gatekeeper] 未配置管理员")
            return
        g          = self.config.get("gate", {})
        interval_h = g.get("observe_interval_hours", 2)
        msgs_text  = "\n".join(f"  [{i+1}] {m}" for i, m in enumerate(messages[:5]))
        text = (
            f"🔔 门禁请示\n"
            f"用户：{name}（{uid}）\n\n"
            f"消息：\n{msgs_text}\n\n"
            f"回复指令：\n"
            f"  同意 {uid}\n"
            f"  观察 {uid}（{interval_h}h后再问）\n"
            f"  拉黑 {uid}"
        )
        # 直接走 aiocqhttp 协议端原生 API send_private_msg，不依赖猜测 UMO 格式
        sent = False
        try:
            platform = context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                client = platform.get_client() if hasattr(platform, "get_client") else getattr(platform, "bot", None)
                if client:
                    for admin_id in admins:
                        try:
                            await client.api.call_action(
                                "send_private_msg",
                                user_id=int(admin_id),
                                message=text,
                            )
                            sent = True
                        except Exception as e:
                            logger.warning(f"[Gatekeeper] send_private_msg 失败 admin={admin_id}: {e}")
        except Exception as e:
            logger.warning(f"[Gatekeeper] 获取 aiocqhttp 平台失败: {e}")

        if not sent:
            logger.error(f"[Gatekeeper] 管理员通知发送失败，未能联系任何管理员！admins={admins}")

    async def _handle_admin_decision(self, uid: str, action: str):
        gate = _load_gate()
        name = gate.get(uid, {}).get("name", uid)

        if action == "allow":
            wl = [str(x) for x in self.config.get("gate_whitelist", [])]
            if uid not in wl:
                wl.append(uid)
            self.config["gate_whitelist"] = wl
            self.config.save_config()
            if uid in gate: gate[uid]["status"] = "allowed"
            _save_gate(gate)
            _append_gate_log(uid, name, "allow", "加入白名单")

        elif action == "observe":
            if uid in gate:
                g       = self.config.get("gate", {})
                interval = g.get("observe_interval_hours", 2)
                gate[uid]["status"]        = "observe"
                gate[uid]["next_ask_at"]   = time.time() + interval * 3600
                gate[uid]["observe_round"] = gate[uid].get("observe_round", 0) + 1
            _save_gate(gate)
            _append_gate_log(uid, name, "observe", f"第{gate[uid]['observe_round']}轮观察")

        elif action == "block":
            bl = [str(x) for x in self.config.get("gate_blacklist", [])]
            if uid not in bl:
                bl.append(uid)
            self.config["gate_blacklist"] = bl
            self.config.save_config()
            if uid in gate: gate[uid]["status"] = "blocked"
            _save_gate(gate)
            _append_gate_log(uid, name, "block", "加入黑名单")

    async def _observe_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                gate       = _load_gate()
                now        = time.time()
                max_rounds = self.config.get("gate", {}).get("observe_max_rounds", 3)
                changed    = False

                for uid, info in gate.items():
                    if info.get("status") != "observe": continue
                    if now < info.get("next_ask_at", 0): continue

                    rnd = info.get("observe_round", 0)
                    if rnd >= max_rounds:
                        info["status"] = "blocked"
                        bl = [str(x) for x in self.config.get("gate_blacklist", [])]
                        if uid not in bl: bl.append(uid)
                        self.config["gate_blacklist"] = bl
                        self.config.save_config()
                        _append_gate_log(uid, info.get("name", uid), "auto_block",
                                         f"观察{rnd}轮后自动拒绝")
                        changed = True
                    else:
                        asyncio.create_task(
                            self._notify_admin(uid, info.get("name", uid),
                                               info.get("messages", []), self.context)
                        )
                        interval = self.config.get("gate", {}).get("observe_interval_hours", 2)
                        info["observe_round"] = rnd + 1
                        info["next_ask_at"]   = now + interval * 3600
                        changed = True

                if changed:
                    _save_gate(gate)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Gatekeeper] observe_loop 异常: {e}")

    # ── 管理员 QQ 命令 ────────────────────────────────────────────────────────
    # 格式：同意/观察/拉黑 + 空格 + QQ号，例如 "同意 123456789"

    @filter.regex(r"^(同意|观察|拉黑)\s+(\d+)$")
    async def on_admin_cmd(self, event: AstrMessageEvent):
        uid_self = str(event.get_sender_id())
        if uid_self not in self._admins():
            return  # 非管理员静默忽略
        import re
        m = re.match(r"^(同意|观察|拉黑)\s+(\d+)$", (event.message_str or "").strip())
        if not m:
            return
        keyword, target_uid = m.group(1), m.group(2)
        action_map = {"同意": "allow", "观察": "observe", "拉黑": "block"}
        label_map  = {"allow": "✅ 已同意", "observe": "👀 已设为观察", "block": "🚫 已拉黑"}
        action = action_map[keyword]
        await self._handle_admin_decision(target_uid, action)
        event.stop_event()
        yield event.plain_result(f"{label_map[action]}：{target_uid}")

    # ── 双模型验证 ────────────────────────────────────────────────────────────

    async def _text_chat_with_fallback(self, prompt: str, model: str = "", **extra_kwargs) -> str:
        """调用 provider.text_chat，若指定模型失败则自动降级为主模型重试一次。返回空字符串表示彻底失败。"""
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("[Gatekeeper] 没有可用的 provider")
            return ""

        if model:
            try:
                resp = await provider.text_chat(prompt, model=model, **extra_kwargs)
                return (resp.completion_text or "").strip()
            except Exception as e:
                logger.warning(
                    f"[Gatekeeper] 指定模型 '{model}' 调用失败（{type(e).__name__}: {e}），"
                    f"自动降级使用主模型重试。请检查该 provider 的模型名称/权限是否正确。"
                )

        try:
            resp = await provider.text_chat(prompt, **extra_kwargs)
            return (resp.completion_text or "").strip()
        except Exception as e:
            logger.error(f"[Gatekeeper] 主模型调用也失败: {e}", exc_info=True)
            return ""

    async def _judge_chain_leak(self, text: str, model: str) -> bool:
        prompt = (
            "判断以下文本是否是AI思考过程的泄露（思维链/推理步骤/内心独白）。\n"
            "只回答 YES 或 NO，不要其他内容。\n\n"
            f"文本：\n{text}"
        )
        answer = await self._text_chat_with_fallback(prompt, model)
        return answer.upper().startswith("YES")

    async def _gen_short_reply(self, original: str, model: str) -> str:
        prompt = (
            "以下回复混入了AI思考过程。忽略思考部分，根据核心意思生成50字以内的简短自然回复。\n"
            "只输出回复内容。\n\n"
            f"原文：\n{original}"
        )
        result = await self._text_chat_with_fallback(prompt, model)
        return result or "嗯～"

    # ── 表情包缓存 ────────────────────────────────────────────────────────────

    async def _moji_preprocess(self, event: AstrMessageEvent, is_group: bool, is_admin: bool):
        m = self.config.get("moji", {})
        if is_group and not m.get("group_enabled", True): return
        if not is_group:
            if is_admin and not m.get("admin_enabled", False): return
            if not is_admin and not m.get("private_enabled", True): return

        chain = event.message_obj.message if event.message_obj else None
        if not chain:
            logger.debug("[Gatekeeper] moji: 消息链为空")
            return

        # 不识别开关：直接从消息链里过滤掉图片组件
        if m.get("no_read_enabled", False):
            event.message_obj.message = [c for c in chain if not _is_image_comp(c)]
            return

        cache       = _load_moji()
        limit       = m.get("cache_limit", 500)
        size_limit  = m.get("sticker_max_size", 0)  # 0 = 不限制，按字节判断表情包 vs 照片
        modified    = False
        desc_parts  = []

        img_comps = [c for c in chain if _is_image_comp(c)]
        if not img_comps:
            return  # 没有图片组件，静默跳过，不打日志（避免每条消息都刷屏）
        logger.debug(f"[Gatekeeper] moji: 检测到 {len(img_comps)} 个图片组件")

        for comp in img_comps:
            img_bytes = await _fetch_image_bytes(comp)
            if not img_bytes:
                logger.warning(f"[Gatekeeper] moji: 图片下载失败 comp={comp}")
                continue

            # 尺寸判断：超过阈值的视为照片，跳过缓存识别（省 token，避免把照片当表情包存）
            if size_limit > 0:
                w, h = _get_image_size(img_bytes)
                if w and h and max(w, h) > size_limit:
                    logger.debug(f"[Gatekeeper] moji: 图片 {w}x{h} 超过阈值 {size_limit}，当作照片跳过")
                    continue

            img_hash = hashlib.md5(img_bytes).hexdigest()

            if img_hash in cache:
                entry = cache[img_hash]
                entry["last_used"] = time.time()
                entry["use_count"] = entry.get("use_count", 0) + 1
                desc = entry["desc"]
                modified = True
                logger.debug(f"[Gatekeeper] moji: 缓存命中 hash={img_hash[:8]} desc={desc}")
            else:
                desc = await self._recognize_image(img_bytes, m.get("vision_model", ""))
                if desc:
                    cache[img_hash] = {"desc": desc, "last_used": time.time(), "use_count": 1}
                    cache    = _moji_evict(cache, limit)
                    modified = True
                    logger.info(f"[Gatekeeper] moji: 新识别并缓存 hash={img_hash[:8]} desc={desc}")
                else:
                    logger.warning(f"[Gatekeeper] moji: 识别返回空描述 hash={img_hash[:8]}")

            if desc:
                desc_parts.append(f"[表情包：{desc}]")

        if modified:
            _save_moji(cache)
            logger.debug(f"[Gatekeeper] moji: 缓存已保存，当前共 {len(cache)} 条")

        if desc_parts:
            try:
                chain.append(Comp.Plain("\n" + " ".join(desc_parts)))
            except Exception:
                pass

    async def _get_image_caption_provider(self):
        """获取 AstrBot 后台「默认图片转述模型」对应的 provider 对象（provider_settings.default_image_caption_provider_id）。
        找不到则返回 None，调用方应自行降级。"""
        try:
            astrbot_cfg = self.context.get_config()
            cap_id = (astrbot_cfg.get("provider_settings", {}) or {}).get("default_image_caption_provider_id", "")
            if not cap_id:
                return None
            provider = self.context.get_provider_by_id(cap_id)
            if not provider:
                logger.warning(f"[Gatekeeper] moji: 配置的图片转述 provider_id='{cap_id}' 未找到对应 provider")
            return provider
        except Exception as e:
            logger.warning(f"[Gatekeeper] moji: 读取图片转述 provider 配置失败: {e}")
            return None

    async def _recognize_image(self, img_bytes: bytes, model: str) -> str:
        b64    = base64.b64encode(img_bytes).decode()
        prompt = "用15字以内简洁描述这张表情包的内容和情绪。只输出描述。"
        img_kwargs = {"image_urls": [f"data:image/png;base64,{b64}"]}

        # 优先用 AstrBot 后台配置好的「默认图片转述模型」（最可靠，因为是专门为识图配置的 provider）
        cap_provider = await self._get_image_caption_provider()
        if cap_provider:
            try:
                resp = await cap_provider.text_chat(prompt, **img_kwargs)
                return (resp.completion_text or "").strip()
            except Exception as e:
                logger.warning(
                    f"[Gatekeeper] moji: 调用 AstrBot 默认图片转述模型失败（{type(e).__name__}: {e}），"
                    f"降级使用插件自配置的视觉模型。"
                )

        # 降级：走插件自己的 vision_model 配置（走统一降级方法，指定模型失败会再降级到主模型）
        return await self._text_chat_with_fallback(prompt, model, **img_kwargs)

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def terminate(self):
        if self._observe_task:
            self._observe_task.cancel()


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _is_image_comp(comp) -> bool:
    return type(comp).__name__ in ("Image", "ImageComponent") or (
        hasattr(comp, "url") and hasattr(comp, "file")
    )

def _get_image_size(img_bytes: bytes):
    """读取图片宽高，失败返回 (None, None)。用 Pillow，不依赖平台协议字段。"""
    try:
        from PIL import Image as PILImage
        import io
        with PILImage.open(io.BytesIO(img_bytes)) as im:
            return im.size  # (width, height)
    except Exception:
        return (None, None)

async def _fetch_image_bytes(comp) -> Optional[bytes]:
    try:
        import aiohttp
        url = getattr(comp, "url", None) or getattr(comp, "file", None)
        if not url: return None
        if not str(url).startswith("http"):
            p = Path(str(url))
            return p.read_bytes() if p.exists() else None
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.read() if r.status == 200 else None
    except Exception as e:
        logger.warning(f"[Gatekeeper] 获取图片失败: {e}")
        return None
