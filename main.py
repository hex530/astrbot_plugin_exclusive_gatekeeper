from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.all import *
import os, json, time

@register("exclusive_gatekeeper", "夕小柠 & 陆渊", "智能门禁系统：支持三档审批与 LLM 汇报。", "1.0.0")
class ExclusiveGatekeeper(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_path = os.path.join(os.path.dirname(__file__), "gatekeeper_data.json")
        self.data = self._load_data()
        self.last_ask_time = {}

    def _load_data(self):
        if os.path.exists(self.data_path):
            try:
                with open(self.data_path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    if "users" not in d: d["users"] = {}
                    if "cache" not in d: d["cache"] = {}
                    if "last_ask_id" not in d: d["last_ask_id"] = None
                    return d
            except: pass
        return {"users": {}, "cache": {}, "last_ask_id": None}

    def _save_data(self):
        with open(self.data_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_gatekeeper(self, event: AstrMessageEvent):
        if not event: return
        sender_id = event.get_sender_id()
        if not sender_id or sender_id == "None": return # 自动识别并过滤无效/空 QQ 号
        sender_id = str(sender_id)
        
        if sender_id == str(event.get_self_id()): return
        
        # 过滤空消息（如点赞等系统通知）
        message_str = event.get_message_str().strip()
        if not message_str: return 
        
        # 获取管理员列表
        admin_qqs_str = self.config.get("admin_qqs", "")
        admin_list = [x.strip() for x in admin_qqs_str.split(",") if x.strip()]
        
        # 兼容系统级管理员
        config_core = self.context.get_config()
        system_admins = [x.strip() for x in str(config_core.get("admin_qqs", "")).split(",") if x.strip()]
        admin_list.extend(system_admins)

        # 管理员审批逻辑
        if sender_id in admin_list:
            raw_msg = event.get_message_str().strip()
            cmd = None
            if raw_msg.startswith("准许"): cmd = "white"
            elif raw_msg.startswith("拒绝"): cmd = "black"
            elif raw_msg.startswith("观察"): cmd = "pending"
            
            if cmd:
                parts = raw_msg.split()
                target = parts[1] if len(parts) > 1 else self.data.get("last_ask_id")
                if not target:
                    await event.send(MessageChain([Plain("未找到待审批的目标。")]))
                    event.stop_event()
                    return
                self.data["users"][target] = {"status": cmd, "count": 0, "last_time": time.time()}
                self.data["cache"].pop(target, None)
                self._save_data()
                await event.send(MessageChain([Plain(f"审批成功：已将 {target} 设为【{cmd}】状态。")]))
                event.stop_event()
                return
            return

        # 访客逻辑
        user_info = self.data["users"].get(sender_id, {"status": "none"})
        if user_info["status"] == "black":
            event.stop_event()
            return
        if user_info["status"] == "white":
            return

        # 拦截并缓存
        event.stop_event()
        if sender_id not in self.data["cache"]: self.data["cache"][sender_id] = []
        self.data["cache"][sender_id].append(event.get_message_str())
        self.data["last_ask_id"] = sender_id
        self._save_data()

        # 达到阈值自动回复
        threshold = int(self.config.get("threshold", 3))
        if len(self.data["cache"][sender_id]) == threshold:
            msg = str(self.config.get("intercept_msg", "抱歉，主人现在不在，消息已记录。"))
            await event.send(MessageChain([Plain(msg)]))

        # 向管理员请示
        now = time.time()
        if sender_id not in self.last_ask_time or (now - self.last_ask_time[sender_id]) > 60:
            self.last_ask_time[sender_id] = now
            nickname = str(event.get_sender_name())
            ask_prompt = self.config.get("ask_prompt", "有个叫 {nickname}({sender_id}) 的人找你，他说：‘{message}’。请问是否理会？回复‘准许/拒绝/观察’即可。")
            
            final_ask = ask_prompt.format(nickname=nickname, sender_id=sender_id, message=event.get_message_str())
            
            if self.config.get("use_llm_ask", True):
                try:
                    llm_service = self.context.get_llm_service()
                    resp = await llm_service.request_llm(f"你是一个智能助手，请根据以下信息生成一段汇报：{final_ask}")
                    final_ask = resp.role_content
                except: pass

            for admin in admin_list:
                await event.bot.send_private_msg(user_id=int(admin), message=final_ask)
