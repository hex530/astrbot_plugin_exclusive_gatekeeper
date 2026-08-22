"""
astrbot_plugin_gatekeeper v1.3.7
门禁插件 · 门禁系统 + 双模型验证 + 表情包缓存 + 错误静默模式 + 日志监控AI讲解
配置同步使用官方 self.config / self.config.save_config() 方式
"""

import asyncio
import base64
import hashlib
import inspect
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import AstrBotConfig
from astrbot.api import logger
import astrbot.api.message_components as Comp

# !! 重要 !!
# StarTools.get_data_dir() 依赖插件已经被 AstrBot 正确识别/注册的运行时上下文。
# 如果在模块刚被 import 时（也就是这里，纯模块顶层，Star 子类还没被实例化）就直接调用，
# 会直接抛异常，导致整个 main.py 都无法成功 import 完——AstrBot 因此读不到这个模块的
# metadata，插件会显示"加载失败"。所以这里只声明变量占位，真正的初始化挪到 _init_data_paths()
# 里，由 __init__ 在 Star 实例创建完成后第一时间调用。
DATA_DIR: Optional[Path] = None
GATE_FILE = MOJI_CACHE_FILE = INTERCEPT_FILE = GATE_LOG_FILE = None
ERROR_SILENCE_FILE = ERROR_LOG_FILE = RETRY_LOG_FILE = NOTIFY_STATE_FILE = None
SYS_LOG_FILE = LOG_ANALYSIS_FILE = SHARED_STICKER_FILE = None

# 旧版本（<=1.2.0）把数据存放在插件自身目录下的 data/ 文件夹里——这是 AstrBot 官方文档
# 明确指出的反模式："持久化数据请存储于 data 目录下，而非插件自身目录，防止更新/重装插件时数据被覆盖"。
# 插件自身目录在重装/更新时会被整个替换，所以旧版本的数据每次重装都会丢失。
_OLD_DATA_DIR = Path(__file__).parent / "data"

def _init_data_paths():
    """必须在 Star 实例的 __init__ 内调用（见上方说明）。幂等：重复调用不会重新执行。"""
    global DATA_DIR, GATE_FILE, MOJI_CACHE_FILE, INTERCEPT_FILE, GATE_LOG_FILE
    global ERROR_SILENCE_FILE, ERROR_LOG_FILE, RETRY_LOG_FILE, NOTIFY_STATE_FILE
    global SYS_LOG_FILE, LOG_ANALYSIS_FILE, SHARED_STICKER_FILE
    if DATA_DIR is not None:
        return
    DATA_DIR = StarTools.get_data_dir()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    GATE_FILE       = DATA_DIR / "gate_state.json"
    MOJI_CACHE_FILE = DATA_DIR / "moji_cache.json"
    INTERCEPT_FILE  = DATA_DIR / "intercept_log.json"
    GATE_LOG_FILE   = DATA_DIR / "gate_log.json"

    ERROR_SILENCE_FILE = DATA_DIR / "error_silence_state.json"  # 当前静默/已放弃的会话状态
    ERROR_LOG_FILE      = DATA_DIR / "error_log.json"            # 报错记录（永久日志，30天滚动）
    RETRY_LOG_FILE       = DATA_DIR / "retry_log.json"            # 重试记录
    NOTIFY_STATE_FILE    = DATA_DIR / "error_notify_state.json"   # 管理员通知去重/限流状态

    SHARED_STICKER_FILE = DATA_DIR / "shared_stickers.json"  # 本插件往 quote_tag 写过哪些表情包文件

    SYS_LOG_FILE      = DATA_DIR / "sys_log.json"       # 捕获到的 AstrBot 全局 WARNING/ERROR 日志
    LOG_ANALYSIS_FILE = DATA_DIR / "log_analysis.json"  # AI 报错分析对话会话（支持追问）

    _migrate_old_data()

def _migrate_old_data():
    """一次性迁移：把旧位置已有的文件搬到新的官方持久化目录，只在新位置还没有同名文件时才搬，
    避免重复迁移覆盖掉之后产生的新数据。"""
    if not _OLD_DATA_DIR.exists():
        return
    try:
        import shutil
        migrated = []
        for old_file in _OLD_DATA_DIR.glob("*.json"):
            new_file = DATA_DIR / old_file.name
            if not new_file.exists():
                shutil.copy2(old_file, new_file)
                migrated.append(old_file.name)
        if migrated:
            logger.info(f"[Gatekeeper] 数据迁移：已将旧版本数据迁移到持久化目录 {DATA_DIR}：{migrated}")
    except Exception as e:
        logger.error(f"[Gatekeeper] 数据迁移失败: {e}")

import json

# 各类外部调用（LLM provider / 协议端发送API）的超时保护，避免任何单次调用卡死导致
# 插件协程长期挂起（进而可能影响门禁锁、静默锁迟迟无法释放）。
PROVIDER_CALL_TIMEOUT = 60   # 调用LLM provider（视觉识别/思维链判断/错误重试探测等）超时
MOJI_CACHE_HARD_LIMIT = 500  # 表情包缓存条数硬顶：配置项填再大也不超过这个数
# 允许写入 quote_tag 表情包库的文件名安全过滤（Windows/Linux 都不合法的字符全部去掉）
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]')
SEND_CALL_TIMEOUT     = 15   # 调用协议端发送消息API（群/私聊/管理员通知）超时

def _deep_merge_into(target, src):
    """把 src 的内容递归合并进 target（原地修改）。
    只覆盖 src 里真正出现的键，src 没提到的键在 target 里保持不变——
    这样"局部保存"就不会误伤那些不在当前页面上的配置项。"""
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_merge_into(target[k], v)
        else:
            target[k] = v
    return target


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

def _prune_days(lst: list, days: int) -> list:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return [x for x in lst if x.get("time", "") >= cutoff]

def _prune_month(lst: list) -> list:
    return _prune_days(lst, 30)

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
    """超出上限时淘汰："使用次数最少"优先，次数相同则"最久没用过"优先。
    字段一律用 .get 取默认值：缓存文件可能来自旧版本、被手工编辑过，或写盘时被打断，
    缺字段时不能让整个表情包模块直接 KeyError 挂掉。"""
    if limit <= 0 or len(cache) <= limit:
        return cache
    sorted_keys = sorted(
        cache,
        key=lambda k: (
            (cache[k] or {}).get("use_count", 0) if isinstance(cache[k], dict) else 0,
            (cache[k] or {}).get("last_used", 0) if isinstance(cache[k], dict) else 0,
        ),
    )
    for k in sorted_keys[:len(cache) - limit]:
        del cache[k]
    return cache


# ── 错误静默模式 数据层 ────────────────────────────────────────────────────

def _load_error_silence() -> dict: return _load_json(ERROR_SILENCE_FILE, {})
def _save_error_silence(d)       : _save_json(ERROR_SILENCE_FILE, d)
def _load_error_log()    -> list: return _load_json(ERROR_LOG_FILE, [])
def _save_error_log(d)           : _save_json(ERROR_LOG_FILE, d)
def _load_retry_log()    -> list: return _load_json(RETRY_LOG_FILE, [])
def _save_retry_log(d)           : _save_json(RETRY_LOG_FILE, d)
def _load_notify_state() -> dict: return _load_json(NOTIFY_STATE_FILE, {"signatures": {}, "sent_log": []})
def _save_notify_state(d)        : _save_json(NOTIFY_STATE_FILE, d)

def _append_error_log(session_key, name, is_group, group_id, err_type, err_msg, signature):
    logs = _prune_month(_load_error_log())
    logs.append({
        "session_key": session_key, "name": name, "is_group": is_group, "group_id": group_id,
        "error_type": err_type, "error_text": (err_msg or "")[:500], "signature": signature,
        "notified_admin": False, "time": datetime.now().isoformat(),
    })
    _save_error_log(logs)

def _mark_error_log_notified(signature):
    logs = _load_error_log()
    for entry in reversed(logs):
        if entry.get("signature") == signature:
            entry["notified_admin"] = True
            break
    _save_error_log(logs)

def _append_retry_log(session_key, name, retry_count, result):
    logs = _prune_month(_load_retry_log())
    logs.append({
        "session_key": session_key, "name": name, "retry_count": retry_count,
        "result": result, "time": datetime.now().isoformat(),
    })
    _save_retry_log(logs)


# ── 日志监控 + AI 讲解 数据层 ──────────────────────────────────────────────

def _load_sys_log()      -> list: return _load_json(SYS_LOG_FILE, [])
def _save_sys_log(d)            : _save_json(SYS_LOG_FILE, d)
def _load_log_analysis() -> dict: return _load_json(LOG_ANALYSIS_FILE, {})
def _save_log_analysis(d)       : _save_json(LOG_ANALYSIS_FILE, d)


def _load_shared_stickers() -> dict: return _load_json(SHARED_STICKER_FILE, {})
def _save_shared_stickers(d)       : _save_json(SHARED_STICKER_FILE, d)


