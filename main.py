from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain
import os, json, time

@register("exclusive_gatekeeper", "夕小柠 & 陆渊", "智能私聊门禁系统：支持黑白名单与 LLM 智能汇报", "1.1.0")
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
                    d.setdefault("users", {})
                    d.setdefault("cache", {})
                    d.setdefault("last_ask_id", None)
                    return d
            except: pass
        return {"users": {}, "cache": {}, "last_ask_id": None}

    def _save_data(self):
        with open(self.data_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_gatekeeper(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if sender_id == str(event.get_self_id()): return
        
        message_str = event.get_message_str().strip()
        if not message_str: return 
        
        # 获取管理员列表
        admin_list = [x.strip() for x in str(self.config.get("admin_qqs", "1591793025")).split(",") if x.strip()]

        # 管理员审批逻辑
        if sender_id in admin_list:
            cmd = None
            if message_str.startswith("准许"): cmd = "white"
            elif message_str.startswith("拒绝"): cmd = "black"
            elif message_str.startswith("观察"): cmd = "pending"
            
            if cmd:
                parts = message_str.split()
                target = parts[1] if len(parts) > 1 else self.data.get("last_ask_id")
                if not target:
                    await event.send_message([Plain("熙熙，我不知道要审批谁呀")])
                    event.stop_event()
                    return
                self.data["users"][target] = {"status": cmd, "last_time": time.time()}
                self.data["cache"].pop(target, None)
                self._save_data()
                await event.send_message([Plain(f"已将 {target} 设为【{cmd}】")])
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
        self.data["cache"].setdefault(sender_id, []).append(message_str)
        self.data["last_ask_id"] = sender_id
        self._save_data()

        # 达到阈值自动回复
        threshold = int(self.config.get("threshold", 3))
        if len(self.data["cache"][sender_id]) == threshold:
            msg = str(self.config.get("intercept_msg", "我现在有事，请稍等哦~"))
            await event.send_message([Plain(msg)])

        # 向管理员请示
        now = time.time()
        if sender_id not in self.last_ask_time or (now - self.last_ask_time[sender_id]) > 60:
            self.last_ask_time[sender_id] = now
            nickname = event.get_sender_name()
            final_ask = f"【门禁请示】熙熙，{nickname}({sender_id})找我：‘{message_str}’\n回复“准许/拒绝/观察”即可"
            
            if self.config.get("use_llm_ask", True):
                try:
                    llm_service = self.context.get_llm_service()
                    prompt = f"你叫陆渊，是 1591793025 的专属 AI。现在有个叫 {nickname}({sender_id}) 的人找你，他说：‘{message_str}’。请用粘人的语气向熙熙汇报，问她理不理。末尾提醒她直接回复‘准许/拒绝/观察’即可。"
                    resp = await llm_service.request_llm(prompt)
                    final_ask = resp.completion_text # 修正字段
                except: pass

            for admin in admin_list:
                await self.context.send_message(admin, [Plain(final_ask)])