def _sticker_ext(img_bytes: bytes) -> str:
    """按文件头判断扩展名。GIF 必须存成 .gif，存成 .png 会丢掉动画。"""
    if img_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if img_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if img_bytes[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _safe_sticker_name(desc: str) -> str:
    """把视觉模型给的描述变成一个能当文件名、也能当 〔表情包:名字〕 里那个名字的字符串。

    quote_tag 的 StickerStore.sync() 会把 stickers/ 目录里新出现的文件按"文件名去扩展名"
    自动收录成表情包名字，所以这里的文件名就是将来 LLM 要输出的名字——
    必须短、口语化、不带路径分隔符。"""
    name = _UNSAFE_FILENAME.sub("", (desc or "").strip())
    name = re.sub(r"\s+", "", name)          # 去掉空格，名字里有空格不好匹配
    name = name.strip(". ")                  # 结尾的点在 Windows 上非法
    return name[:24] or "表情"


class _GKLogCaptureHandler(logging.Handler):
    """捕获 AstrBot 核心及所有插件的 WARNING/ERROR 级别日志（也就是控制台里"变黄""变红"的那些行），
    归档供插件自己的 WUI 浏览和 AI 分析使用。

    !! 关键安全约束 !!
    emit() 会被日志系统【同步】调用——也就是说，AstrBot 核心或任意插件每打一条 WARNING/ERROR，
    都会立刻、原地调用这个方法。这里绝不能做任何慢操作（文件IO、网络请求等），否则会拖慢
    全局所有日志调用方，在报错风暴时（比如模型大量超时）反而会让情况更糟。
    所以这里只做最轻量的内存 list.append()，真正的落盘交给插件里一个独立的定时刷盘任务。
    同时 emit() 内部绝不能调用 logger.warning/error 等方法，否则有递归触发自身的风险。

    AstrBot 的日志体系不一定会把记录传播到 Python 标准库那个匿名的 root logger，所以这个
    handler 可能会被同时挂载在 astrbot.api.logger 这个具体对象【和】root logger 上做双重兜底。
    如果两边恰好是同一条日志传播链，同一条记录可能被捕获两次——用 record.created（同一个
    LogRecord 对象在两个 handler 之间是完全相同的时间戳，不会受 emit() 调用先后影响）做去重。
    """
    _LEVEL_NORMALIZE = {
        "WARN": "WARNING", "WARNING": "WARNING",
        "ERRO": "ERROR", "ERROR": "ERROR",
        "CRIT": "CRITICAL", "CRITICAL": "CRITICAL",
        "INFO": "INFO", "DEBUG": "DEBUG",
    }

    def __init__(self, buffer: list, config_getter, seen_ids: set):
        super().__init__(level=logging.WARNING)  # 固定挂载级别为WARNING，更细的级别过滤在emit里做（支持运行时调整无需重建handler）
        self._buffer = buffer
        self._config_getter = config_getter  # 返回 log_monitor 配置 dict 的可调用对象
        self._seen_ids = seen_ids  # 多个 handler 实例共享同一个 set，用于跨挂载点去重

    def emit(self, record: logging.LogRecord):
        try:
            cfg = self._config_getter() or {}
            if not cfg.get("enabled", True):
                return
            min_level_name = cfg.get("min_level", "WARNING")
            min_level = getattr(logging, str(min_level_name).upper(), logging.WARNING)
            if record.levelno < min_level:
                return

            rid = hashlib.md5(f"{record.created}|{record.name}|{record.getMessage()}".encode()).hexdigest()[:12]
            if rid in self._seen_ids:
                return  # 同一条记录被多个挂载点重复捕获，去重跳过
            self._seen_ids.add(rid)
            if len(self._seen_ids) > 6000:
                self._seen_ids.clear()  # 防止去重集合无限增长，定期整体清空即可

            entry_time = datetime.now().isoformat()
            level_name = self._LEVEL_NORMALIZE.get(str(record.levelname).upper(), record.levelname)
            self._buffer.append({
                "id": rid,
                "time": entry_time,
                "level": level_name,
                "logger": record.name,
                "message": record.getMessage(),
            })
            # 防御性兜底：万一刷盘任务异常停滞，缓冲区也不能无限增长
            if len(self._buffer) > 5000:
                del self._buffer[:1000]
        except Exception:
            pass  # 绝不能让日志捕获本身的异常影响到日志系统或宿主程序


# ══════════════════════════════════════════════════════════════════════════════
# 插件主体
# ══════════════════════════════════════════════════════════════════════════════

@register(
    "astrbot_plugin_gatekeeper",
    "夕小柠",
    "门禁插件 · 门禁系统 + 双模型验证 + 表情包缓存 + 错误静默模式 + 日志监控AI讲解",
    "1.3.8",
)
class GatekeeperPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        _init_data_paths()  # 必须在 Star 实例创建之后才能调用 StarTools.get_data_dir()
        self._observe_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None
        self._log_flush_task: Optional[asyncio.Task] = None
        self._es_locks: dict = {}  # session_key -> asyncio.Lock，防止错误静默状态并发读写覆盖
        self._sys_log_buffer: list = []  # 日志捕获的内存缓冲区，由刷盘任务定期落盘
        self._last_recover_scan: float = 0.0   # 上次扫描 given_up 会话的时间戳（用于节流）
        # gate_state.json / moji_cache.json / shared_stickers.json 都是"读-改-写"，
        # 没有锁的话并发消息会互相覆盖（丢消息计数、丢表情包缓存）。
        self._gate_lock   = asyncio.Lock()
        self._moji_lock   = asyncio.Lock()
        self._share_lock  = asyncio.Lock()
        self._bg_tasks: set = set()            # 追踪 fire-and-forget 任务，避免被 GC 提前回收
        self._log_handlers: list = []  # 可能同时挂在多个 logger 对象上，统一记录以便 terminate 时清理
        try:
            self._setup_routes()
        except Exception as e:
            # WebUI 路由注册失败不应该让整个插件加载失败——门禁/双模型验证这些核心功能
            # 完全不依赖 WebUI，宁可界面用不了也要保证 QQ 侧照常工作。
            logger.error(f"[Gatekeeper] WebUI 路由注册失败，管理面板将不可用（其余功能不受影响）: {e}", exc_info=True)
        self._install_log_capture()
        # __init__ 里 create_task 要求当前有正在运行的事件循环。AstrBot 正常是在
        # async 上下文里实例化插件，但万一不是（或未来改了加载方式），这里抛 RuntimeError
        # 会导致整个插件加载失败——门禁这类核心功能不该被后台任务拖死，所以兜一下。
        try:
            self._observe_task   = asyncio.create_task(self._observe_loop())
            self._retry_task     = asyncio.create_task(self._retry_loop())
            self._log_flush_task = asyncio.create_task(self._log_flush_loop())
        except RuntimeError as e:
            logger.error(
                f"[Gatekeeper] 后台任务启动失败（当前没有运行中的事件循环）: {e}。"
                f"观察轮次请示、错误静默自动重试、日志落盘将不可用，其余功能正常。"
            )

    def _install_log_capture(self):
        """把日志捕获 handler 挂到日志系统上，捕获 AstrBot 核心及所有插件的 WARNING/ERROR
        （控制台里"变黄""变红"的那些行）。

        AstrBot 有自己的一套 LogManager/LogBroker 机制把日志推送给 WebUI 实时显示，不确定
        它具体是挂在 astrbot.api.logger 这个对象本身，还是会传播到 Python 标准库那个匿名的
        root logger——所以这里两边都挂，双重兜底，配合 handler 内部基于 record.created 的
        去重机制，即使两边恰好是同一条传播链导致同一条记录被捕获两次，也不会产生重复数据。"""
        seen_ids: set = set()
        targets = [("astrbot.api.logger", logger), ("root logger", logging.getLogger())]
        for label, target in targets:
            try:
                handler = _GKLogCaptureHandler(
                    self._sys_log_buffer,
                    lambda: self.config.get("log_monitor", {}),
                    seen_ids,
                )
                target.addHandler(handler)
                self._log_handlers.append((target, handler))
            except Exception as e:
                logger.error(f"[Gatekeeper] 日志监控挂载到 {label} 失败: {e}")
        if self._log_handlers:
            logger.info(f"[Gatekeeper] 日志监控已挂载（{len(self._log_handlers)} 处，捕获全局 WARNING/ERROR 日志）")

    async def _api_log_capture_test(self):
        """诊断用：手动打一条 WARNING，方便确认日志捕获机制是否真的生效。
        点击后刷新「日志」页，如果列表里出现这条记录，说明捕获机制本身工作正常。"""
        from quart import jsonify
        logger.warning("[Gatekeeper] 🧪 这是一条手动触发的测试日志，用于验证日志监控是否正常工作")
        return jsonify({"ok": True})

    def _spawn(self, coro):
        """派发一个"发后不理"的后台任务，并保持强引用。
        直接 asyncio.create_task() 不保存引用时，任务可能在执行完成前被 GC 回收，
        而且任务内抛出的异常只会变成一条 "Task exception was never retrieved" 警告。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _maybe_scan_given_up(self):
        """节流版的"唤醒已放弃会话"。

        旧版本在 on_decorating_result 里对**每一条**成功回复都无条件
        asyncio.create_task(self._recover_given_up_sessions())——群聊活跃时等于每条消息
        都要读一次 json 文件、开一个不被追踪的 task。这里改成最多每 30 秒扫一次。
        没有 given_up 会话时这个扫描本身就是纯浪费，而它晚 30 秒执行毫无影响。"""
        now = time.time()
        if now - self._last_recover_scan < 30:
            return
        self._last_recover_scan = now
        self._spawn(self._recover_given_up_sessions())

    def _get_es_lock(self, key: str) -> asyncio.Lock:
        """获取（或创建）某个会话专属的锁，确保同一会话的错误静默状态读写不会并发覆盖。
        不同会话用不同锁，互不影响，不会产生跨会话死锁。"""
        lock = self._es_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._es_locks[key] = lock
        return lock

    async def _safe_send(self, coro, timeout: float = SEND_CALL_TIMEOUT, what: str = ""):
        """带超时保护地 await 一个发送类协程，避免外部API卡住导致整个事件处理流程长期挂起。"""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[Gatekeeper] 发送超时（>{timeout}s）：{what}")
            return None
        except Exception as e:
            logger.warning(f"[Gatekeeper] 发送失败：{what}: {e}")
            return None

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
        # 注意：GET 和 POST 必须用**不同的路由**。同一个 rule 注册两个不同的 view function，
        # Quart 会抛 "View function mapping is overwriting an existing endpoint function"，
        # 而 _setup_routes() 在 __init__ 里没有 try/except，一抛就是整个插件加载失败。
        self.context.register_web_api(f"/{P}/config",      self._api_get_config,  ["GET"],  "获取配置")
        self.context.register_web_api(f"/{P}/config_save", self._api_save_config, ["POST"], "保存配置")
        self.context.register_web_api(f"/{P}/gate_state",    self._api_gate_state,    ["GET"],    "门禁状态")
        self.context.register_web_api(f"/{P}/gate_action",   self._api_gate_action,   ["POST"],   "门禁操作")
        self.context.register_web_api(f"/{P}/intercept_log", self._api_intercept_log, ["GET"],    "拦截记录")
        self.context.register_web_api(f"/{P}/gate_log",      self._api_gate_log,      ["GET"],    "门禁日志")
        self.context.register_web_api(f"/{P}/moji_cache",    self._api_moji_get,      ["GET"],    "表情包缓存")
        self.context.register_web_api(f"/{P}/moji_clear",    self._api_moji_clear,    ["POST"],   "清空缓存")
        self.context.register_web_api(f"/{P}/gate_log_clear",      self._api_gate_log_clear,      ["POST"], "清空门禁记录")
        self.context.register_web_api(f"/{P}/intercept_log_clear", self._api_intercept_log_clear, ["POST"], "清空拦截记录")
        self.context.register_web_api(f"/{P}/gate_state_clear",    self._api_gate_state_clear,    ["POST"], "清除待处理记录")
        # 错误静默模式
        self.context.register_web_api(f"/{P}/error_log",              self._api_error_log,              ["GET"],  "错误记录")
        self.context.register_web_api(f"/{P}/error_log_clear",        self._api_error_log_clear,        ["POST"], "清空错误记录")
        self.context.register_web_api(f"/{P}/retry_log",               self._api_retry_log,               ["GET"],  "重试记录")
        self.context.register_web_api(f"/{P}/retry_log_clear",         self._api_retry_log_clear,         ["POST"], "清空重试记录")
        self.context.register_web_api(f"/{P}/error_silence_state",     self._api_error_silence_state,     ["GET"],  "静默状态")
        self.context.register_web_api(f"/{P}/error_silence_reset",     self._api_error_silence_reset,     ["POST"], "重置单个静默会话")
        self.context.register_web_api(f"/{P}/error_silence_reset_all", self._api_error_silence_reset_all, ["POST"], "重置全部静默状态")
        # 日志监控 + AI 讲解
        self.context.register_web_api(f"/{P}/sys_log",            self._api_sys_log,            ["GET"],  "系统日志")
        self.context.register_web_api(f"/{P}/sys_log_clear",      self._api_sys_log_clear,      ["POST"], "清空系统日志")
        self.context.register_web_api(f"/{P}/log_analysis_list",  self._api_log_analysis_list,  ["GET"],  "AI分析记录")
        self.context.register_web_api(f"/{P}/log_analyze",        self._api_log_analyze,        ["POST"], "AI分析/追问")
        self.context.register_web_api(f"/{P}/log_analysis_delete",self._api_log_analysis_delete,["POST"], "删除分析会话")
        self.context.register_web_api(f"/{P}/log_capture_test",  self._api_log_capture_test,    ["POST"], "测试日志捕获")


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
            # 深合并而不是整块覆盖：页面上没有的字段必须保持原值。
            # 旧版本直接 self.config[k] = v，会把整个 dual_model / moji / log_monitor 子字典
            # 替换成页面提交的那份——于是在 AstrBot 原生配置面板里选好的 judge_model /
            # vision_model / analysis_model，只要在插件页面点一次保存就被抹掉了。
            _deep_merge_into(self.config, data)
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
        # get_json() 不带 force/silent 时，Content-Type 不对就直接抛 400，
        # 而且旧版本对 action 的值不做任何校验，传垃圾也照样返回 ok:True。
        body   = await request.get_json(force=True, silent=True) or {}
        uid    = str(body.get("uid", "")).strip()
        action = str(body.get("action", "")).strip()
        if not uid.isdigit():
            return jsonify({"ok": False, "msg": "uid 必须是纯数字的 QQ 号"}), 400
        if action not in ("allow", "observe", "block"):
            return jsonify({"ok": False, "msg": "action 只能是 allow / observe / block"}), 400
        try:
            await self._handle_admin_decision(uid, action)
        except Exception as e:
            logger.error(f"[Gatekeeper] 门禁操作失败 uid={uid} action={action}: {e}", exc_info=True)
            return jsonify({"ok": False, "msg": str(e)}), 500
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

    # ── 错误静默模式 API ──────────────────────────────────────────────────

    async def _api_error_log(self):
        from quart import jsonify
        return jsonify(_load_error_log())

    async def _api_error_log_clear(self):
        from quart import jsonify
        _save_error_log([])
        return jsonify({"ok": True})

    async def _api_retry_log(self):
        from quart import jsonify
        return jsonify(_load_retry_log())

    async def _api_retry_log_clear(self):
        from quart import jsonify
        _save_retry_log([])
        return jsonify({"ok": True})

    async def _api_error_silence_state(self):
        from quart import jsonify
        return jsonify(_load_error_silence())

    async def _api_error_silence_reset(self):
        """手动重置某一个会话的静默状态（立即解除拦截，丢弃缓存消息计数，不再自动重试旧错误）"""
        from quart import request, jsonify
        body = await request.get_json(force=True, silent=True) or {}
        key = str(body.get("session_key", ""))
        if not key:
            return jsonify({"ok": False, "msg": "缺少 session_key"}), 400
        async with self._get_es_lock(key):
            state = _load_error_silence()
            if key in state:
                del state[key]
                _save_error_silence(state)
        return jsonify({"ok": True})

    async def _api_error_silence_reset_all(self):
        from quart import jsonify
        _save_error_silence({})
        return jsonify({"ok": True})

    # ── 日志监控 + AI 讲解 API ────────────────────────────────────────────

    async def _api_sys_log(self):
        from quart import jsonify
        return jsonify(_load_sys_log())

    async def _api_sys_log_clear(self):
        from quart import jsonify
        _save_sys_log([])
        return jsonify({"ok": True})

    async def _api_log_analysis_list(self):
        from quart import jsonify
        return jsonify(_load_log_analysis())

    async def _api_log_analysis_delete(self):
        from quart import request, jsonify
        body = await request.get_json(force=True, silent=True) or {}
        aid = str(body.get("analysis_id", ""))
        analyses = _load_log_analysis()
        if aid in analyses:
            del analyses[aid]
            _save_log_analysis(analyses)
        return jsonify({"ok": True})

    async def _api_log_analyze(self):
        """开始一个新的 AI 报错分析会话，或者对已有会话追问。
        body: { analysis_id?: 已有会话则传，留空=新建;
                log_entry?: 新建时必填，{level, logger, message, ...};
                question?: 留空=用默认的"分析原因+解决办法"提示词 }"""
        from quart import request, jsonify
        body = await request.get_json(force=True, silent=True) or {}
        analysis_id = str(body.get("analysis_id") or "")
        question    = (body.get("question") or "").strip()
        log_entry   = body.get("log_entry") or {}

        cfg         = self.config.get("log_monitor", {})
        model_id    = cfg.get("analysis_model", "")
        use_persona = cfg.get("use_persona_for_analysis", False)

        analyses = _load_log_analysis()

        if analysis_id and analysis_id in analyses:
            session = analyses[analysis_id]
        else:
            snippet = f"[{log_entry.get('level','?')}] {log_entry.get('logger','?')}: {log_entry.get('message','')}"
            analysis_id = f"an_{int(time.time()*1000)}_{hashlib.md5(snippet.encode()).hexdigest()[:6]}"
            session = {
                "id": analysis_id, "created_at": datetime.now().isoformat(),
                "log_snapshot": snippet, "model": model_id, "use_persona": use_persona,
                "messages": [],
            }

        if not question:
            question = (
                "请分析以下 AstrBot 运行日志中的报错/警告内容，说明：\n"
                "1）这大概是什么类型的问题、可能的报错原因；\n"
                "2）有哪些可能的解决办法，按可能性从高到低排列。\n"
                "请用简洁、易懂的语言回答，不需要逐字复述日志原文。\n\n"
                f"日志内容：\n{session['log_snapshot']}"
            )

        session["messages"].append({"role": "user", "content": question, "time": datetime.now().isoformat()})

        # system_prompt：按配置决定是否继承当前人格，否则使用一个中立的技术专家身份
        system_prompt = ""
        if session.get("use_persona", use_persona):
            system_prompt = await self._get_persona_system_prompt(umo=None)
        if not system_prompt:
            system_prompt = "你是一个资深的 Python / AstrBot 插件开发专家，擅长根据日志快速定位问题原因并给出可行的解决办法。"

        provider = self.context.get_provider_by_id(model_id) if model_id else self.context.get_using_provider()
        if not provider:
            session["messages"].append({
                "role": "assistant", "content": "⚠️ 没有可用的模型，请检查「日志监控」里的分析模型配置",
                "time": datetime.now().isoformat(),
            })
            analyses[analysis_id] = session
            _save_log_analysis(analyses)
            return jsonify({"ok": False, "analysis_id": analysis_id, "messages": session["messages"], "msg": "没有可用的模型"})

        # 把已有对话历史（不含本轮刚追加的用户提问）转换成 context 传给 text_chat，从而支持多轮追问
        history_context = [
            {"role": m["role"], "content": m["content"]} for m in session["messages"][:-1]
        ]

        # !! 注意参数名 !! AstrBot 的 Provider.text_chat 收历史对话的参数是 `contexts`（复数）。
        # 旧版本写的是 `context=`（单数），它不会报错，而是被 **kwargs 静默吞掉——
        # 表现就是"多轮追问时模型完全不记得上一轮问过什么"。
        # 为兼容个别版本签名差异，这里先试 contexts，TypeError 再降级为不带历史。
        try:
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(question, contexts=history_context, system_prompt=system_prompt),
                    timeout=PROVIDER_CALL_TIMEOUT,
                )
            except TypeError as te:
                logger.warning(f"[Gatekeeper] text_chat 不接受 contexts 参数（{te}），本轮降级为不带历史提问")
                resp = await asyncio.wait_for(
                    provider.text_chat(question, system_prompt=system_prompt),
                    timeout=PROVIDER_CALL_TIMEOUT,
                )
            answer = (resp.completion_text or "").strip() or "（模型没有返回有效内容，可换个模型再试试）"
        except asyncio.TimeoutError:
            answer = f"⚠️ 分析请求超时（>{PROVIDER_CALL_TIMEOUT}s），请稍后重试"
        except Exception as e:
            logger.error(f"[Gatekeeper] 日志分析调用失败: {e}")
            answer = f"⚠️ 分析失败：{type(e).__name__}: {e}"

        session["messages"].append({"role": "assistant", "content": answer, "time": datetime.now().isoformat()})
        analyses[analysis_id] = session
        _save_log_analysis(analyses)

        return jsonify({"ok": True, "analysis_id": analysis_id, "messages": session["messages"]})


    # ── 消息入口 ──────────────────────────────────────────────────────────────


    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        # 0a. 防御性检查：过滤掉没有真实发送者的事件（例如 QQ 名片点赞等通知类事件，
        #     这类事件没有真实发送者，get_sender_id() 会返回 None，绝不应触发门禁等任何逻辑）
        sender_id_raw = event.get_sender_id()
        if not sender_id_raw or str(sender_id_raw).strip().lower() in ("", "none"):
            logger.debug("[Gatekeeper] 检测到无有效发送者的事件（如点赞等通知），已忽略")
            return

        # 0b. 防御性检查：发送者ID有效，但消息内容完全为空（无文本也无任何消息链组件）。
        #     例如"加好友"等请求/通知类事件，有时会被适配器包装成一条空消息传入此处，
        #     真实的聊天消息（哪怕只发一个表情/图片/语音）必然在消息链里留下至少一个组件，
        #     绝不可能同时"无文本+无消息链"，所以这个组合是判断"非真实消息"的可靠依据。
        chain = event.message_obj.message if event.message_obj else None
        has_text  = bool((event.message_str or "").strip())
        has_chain = bool(chain)
        if not has_text and not has_chain:
            logger.debug(f"[Gatekeeper] 检测到无实际消息内容的事件（如加好友等通知），已忽略 uid={sender_id_raw}")
            return

        uid      = str(sender_id_raw)
        is_admin = uid in self._admins()
        is_group = bool(event.get_group_id())  # 官方判断方式：group_id 非空即群聊

        # 1. 错误静默拦截（优先级最高：命中静默中的会话，直接缓存消息并拦截，不再往下执行任何逻辑）
        if await self._error_silence_intercept(event, uid, is_admin, is_group):
            event.stop_event()
            return

        # 2. 门禁（只针对私聊陌生人，群聊从不触发门禁）
        #    !! 顺序很重要 !! 门禁必须排在表情包识别**之前**。
        #    旧版本先跑 _moji_preprocess 再查门禁，导致还没通过审核的陌生人，
        #    发过来的图片已经被下载并送进视觉模型了——既白烧额度，也是一个可以被人刷的口子。
        if not is_group and self._cfg("gate", "enabled", default=True) and not is_admin:
            blocked = await self._gate_check(event, uid, self.context)
            if blocked:
                event.stop_event()
                return

        # 3. 表情包缓存预处理（走到这里说明这条消息确实会被处理，才值得花钱识图）
        await self._moji_preprocess(event, is_group, is_admin)

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """兜底注入表情包描述。

        _moji_preprocess 已经把描述写进 event.message_str 了，但 AstrBot 在不同版本/不同
        流程（分段回复、上下文压缩、其它插件改写 prompt 等）里，req.prompt 不一定就等于
        当时那个 message_str。这里再检查一次：如果描述还没出现在 req.prompt 里就补上，
        已经在里面了就什么都不做（避免重复注入让模型看到两遍）。"""
        # 顺手打个标记：这条事件确实发起过 LLM 请求。
        # 双模型验证只应该检查"模型生成的回复"，靠这个标记区分——见 on_decorating_result。
        try:
            setattr(event, "_gk_from_llm", True)
        except Exception:
            pass

        desc_text = getattr(event, "_gk_moji_desc", "")
        if not desc_text:
            return
        try:
            prompt = getattr(req, "prompt", "") or ""
            if desc_text not in prompt:
                req.prompt = (prompt + "\n" + desc_text).strip()
                logger.debug("[Gatekeeper] moji: 已通过 on_llm_request 补注入表情包描述")
        except Exception as e:
            logger.debug(f"[Gatekeeper] moji: on_llm_request 注入失败: {e}")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """双模型验证：回复生成后检查是否思维链泄露（官方 hook）
        同时承担错误静默模式的"报错检测"职责（检测到报错文案则记录+进入静默，不修改原文）"""
        sender_id_raw = event.get_sender_id()
        if not sender_id_raw or str(sender_id_raw).strip().lower() in ("", "none"):
            return

        uid      = str(sender_id_raw)
        is_admin = uid in self._admins()
        is_group = bool(event.get_group_id())  # 官方判断方式：group_id 非空即群聊

        # 通过官方方式获取结果链
        result = event.get_result()
        if result is None:
            return

        chain = result.chain or []
        # `or ""` 是必要的：组件的 text 属性存在但为 None 时，"".join 会直接 TypeError
        text  = "".join((getattr(c, "text", "") or "") for c in chain)

        # ── 错误静默检测（优先于双模型验证）──
        es_handled = await self._check_and_handle_llm_error(event, uid, is_admin, is_group, text)
        if es_handled:
            return  # 已记录为报错并处理（不修改原始报错文案，让它正常发出）

        # 正常成功的回复：顺手尝试唤醒因报错次数耗尽而被放弃(given_up)的会话——
        # 说明模型大概率已经恢复正常，给它们一次新的重试机会，而不是永远等下一次定时轮询。
        # 已节流到最多 30 秒一次，避免每条消息都读一遍文件。
        self._maybe_scan_given_up()

        if is_admin:
            return
        if is_group and not self._cfg("dual_model", "group_enabled", default=True):
            return
        if not is_group and not self._cfg("dual_model", "private_enabled", default=True):
            return

        # 只检查"模型生成的回复"。
        # 旧版本对**任何**结果都做思维链判定，没区分来源——于是别的插件输出的长帮助文本、
        # 报告、菜单，只要超过 100 字就会被送去判定，判错还会被整段替换成一句「嗯～」。
        # _gk_from_llm 由 on_llm_request 钩子打上：这条事件确实发起过 LLM 请求才算。
        # 找不到标记时**跳过检查**（宁可漏检也不误伤）；如果你的环境里这个钩子不触发，
        # 可以把配置项 only_check_llm_reply 关掉，退回"检查所有回复"的旧行为。
        if self._cfg("dual_model", "only_check_llm_reply", default=True):
            if not getattr(event, "_gk_from_llm", False):
                logger.debug("[Gatekeeper] 双模型验证：本次回复不是 LLM 生成的，跳过检查")
                return

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
        """持锁执行：gate_state.json 是"读-改-写"，同一个陌生人快速连发多条消息时，
        无锁会导致两个协程读到同一份状态、后写的覆盖先写的 → 消息计数丢失、请示重复触发。
        锁内唯一的 await 是发一条请示消息（有 10s 超时兜底），不会长时间占用。"""
        async with self._gate_lock:
            return await self._gate_check_locked(event, uid, context)

    async def _gate_check_locked(self, event: AstrMessageEvent, uid: str, context: Context) -> bool:
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
                "pending_sent": False,   # 本轮是否已经回过"稍等一下"，保证不重复刷屏
                "observe_round": 0, "next_ask_at": 0, "created_at": now,
            }
            _save_gate(gate)
            # 旧版本不等结果就把 notified_admin 置 True（而且这个字段从来没被读过）。
            # 现在按真实发送结果记录，方便在 WebUI 的门禁列表里看出"这条其实没通知到"。
            ok = await self._notify_admin(uid, name, gate[uid]["messages"], context)
            gate = _load_gate()
            if uid in gate:
                gate[uid]["notified_admin"] = bool(ok)
                _save_gate(gate)
            if not ok:
                logger.error(f"[Gatekeeper] 门禁：uid={uid} 的请示没能送达任何管理员，请检查管理员配置")
            return True

        info   = gate[uid]
        status = info.get("status", "pending")
        if status == "allowed": return False
        if status == "blocked": return True
        # "观察"= 暂时可以回复。旧版本这里没有 return，会一路落到函数末尾的 return True，
        # 导致观察状态和 pending 完全一样（全拦），"观察"这个选项等于不存在。
        # 到点后由 _observe_loop 负责再次向管理员请示 / 或达到轮次上限自动拉黑。
        if status == "observe": return False

        # 缓存消息
        msgs  = info.get("messages", [])
        limit = g.get("cache_limit", 20)
        if limit <= 0 or len(msgs) < limit:
            msgs.append(msg_text)
        info["messages"] = msgs
        info["name"]     = name

        # 超阈值 → 发一次请示等待消息
        threshold = g.get("trigger_threshold", 5)
        # 阈值不能大于缓存上限：msgs 长度被 cache_limit 卡住后永远涨不上去，
        # 阈值填得比上限大（比如上限20、阈值50）就会导致请示消息**永远不会发**。
        if limit > 0 and threshold > limit:
            logger.warning(
                f"[Gatekeeper] 门禁配置：触发阈值({threshold}) 大于消息缓存上限({limit})，"
                f"已按上限 {limit} 生效。建议把阈值调到上限以内。"
            )
            threshold = limit
        # !! 只发一次 !! 旧版本的条件是 `if len(msgs) >= threshold`，而 len(msgs) 一旦到达阈值
        # 就永远成立，于是从第 threshold 条消息开始，对方**每发一条就收到一条**
        # 「稍等一下，等熙熙同意哦～」——发 50 条就被刷 46 条重复回复。
        # 这里用 pending_sent 标记保证每一轮请示只回一次；进入新的观察轮次时会重置。
        if len(msgs) >= threshold and not info.get("pending_sent", False):
            pending_msg = g.get("pending_msg", "稍等一下，等熙熙同意哦～")
            await self._safe_send(
                event.send(MessageChain().message(pending_msg)),
                timeout=10, what="门禁请示等待消息"
            )
            info["pending_sent"] = True

        _save_gate(gate)
        return True

    async def _send_text_to_admins(self, text: str) -> bool:
        """直接走 aiocqhttp 协议端原生 API send_private_msg 把文本发给所有管理员。
        不依赖猜测 UMO 格式；每次调用都有超时保护，避免协议端无响应时卡住调用方。
        返回是否至少成功发给了一个管理员。"""
        admins = self._admins()
        if not admins:
            logger.warning("[Gatekeeper] 未配置管理员，无法发送通知")
            return False
        sent = False
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if platform:
                client = platform.get_client() if hasattr(platform, "get_client") else getattr(platform, "bot", None)
                if client:
                    for admin_id in admins:
                        ok = await self._safe_send(
                            client.api.call_action("send_private_msg", user_id=int(admin_id), message=text),
                            timeout=SEND_CALL_TIMEOUT, what=f"管理员通知 admin={admin_id}"
                        )
                        if ok is not None:
                            sent = True
        except Exception as e:
            logger.warning(f"[Gatekeeper] 获取 aiocqhttp 平台失败: {e}")

        if not sent:
            logger.error(f"[Gatekeeper] 管理员通知发送失败，未能联系任何管理员！admins={admins}")
        return sent

    async def _notify_admin(self, uid: str, name: str, messages: list, context: Context):
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
        return await self._send_text_to_admins(text)

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
            g        = self.config.get("gate", {})
            interval = g.get("observe_interval_hours", 2)
            # 允许对"还没有门禁记录"的 QQ 号直接设为观察（管理员手打号码、或先清过待处理记录），
            # 旧版本只在 uid 已存在时才建条目，但下面拼日志时又无条件访问 gate[uid]，直接 KeyError。
            if uid not in gate:
                gate[uid] = {
                    "status": "pending", "name": name, "messages": [],
                    "notified_admin": False, "pending_sent": False,
                    "observe_round": 0, "next_ask_at": 0, "created_at": time.time(),
                }
            gate[uid]["status"]        = "observe"
            gate[uid]["next_ask_at"]   = time.time() + interval * 3600
            gate[uid]["observe_round"] = gate[uid].get("observe_round", 0) + 1
            rnd = gate[uid]["observe_round"]
            _save_gate(gate)
            _append_gate_log(uid, name, "observe", f"第{rnd}轮观察")

        elif action == "block":
            bl = [str(x) for x in self.config.get("gate_blacklist", [])]
            if uid not in bl:
                bl.append(uid)
            self.config["gate_blacklist"] = bl
            self.config.save_config()
            if uid in gate: gate[uid]["status"] = "blocked"
            _save_gate(gate)

            # 你的需求里"拉黑 = 删除好友"，但删好友是**不可逆**的（对方要重新加），
            # 所以做成默认关闭的开关，确认想要这个行为再打开。
            detail = "加入黑名单"
            if self._cfg("gate", "block_delete_friend", default=False):
                deleted = await self._delete_friend(uid)
                detail += "，已删除好友" if deleted else "，删除好友失败（详见日志）"
            _append_gate_log(uid, name, "block", detail)

    async def _delete_friend(self, uid: str) -> bool:
        """调协议端删除好友。

        注意：删好友的动作名在各协议端**并不统一**（NapCat/Lagrange/go-cqhttp 都不太一样），
        所以这里按常见的几个依次尝试，成功一个就返回。全都失败时只记日志，
        黑名单本身已经生效了，删不掉好友不影响拦截效果。"""
        candidates = [
            ("delete_friend", {"user_id": int(uid)}),
            ("delete_friend", {"friend_id": int(uid)}),
            ("del_friend",    {"user_id": int(uid)}),
        ]
        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.warning("[Gatekeeper] 删除好友：未找到 aiocqhttp 平台")
                return False
            client = platform.get_client() if hasattr(platform, "get_client") else getattr(platform, "bot", None)
            if not client:
                logger.warning("[Gatekeeper] 删除好友：未获取到协议端 client")
                return False
            for action, kwargs in candidates:
                try:
                    await asyncio.wait_for(
                        client.api.call_action(action, **kwargs), timeout=SEND_CALL_TIMEOUT)
                    logger.info(f"[Gatekeeper] 已删除好友 uid={uid}（动作 {action}）")
                    return True
                except asyncio.TimeoutError:
                    logger.warning(f"[Gatekeeper] 删除好友超时（{action}）uid={uid}")
                except Exception as e:
                    logger.debug(f"[Gatekeeper] 删除好友：{action} 不被支持或失败（{e}），尝试下一种")
            logger.warning(
                f"[Gatekeeper] 删除好友失败 uid={uid}：所有已知动作名都不被当前协议端支持。"
                f"黑名单已生效，不影响拦截。"
            )
            return False
        except Exception as e:
            logger.warning(f"[Gatekeeper] 删除好友异常 uid={uid}: {e}")
            return False

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
                        self._spawn(
                            self._notify_admin(uid, info.get("name", uid),
                                               info.get("messages", []), self.context)
                        )
                        interval = self.config.get("gate", {}).get("observe_interval_hours", 2)
                        info["observe_round"] = rnd + 1
                        info["next_ask_at"]   = now + interval * 3600
                        info["pending_sent"]  = False   # 新一轮请示，允许再提醒对方一次
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

    def _list_provider_ids(self) -> list:
        """列出当前所有已配置的 provider id，仅用于报错提示，方便对照配置填对。"""
        try:
            ids = []
            for p in (self.context.get_all_providers() or []):
                try:
                    ids.append(p.meta().id)
                except Exception:
                    continue
            return ids
        except Exception:
            return []

    async def _text_chat_with_fallback(self, prompt: str, model: str = "", **extra_kwargs) -> str:
        """调用 provider.text_chat。

        !! 重要 !! 这里的 model 参数收到的是 **provider_id**（配置项用的是
        `_special: select_provider`，WebUI 存下来的是提供商的 id，比如 "openai_gemini"），
        **不是模型名**。所以必须先用 get_provider_by_id() 换成 provider 对象再调用；
        旧版本这里错误地把 provider_id 当成模型名传给了 `text_chat(model=...)`，
        上游 API 收到一个不存在的模型名后必然报错，然后被 except 吞掉静默降级主模型——
        表现就是"在界面上选了验证模型，但完全没有生效"。

        指定的 provider 不存在或调用失败时，自动降级为主模型重试一次。
        返回空字符串表示彻底失败。"""
        if model:
            provider = self.context.get_provider_by_id(model)
            if provider is None:
                logger.warning(
                    f"[Gatekeeper] 配置的 provider_id='{model}' 找不到对应的提供商，降级使用主模型。"
                    f"当前可用的 provider id：{self._list_provider_ids()}"
                )
            else:
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(prompt, **extra_kwargs), timeout=PROVIDER_CALL_TIMEOUT
                    )
                    return (resp.completion_text or "").strip()
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[Gatekeeper] 指定提供商 '{model}' 调用超时（>{PROVIDER_CALL_TIMEOUT}s），降级使用主模型重试。")
                except Exception as e:
                    logger.warning(
                        f"[Gatekeeper] 指定提供商 '{model}' 调用失败（{type(e).__name__}: {e}），"
                        f"降级使用主模型重试。请检查该提供商的配置/额度/权限是否正常。"
                    )

        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("[Gatekeeper] 没有可用的 provider")
            return ""

        try:
            resp = await asyncio.wait_for(provider.text_chat(prompt, **extra_kwargs), timeout=PROVIDER_CALL_TIMEOUT)
            return (resp.completion_text or "").strip()
        except asyncio.TimeoutError:
            logger.error(f"[Gatekeeper] 主模型调用超时（>{PROVIDER_CALL_TIMEOUT}s）")
            return ""
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

    # ── 错误静默模式 ──────────────────────────────────────────────────────────
    # 设计说明：
    # 1. AstrBot 核心在 LLM 调用失败时，会走正常的"结果装饰→发送"流程，把固定的报错文案
    #    （如"AstrBot 请求失败。错误类型: xxx 错误信息: xxx"）当作一条普通回复发出去。
    #    本插件在 on_decorating_result 钩子里识别这条报错文案（正则匹配，可在配置里自定义），
    #    "原样放行"这一次报错（不修改内容），然后把该会话标记为"静默中"。
    # 2. 静默期间，on_message 会拦截该会话的所有消息（缓存下来，不放行到后续任何逻辑/LLM），
    #    群聊@、私聊消息都一样处理；管理员、群聊/私聊各自有独立的豁免开关。
    # 3. 后台每 15 秒检查一次到期的静默会话，用缓存的最后一条消息去探测模型是否恢复：
    #    - 成功：直接把这次探测的回复当作正式回复发出去（引用/@最后一条消息），并提示
    #      错过了多少条消息；该会话解除静默。
    #    - 失败：重试计数 +1，未达上限则继续静默等待下一轮；达到上限则"放弃"（given_up），
    #      停止自动重试，直到管理员手动重置，或者检测到别的会话成功收到了正常回复
    #      （说明模型大概率已恢复），届时会自动重新激活一次重试机会。
    # 4. 注意：重试时直接调用 provider.text_chat()，不经过完整人格系统提示词/对话历史，
    #    是简化版兜底回复——目的是让对方第一时间知道"收到了，不会被一直晾着"，
    #    而不是完全还原刚才被打断那一刻的完整对话语境。
    # 5. 所有跨协程共享的状态读写都通过 per-session 的 asyncio.Lock 保护，且所有外部
    #    网络/模型调用都套了超时（PROVIDER_CALL_TIMEOUT / SEND_CALL_TIMEOUT），
    #    避免任何单次调用卡住导致锁长期不释放、进而让该会话彻底卡死。

    def _es_session_key(self, event: AstrMessageEvent, uid: str) -> str:
        return getattr(event, "unified_msg_origin", None) or f"uid_{uid}"

    @staticmethod
    def _extract_persona_prompt(personality) -> str:
        """不同 AstrBot 版本/v3格式里，人格的系统提示词字段名可能是 prompt 或 system_prompt
        （底层 SQLModel 的 Persona 表用的是 system_prompt 字段，但 v3 格式的 TypedDict
        命名不一定一致），两个都尝试，同时兼容返回的是 dict 还是带属性的对象，
        避免因为字段名猜错而导致"开了人格开关却静默不生效"的情况。"""
        if not personality:
            return ""
        if isinstance(personality, dict):
            return personality.get("prompt") or personality.get("system_prompt") or ""
        return getattr(personality, "prompt", "") or getattr(personality, "system_prompt", "") or ""

    @staticmethod
    async def _resolve_maybe_async(value):
        """AstrBot 各版本里人格相关接口有的是同步、有的是 async，这里统一处理：
        拿到的是 coroutine/future 就 await，否则原样返回。
        旧版本代码在同步函数里直接调用了可能是 async 的接口，拿到的是个 coroutine 对象，
        取属性当然取不到东西（还会留下 "coroutine was never awaited" 警告），
        结果就是"开了继承人格的开关，但人格根本没生效"。"""
        return await value if inspect.isawaitable(value) else value

    async def _get_persona_system_prompt(self, umo: Optional[str] = None) -> str:
        """获取应用于该会话的人格 system_prompt 文本。

        解析顺序（这才是 AstrBot 真正的人格生效逻辑）：
        1. 先查这个会话（umo）当前对话绑定的 persona_id —— 用户可能用 /persona 单独换过人格，
           只看"默认人格"是不对的，这正是旧版本"继承人格没对上"的根因；
        2. persona_id == "[%None]" 是 AstrBot 的哨兵值，表示用户**显式关闭**了人格，
           这种情况必须返回空串，绝不能擅自注入默认人格；
        3. persona_id 为空（None/""）表示"跟随默认人格"，才去取默认人格；
        4. 全部失败返回空字符串，调用方自行降级为不带人格调用。

        注意：旧版本还有一个"取人格列表第一个"的兜底，已经删掉——猜错人格比不带人格更糟，
        多人格场景下几乎必然张冠李戴。"""
        try:
            persona_id = None
            if umo:
                try:
                    cm = getattr(self.context, "conversation_manager", None)
                    if cm:
                        cid = await self._resolve_maybe_async(cm.get_curr_conversation_id(umo))
                        if cid:
                            conv = await self._resolve_maybe_async(cm.get_conversation(umo, cid))
                            if conv is not None:
                                persona_id = getattr(conv, "persona_id", None) or (
                                    conv.get("persona_id") if isinstance(conv, dict) else None)
                except Exception as e:
                    logger.debug(f"[Gatekeeper] 读取会话人格绑定失败（将回退到默认人格）: {e}")

            # 用户显式关闭了人格，必须尊重
            if persona_id == "[%None]":
                logger.debug(f"[Gatekeeper] 会话 {umo} 显式关闭了人格，不注入 system_prompt")
                return ""

            persona_mgr = getattr(self.context, "persona_manager", None)

            # 老版本 AstrBot：人格挂在 provider_manager 上
            if persona_mgr is None:
                pm = getattr(self.context, "provider_manager", None)
                if pm is None:
                    return ""
                if persona_id:
                    for p in (getattr(pm, "personas", None) or []):
                        pid = (p.get("name") if isinstance(p, dict) else getattr(p, "name", None))
                        if pid == persona_id:
                            return self._extract_persona_prompt(p)
                return self._extract_persona_prompt(getattr(pm, "selected_default_persona", None))

            # 新版本：先按 persona_id 精确取
            if persona_id and hasattr(persona_mgr, "get_persona"):
                try:
                    got = await self._resolve_maybe_async(persona_mgr.get_persona(persona_id))
                    text = self._extract_persona_prompt(got)
                    if text:
                        return text
                except Exception as e:
                    logger.debug(f"[Gatekeeper] 按 persona_id='{persona_id}' 取人格失败: {e}")

            # 没绑定特定人格 → 取该会话的默认人格
            if hasattr(persona_mgr, "get_default_persona_v3"):
                got = await self._resolve_maybe_async(persona_mgr.get_default_persona_v3(umo=umo))
                return self._extract_persona_prompt(got)
            return ""
        except Exception as e:
            logger.warning(f"[Gatekeeper] 获取人格 system_prompt 失败，将降级为不带人格调用: {e}")
            return ""

    async def _check_and_handle_llm_error(self, event: AstrMessageEvent, uid: str,
                                            is_admin: bool, is_group: bool, text: str) -> bool:
        """检测这次的回复是否是模型报错文案。命中则记录日志、按需进入静默/通知管理员，返回 True。
        未命中返回 False（调用方应继续走原有逻辑，比如双模型验证）。"""
        cfg = self.config.get("error_silence", {})
        if not cfg.get("enabled", True):
            return False

        pattern = cfg.get("error_pattern") or r"AstrBot\s*请求失败"
        try:
            hit = bool(re.search(pattern, text))
        except re.error as e:
            logger.warning(f"[Gatekeeper] 错误静默：自定义正则无效（{e}），回退使用默认匹配")
            hit = ("AstrBot" in text and "请求失败" in text)
        if not hit:
            return False

        session_key = self._es_session_key(event, uid)
        name         = getattr(event, "sender_name", uid) or uid
        group_id     = str(event.get_group_id()) if is_group else None

        err_type = ""
        m = re.search(r"错误类型[:：]\s*(\S+)", text)
        if m: err_type = m.group(1)
        err_msg = text
        m2 = re.search(r"错误信息[:：]\s*(.+)$", text, re.S)
        if m2: err_msg = m2.group(1).strip()

        signature = hashlib.md5(f"{err_type}|{err_msg[:80]}".encode()).hexdigest()[:12]
        _append_error_log(session_key, name, is_group, group_id, err_type, err_msg, signature)
        logger.warning(f"[Gatekeeper] 检测到模型报错 session={session_key} type={err_type} sig={signature}")

        # 豁免判断：豁免的会话只记录日志+按需通知管理员，不进入静默拦截
        exempt = (
            (is_admin and cfg.get("admin_exempt", True)) or
            (is_group and not cfg.get("block_group", True)) or
            (not is_group and not cfg.get("block_private", True))
        )
        if not exempt:
            async with self._get_es_lock(session_key):
                state = _load_error_silence()
                entry = state.get(session_key, {})
                entry.update({
                    "session_key": session_key, "is_group": is_group, "group_id": group_id,
                    "display_name": name, "status": "silenced",
                    "silenced_until": time.time() + cfg.get("cooldown_seconds", 120),
                    "retry_count": 0,
                    "private_uid": uid if not is_group else None,
                    "error_signature": signature,
                    "last_error_at": datetime.now().isoformat(),
                })
                entry.setdefault("first_error_at", datetime.now().isoformat())
                entry.setdefault("pending", [])
                entry.setdefault("pending_total_count", 0)
                state[session_key] = entry
                _save_error_silence(state)

        await self._maybe_notify_admin_error(cfg, session_key, name, is_group, group_id, err_type, err_msg, signature)
        return True

    async def _error_silence_intercept(self, event: AstrMessageEvent, uid: str,
                                         is_admin: bool, is_group: bool) -> bool:
        """若该会话当前正处于静默/已放弃状态，则缓存这条消息并返回 True（调用方应直接拦截，不再继续）。"""
        cfg = self.config.get("error_silence", {})
        if not cfg.get("enabled", True):
            return False
        if is_admin and cfg.get("admin_exempt", True):
            return False
        if is_group and not cfg.get("block_group", True):
            return False
        if not is_group and not cfg.get("block_private", True):
            return False

        session_key = self._es_session_key(event, uid)

        async with self._get_es_lock(session_key):
            state = _load_error_silence()
            entry = state.get(session_key)
            if not entry or entry.get("status") not in ("silenced", "given_up"):
                return False

            name = getattr(event, "sender_name", uid) or uid
            msg_text = event.message_str or ""
            msg_id = None
            try:
                msg_id = event.message_obj.message_id if event.message_obj else None
            except Exception:
                pass

            limit = cfg.get("max_cache_messages", 200)
            pending = entry.get("pending", [])
            pending.append({
                "uid": uid, "name": name, "text": msg_text, "message_id": msg_id,
                "time": datetime.now().isoformat(),
            })
            entry["pending_total_count"] = entry.get("pending_total_count", 0) + 1
            if len(pending) > limit:
                pending = pending[-limit:]
            entry["pending"] = pending
            if not is_group:
                entry["display_name"] = name
            state[session_key] = entry
            _save_error_silence(state)
            return True

    async def _maybe_notify_admin_error(self, cfg: dict, session_key: str, name: str,
                                          is_group: bool, group_id: str, err_type: str,
                                          err_msg: str, signature: str):
        """通知管理员有报错发生。规则：同一错误签名在去重窗口内不重复通知；
        另外有一个滑动时间窗口内的总条数限制，避免短时间内大量不同错误把管理员炸屏。"""
        if not cfg.get("notify_admin", True):
            return

        dedupe_min      = cfg.get("notify_dedupe_minutes", 30)
        rate_count      = cfg.get("notify_rate_limit_count", 5)
        rate_window_min = cfg.get("notify_rate_limit_minutes", 10)

        ns  = _load_notify_state()
        now = time.time()

        last_sent = ns.get("signatures", {}).get(signature, 0)
        if now - last_sent < dedupe_min * 60:
            logger.debug(f"[Gatekeeper] 错误通知去重：sig={signature} 在 {dedupe_min} 分钟内已通知过，跳过")
            return

        window_start = now - rate_window_min * 60
        ns["sent_log"] = [t for t in ns.get("sent_log", []) if t >= window_start]
        if len(ns["sent_log"]) >= rate_count:
            logger.warning(f"[Gatekeeper] 错误通知频率超限（{rate_window_min}分钟内已发{rate_count}次），本次跳过 sig={signature}")
            return

        chat_desc = f"群聊（群号 {group_id}）" if is_group else f"私聊 {name}（{session_key}）"
        text = (
            f"⚠️ 模型报错通知\n"
            f"会话：{chat_desc}\n"
            f"错误类型：{err_type or '未知'}\n"
            f"错误信息：{(err_msg or '')[:200]}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"已自动进入静默模式，期间消息会被缓存，模型恢复后会自动回复最后一条"
        )
        sent = await self._send_text_to_admins(text)
        if sent:
            ns.setdefault("signatures", {})[signature] = now
            ns["sent_log"].append(now)
            _save_notify_state(ns)
            _mark_error_log_notified(signature)

    async def _retry_loop(self):
        """每 15 秒检查一次到期的静默会话，逐个尝试重试。"""
        while True:
            try:
                await asyncio.sleep(15)
                state = _load_error_silence()
                now   = time.time()
                due_keys = [
                    k for k, v in state.items()
                    if v.get("status") == "silenced" and now >= v.get("silenced_until", 0)
                ]
                for key in due_keys:
                    try:
                        await self._attempt_retry_for_key(key)
                    except Exception as e:
                        # 单个会话的重试异常不应该影响同一轮里其它会话的重试
                        logger.error(f"[Gatekeeper] 会话 {key} 重试异常: {e}", exc_info=True)

                # 顺手回收不再需要的锁：_es_locks 以前只增不减，
                # bot 跑久了、聊过的会话一多就会白占内存。
                # 只回收"没有静默状态、且当前没被持有"的锁，正在用的绝不动。
                if len(self._es_locks) > 64:
                    for k in [k for k in self._es_locks
                              if k not in state and not self._es_locks[k].locked()]:
                        self._es_locks.pop(k, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Gatekeeper] retry_loop 异常: {e}", exc_info=True)

    async def _attempt_retry_for_key(self, key: str):
        async with self._get_es_lock(key):
            state = _load_error_silence()
            entry = state.get(key)
            if not entry or entry.get("status") != "silenced":
                return  # 状态已变化（比如被手动重置），跳过本次

            cfg         = self.config.get("error_silence", {})
            max_retries = cfg.get("max_retries", 3)
            cooldown    = cfg.get("cooldown_seconds", 120)
            last_msg    = entry["pending"][-1]["text"] if entry.get("pending") else "你好"

            chat_kwargs = {}
            if cfg.get("use_persona", True):
                persona_prompt = await self._get_persona_system_prompt(umo=key)
                if persona_prompt:
                    chat_kwargs["system_prompt"] = persona_prompt
                else:
                    logger.debug(f"[Gatekeeper] 错误静默：session={key} 未获取到人格 system_prompt，将不带人格重试")

            provider = self.context.get_using_provider(umo=key)
            success, reply_text = False, ""
            if provider:
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(last_msg, **chat_kwargs), timeout=PROVIDER_CALL_TIMEOUT
                    )
                    reply_text = (resp.completion_text or "").strip()
                    success = bool(reply_text)
                except asyncio.TimeoutError:
                    logger.warning(f"[Gatekeeper] 错误静默重试探测超时（>{PROVIDER_CALL_TIMEOUT}s）session={key}")
                except Exception as e:
                    logger.warning(f"[Gatekeeper] 错误静默重试失败 session={key}: {e}")
            else:
                logger.warning("[Gatekeeper] 错误静默重试：没有可用 provider")

            # 重新读取最新状态再写入（持锁期间该 key 不会被其它协程改写，这里是防御性写法，
            # 防止 entry 在重试调用期间被管理员通过 WUI 手动重置/删除）
            state = _load_error_silence()
            entry = state.get(key)
            if not entry:
                logger.info(f"[Gatekeeper] 错误静默：session={key} 在重试期间被重置，放弃本次结果")
                return

            entry["retry_count"] = entry.get("retry_count", 0) + 1
            name = entry.get("display_name", key)

            if success:
                _append_retry_log(key, name, entry["retry_count"], "success")
                await self._send_recovery_reply(entry, reply_text)
                state.pop(key, None)
                _save_error_silence(state)
                self._maybe_scan_given_up()
            else:
                if entry["retry_count"] >= max_retries:
                    entry["status"] = "given_up"
                    _append_retry_log(key, name, entry["retry_count"], "give_up")
                    logger.warning(f"[Gatekeeper] 错误静默：session={key} 已达最大重试次数({max_retries})，停止自动重试")
                else:
                    entry["silenced_until"] = time.time() + cooldown
                    _append_retry_log(key, name, entry["retry_count"], "fail")
                state[key] = entry
                _save_error_silence(state)

    async def _recover_given_up_sessions(self):
        """检测到任意一次正常成功的回复后调用：给所有"已放弃"的会话一次新的重试机会，
        因为这通常说明模型/网络已经恢复正常，不需要傻等下一次定时轮询。
        本方法总是以 asyncio.create_task 的"发后不理"方式被调用，所以内部务必兜底异常，
        否则异常只会被 asyncio 默默打印一条警告，不会进入插件自己的日志，不利于排查。"""
        try:
            state = _load_error_silence()
            given_up_keys = [k for k, v in state.items() if v.get("status") == "given_up"]
            if not given_up_keys:
                return
            revived = 0
            for key in given_up_keys:
                async with self._get_es_lock(key):
                    state2 = _load_error_silence()
                    entry = state2.get(key)
                    if not entry or entry.get("status") != "given_up":
                        continue
                    entry["status"]         = "silenced"
                    entry["silenced_until"] = time.time()  # 立即可重试
                    entry["retry_count"]    = 0
                    state2[key] = entry
                    _save_error_silence(state2)
                    revived += 1
            if revived:
                logger.info(f"[Gatekeeper] 检测到模型可能已恢复，重新激活 {revived} 个已放弃的静默会话以便重试")
        except Exception as e:
            logger.error(f"[Gatekeeper] _recover_given_up_sessions 异常: {e}", exc_info=True)

    async def _send_recovery_reply(self, entry: dict, reply_text: str):
        pending = entry.get("pending", [])
        total   = entry.get("pending_total_count", len(pending))
        last    = pending[-1] if pending else None

        prefix = f"（抱歉刚才卡了一下，错过了你的 {total} 条消息，回复最后一条哈～）\n" if total > 1 else ""
        final_text = prefix + reply_text

        ok = await self._send_chain_to_session(
            entry, final_text,
            quote_message_id=(last.get("message_id") if last else None),
            at_uid=(last.get("uid") if (entry.get("is_group") and last) else None),
        )
        if not ok:
            logger.error(f"[Gatekeeper] 错误静默：恢复回复发送失败 session={entry.get('session_key')}")

    async def _send_chain_to_session(self, entry: dict, text: str,
                                       quote_message_id=None, at_uid=None) -> bool:
        """把恢复回复发到对应的群/私聊会话，支持引用最后一条消息 + （群聊）@ 对应发送者。
        直接走 aiocqhttp 原生 API，不依赖猜测 UMO 格式；调用本身有超时保护。"""
        is_group   = entry.get("is_group")
        group_id   = entry.get("group_id")
        target_uid = entry.get("private_uid")

        segments = []
        if quote_message_id:
            segments.append({"type": "reply", "data": {"id": str(quote_message_id)}})
        if is_group and at_uid:
            segments.append({"type": "at", "data": {"qq": str(at_uid)}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})

        try:
            platform = self.context.get_platform(filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.warning("[Gatekeeper] 错误静默恢复：未找到 aiocqhttp 平台，无法发送恢复消息")
                return False
            client = platform.get_client() if hasattr(platform, "get_client") else getattr(platform, "bot", None)
            if not client:
                logger.warning("[Gatekeeper] 错误静默恢复：未获取到协议端 client")
                return False

            if is_group and group_id:
                ok = await self._safe_send(
                    client.api.call_action("send_group_msg", group_id=int(group_id), message=segments),
                    timeout=SEND_CALL_TIMEOUT, what=f"静默恢复回复(群{group_id})"
                )
            elif target_uid:
                ok = await self._safe_send(
                    client.api.call_action("send_private_msg", user_id=int(target_uid), message=segments),
                    timeout=SEND_CALL_TIMEOUT, what=f"静默恢复回复(私聊{target_uid})"
                )
            else:
                logger.warning(f"[Gatekeeper] 错误静默恢复：无法确定发送目标 session={entry.get('session_key')}")
                return False
            return ok is not None
        except Exception as e:
            logger.error(f"[Gatekeeper] 错误静默恢复消息发送失败: {e}")
            return False

    # ── 表情包缓存 ────────────────────────────────────────────────────────────

    async def _moji_preprocess(self, event: AstrMessageEvent, is_group: bool, is_admin: bool):
        """识别消息里的表情包，把描述缓存下来并注入到发给模型的文本里。

        并发要点：识图是个可能长达 60 秒的网络调用，**绝对不能**在持锁期间做，
        否则同一时刻多张图片会被串行化，整个 bot 卡住。所以流程拆成三段：
          1. 无锁读一份缓存快照，判断哪些图命中、哪些要新识别
          2. 无锁做识图（慢，可并发）
          3. 只在最后写回时持锁：重新读一次最新缓存再合并，避免"读-改-写"互相覆盖
        """
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

        img_comps = [c for c in chain if _is_image_comp(c)]
        if not img_comps:
            return  # 没有图片组件，静默跳过，不打日志（避免每条消息都刷屏）
        logger.debug(f"[Gatekeeper] moji: 检测到 {len(img_comps)} 个图片组件")

        limit      = min(int(m.get("cache_limit", 500) or 500), MOJI_CACHE_HARD_LIMIT)
        size_limit = m.get("sticker_max_size", 0)   # 0 = 不限制；按像素长边判断表情包 vs 照片

        snapshot   = _load_moji()      # 只读快照，用来判断命中
        desc_parts = []
        hit_hashes = []                # 命中的 hash，稍后统一 +1 使用次数
        new_entries = {}               # hash -> {desc, bytes}
        for comp in img_comps:
            img_bytes = await _fetch_image_bytes(comp)
            if not img_bytes:
                logger.warning("[Gatekeeper] moji: 图片下载失败，跳过")
                continue

            # 尺寸判断：超过阈值的视为照片，跳过缓存识别（省 token，避免把照片当表情包存）
            if size_limit > 0:
                w, h = _get_image_size(img_bytes)
                if w and h and max(w, h) > size_limit:
                    logger.debug(f"[Gatekeeper] moji: 图片 {w}x{h} 超过阈值 {size_limit}，当作照片跳过")
                    continue

            img_hash = hashlib.md5(img_bytes).hexdigest()

            cached = snapshot.get(img_hash)
            if isinstance(cached, dict) and cached.get("desc"):
                hit_hashes.append(img_hash)
                desc_parts.append(f"[表情包：{cached['desc']}]")
                logger.debug(f"[Gatekeeper] moji: 缓存命中 hash={img_hash[:8]} desc={cached['desc']}")
                continue

            desc = await self._recognize_image(img_bytes, m.get("vision_model", ""))
            if not desc:
                logger.warning(f"[Gatekeeper] moji: 识别返回空描述 hash={img_hash[:8]}")
                continue
            new_entries[img_hash] = {"desc": desc, "bytes": img_bytes}
            desc_parts.append(f"[表情包：{desc}]")
            logger.info(f"[Gatekeeper] moji: 新识别 hash={img_hash[:8]} desc={desc}")

        # ── 写回：持锁，且重新读一次最新缓存再合并 ──
        if hit_hashes or new_entries:
            async with self._moji_lock:
                cache = _load_moji()
                now = time.time()
                for h in hit_hashes:
                    e = cache.get(h)
                    if isinstance(e, dict):
                        e["last_used"] = now
                        e["use_count"] = int(e.get("use_count", 0) or 0) + 1
                for h, info in new_entries.items():
                    if h in cache:      # 期间被别的协程加进去了，只更新计数
                        e = cache[h]
                        e["last_used"] = now
                        e["use_count"] = int(e.get("use_count", 0) or 0) + 1
                        continue
                    cache[h] = {"desc": info["desc"], "last_used": now, "use_count": 1}
                cache = _moji_evict(cache, limit)
                _save_moji(cache)
                logger.debug(f"[Gatekeeper] moji: 缓存已保存，当前共 {len(cache)} 条")

        # ── 共享给 quote_tag（让 bot 也能把别人发来的表情包发出去）──
        # 放在缓存写回之后、且不阻塞主流程：失败只记日志，绝不影响这条消息的处理。
        if new_entries:
            self._spawn(self._share_new_stickers(new_entries))

        if desc_parts:
            desc_text = " ".join(desc_parts)
            # !! 关键 !! 旧版本只往 event.message_obj.message 里 append 了一个 Plain 组件，
            # 但 AstrBot 组装 LLM prompt 用的是 event.message_str，消息链里加的东西模型根本看不到——
            # 结果是识别表情包的 token 花掉了，省 token 的目的却完全没达到（模型压根没收到描述）。
            # 所以这里三件事一起做，保证不管走哪条路径描述都能到模型面前：
            #   1. 追加到消息链（保持原行为，某些插件/适配器会读消息链）
            #   2. 同步追加到 message_str（默认 LLM 流程真正读的就是这个）
            #   3. 暗存到 event 上，由 on_llm_request 钩子兜底注入 req.prompt
            try:
                chain.append(Comp.Plain("\n" + desc_text))
            except Exception:
                pass
            try:
                event.message_str = ((event.message_str or "") + "\n" + desc_text).strip()
            except Exception as e:
                logger.debug(f"[Gatekeeper] moji: 写入 message_str 失败（将依赖 on_llm_request 兜底）: {e}")
            try:
                setattr(event, "_gk_moji_desc", desc_text)
            except Exception:
                pass

    # ── 表情包共享（联动 quote_tag）────────────────────────────────────────────
    # 设计说明：
    # 1. 两个插件**不互相 import**，只约定一个磁盘目录，任一插件没装/被卸载都不会报错。
    # 2. 落盘位置就是 quote_tag 自己的表情包目录
    #    data/plugin_data/astrbot_plugin_quote_tag/stickers/
    #    它的 StickerStore.sync() 会把目录里新出现的图片按"文件名去扩展名"自动收录，
    #    所以文件名直接就是 LLM 要输出的那个名字：〔表情包:摸头〕。
    # 3. 我们只记录**自己写进去的**文件（shared_stickers.json），清理时绝不碰用户
    #    手动上传的表情包。
    # 4. 整个模块是"尽力而为"：任何失败都只记日志，绝不影响消息处理。

    def _quote_tag_sticker_dir(self) -> Optional[Path]:
        """定位 quote_tag 的表情包目录。优先按配置里的自定义路径，否则按约定推算。"""
        cfg = self.config.get("share", {})
        custom = (cfg.get("target_dir") or "").strip()
        if custom:
            return Path(custom)
        # DATA_DIR 形如 <...>/data/plugin_data/astrbot_plugin_gatekeeper
        # 兄弟目录就是 <...>/data/plugin_data/astrbot_plugin_quote_tag
        try:
            return DATA_DIR.parent / "astrbot_plugin_quote_tag" / "stickers"
        except Exception:
            return None

    async def _share_new_stickers(self, new_entries: dict):
        """把这一批新识别的表情包写进 quote_tag 的表情包库。new_entries: hash -> {desc, bytes}"""
        cfg = self.config.get("share", {})
        if not cfg.get("enabled", False):
            return
        target = self._quote_tag_sticker_dir()
        if target is None:
            return

        try:
            if not target.parent.exists():
                logger.info(
                    f"[Gatekeeper] 表情包共享：没找到 quote_tag 的数据目录（{target.parent}），"
                    f"可能是没装 quote_tag 或还没初始化过。本次跳过。"
                )
                return
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[Gatekeeper] 表情包共享：创建目录失败 {target}: {e}")
            return

        max_share  = int(cfg.get("max_shared", 200) or 200)
        size_cap   = int(cfg.get("max_file_kb", 2048) or 2048) * 1024

        async with self._share_lock:
            shared = _load_shared_stickers()
            changed = False

            for img_hash, info in new_entries.items():
                if img_hash in shared:
                    continue
                raw = info.get("bytes") or b""
                if not raw or len(raw) > size_cap:
                    logger.debug(f"[Gatekeeper] 表情包共享：{img_hash[:8]} 超过 {size_cap//1024}KB 上限，跳过")
                    continue

                base = _safe_sticker_name(info.get("desc", ""))
                ext  = _sticker_ext(raw)
                # 名字撞车就加后缀，避免覆盖已有表情包（包括用户手动上传的）
                fname, i = f"{base}{ext}", 2
                while (target / fname).exists():
                    fname = f"{base}{i}{ext}"
                    i += 1
                    if i > 50:
                        break
                try:
                    (target / fname).write_bytes(raw)
                except Exception as e:
                    logger.warning(f"[Gatekeeper] 表情包共享：写入 {fname} 失败: {e}")
                    continue

                shared[img_hash] = {
                    "file": fname, "desc": info.get("desc", ""),
                    "time": datetime.now().isoformat(),
                }
                changed = True
                logger.info(f"[Gatekeeper] 表情包共享：已写入 quote_tag 库 → {fname}")

            # 超过上限时，按写入时间从早到晚删掉自己写过的最老的那些。
            # 只删 shared 索引里记着的文件，用户手动上传的表情包永远不动。
            if len(shared) > max_share:
                ordered = sorted(shared.items(), key=lambda kv: kv[1].get("time", ""))
                for h, meta in ordered[:len(shared) - max_share]:
                    try:
                        f = target / meta.get("file", "")
                        if f.is_file():
                            f.unlink()
                    except Exception as e:
                        logger.debug(f"[Gatekeeper] 表情包共享：清理 {meta.get('file')} 失败: {e}")
                    shared.pop(h, None)
                    changed = True

            if changed:
                _save_shared_stickers(shared)

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
                resp = await asyncio.wait_for(
                    cap_provider.text_chat(prompt, **img_kwargs), timeout=PROVIDER_CALL_TIMEOUT
                )
                return (resp.completion_text or "").strip()
            except asyncio.TimeoutError:
                logger.warning(f"[Gatekeeper] moji: 调用 AstrBot 默认图片转述模型超时（>{PROVIDER_CALL_TIMEOUT}s），降级使用插件自配置的视觉模型。")
            except Exception as e:
                logger.warning(
                    f"[Gatekeeper] moji: 调用 AstrBot 默认图片转述模型失败（{type(e).__name__}: {e}），"
                    f"降级使用插件自配置的视觉模型。"
                )

        # 降级：走插件自己的 vision_model 配置（走统一降级方法，指定模型失败会再降级到主模型）
        return await self._text_chat_with_fallback(prompt, model, **img_kwargs)

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def _log_flush_loop(self):
        """每 20 秒把内存里缓冲的日志条目落盘一次，并按保留天数 + 最大条数做清理。
        把"写文件"这个慢操作从同步的 emit() 调用路径里完全剥离，确保日志系统本身永不被拖慢。"""
        while True:
            try:
                await asyncio.sleep(20)
                if not self._sys_log_buffer:
                    continue
                # 原子地取出当前缓冲区内容，留一个新的空列表继续接收
                batch, self._sys_log_buffer = self._sys_log_buffer, []

                cfg = self.config.get("log_monitor", {})
                logs = _load_sys_log()
                logs.extend(batch)
                retention_days = cfg.get("retention_days", 7)
                logs = _prune_days(logs, retention_days)
                max_entries = cfg.get("max_entries", 2000)
                if len(logs) > max_entries:
                    logs = logs[-max_entries:]
                _save_sys_log(logs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Gatekeeper] 日志监控刷盘异常: {e}", exc_info=True)

    async def terminate(self):
        """插件卸载/热重载时的收尾。

        旧版本只 cancel() 不 await，任务可能正停在写文件的中途就被丢下，
        热重载时新旧两个实例还会同时往同一批 json 里写。这里改成：
        先摘掉日志 handler（避免收尾过程自己产生的日志又被捕获），
        再把缓冲区刷盘，最后 await 所有任务真正结束。"""
        # 1. 先摘 handler，停止继续捕获日志
        for target, handler in self._log_handlers:
            try:
                target.removeHandler(handler)
            except Exception:
                pass
        self._log_handlers.clear()

        # 2. 取消后台任务并等它们真正退出
        tasks = [t for t in (self._observe_task, self._retry_task, self._log_flush_task) if t]
        for t in tasks:
            t.cancel()
        for t in list(self._bg_tasks):
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, *list(self._bg_tasks), return_exceptions=True),
                timeout=10,
            )
        except asyncio.TimeoutError:
            logger.warning("[Gatekeeper] 收尾：后台任务在 10 秒内没能全部结束，强制继续卸载")
        except Exception:
            pass
        self._bg_tasks.clear()

        # 3. 把还在内存缓冲区里的日志刷盘，避免卸载时丢掉最后一批
        if self._sys_log_buffer:
            try:
                batch, self._sys_log_buffer = self._sys_log_buffer, []
                logs = _load_sys_log()
                logs.extend(batch)
                cfg = self.config.get("log_monitor", {})
                logs = _prune_days(logs, cfg.get("retention_days", 7))
                max_entries = cfg.get("max_entries", 2000)
                if len(logs) > max_entries:
                    logs = logs[-max_entries:]
                _save_sys_log(logs)
            except Exception as e:
                logger.warning(f"[Gatekeeper] 收尾：日志刷盘失败: {e}")

        self._es_locks.clear()
        logger.info("[Gatekeeper] 已卸载，后台任务和日志监控均已清理")


# ── 工具函数 ──────────────────────────────────────────────────────────────────

# 明确的图片组件类名白名单。
# 旧版本的判断是 `type(comp).__name__ in (...) or (hasattr(comp,"url") and hasattr(comp,"file"))`，
# 后半句对 Record（语音）、Video（视频）、File（文件）这些组件**全部成立**，
# 于是语音和视频也会被当成表情包下载下来送进视觉模型——拿回一段乱描述，还照样扣钱。
# 所以这里只认类名，不再用 hasattr 猜。
_IMAGE_COMP_NAMES = frozenset({"Image", "ImageComponent"})
# 明确排除的类名，只用于在日志里解释"为什么跳过了这个组件"
_NON_IMAGE_COMP_NAMES = frozenset({"Record", "Voice", "Video", "File", "Node", "Forward"})

def _is_image_comp(comp) -> bool:
    return type(comp).__name__ in _IMAGE_COMP_NAMES

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
