"""今日老婆插件主体。"""
from __future__ import annotations

import os
import time
import random
from typing import Optional

from maibot_sdk import MaiBotPlugin, Command, CONFIG_RELOAD_SCOPE_SELF

from .config import PartnerGameConfig
from .utils import PersistStore, fetch_avatar_base64, today_str, format_user_display

class PartnerGamePlugin(MaiBotPlugin):
    config_model = PartnerGameConfig

    # 生命周期
    async def on_load(self) -> None:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)
        self._store = PersistStore(os.path.join(data_dir, "partner_game_seen.json"))
        self._last_action: dict = {}
        self.pending_proposals: dict = {}
        self._bot_qq_cache: Optional[str] = None
        self.ctx.logger.info("✅ partner_game 已加载")

    async def on_unload(self) -> None:
        store = getattr(self, "_store", None)
        if store is not None:
            await store.save()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info(f"partner_game 配置已更新: {version}")
            # 配置 bot_qq 改了，清缓存让下次重新解析
            self._bot_qq_cache = None

    # 上下文解析
    def _extract_ctx(self, kwargs: dict):
        """从命令回调 kwargs 中提取 group_id / sender_qq / sender_name。"""
        msg = kwargs.get("message", {})
        base_info = msg.get("message_base_info", {}) or kwargs.get("message_base_info", {}) or {}
        user_info = base_info.get("user_info", {}) or {}
        group_info = base_info.get("group_info", {}) or {}

        group_id = kwargs.get("group_id") or msg.get("group_id") or group_info.get("group_id")
        sender_qq = kwargs.get("user_id") or msg.get("user_id") or user_info.get("user_id")

        # raw_event 兜底
        raw_event = kwargs.get("raw_event") or msg.get("raw_event")
        if raw_event:
            if not group_id:
                if isinstance(raw_event, dict):
                    group_id = raw_event.get("group_id")
                else:
                    group_id = getattr(raw_event, "group_id", None)
            if not sender_qq:
                if isinstance(raw_event, dict):
                    sender_qq = raw_event.get("user_id")
                else:
                    sender_qq = getattr(raw_event, "user_id", None)

        sender_name = (
            user_info.get("user_cardname")
            or user_info.get("user_nickname")
            or ""
        )
        return (
            str(group_id) if group_id else None,
            str(sender_qq) if sender_qq else None,
            sender_name,
        )

    def _extract_at_qq(self, kwargs: dict) -> Optional[str]:
        # 1. Parse MaiBot's message['raw_message'] list
        msg_obj = kwargs.get("message")
        if isinstance(msg_obj, dict):
            raw_msg_list = msg_obj.get("raw_message")
            if isinstance(raw_msg_list, list):
                for seg in raw_msg_list:
                    if isinstance(seg, dict) and seg.get("type") == "at":
                        data = seg.get("data", {})
                        val = data.get("target_user_id") or data.get("qq") or data.get("target")
                        if val: return str(val)
        
        # 2. Try raw_event message chain (for other adapters)
        raw_event = kwargs.get("raw_event") or kwargs.get("message", {}).get("raw_event") or {}
        msg_chain = raw_event.get("message")
        if isinstance(msg_chain, list):
            for seg in msg_chain:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    data = seg.get("data", {})
                    val = data.get("target_user_id") or data.get("qq") or data.get("target")
                    if val: return str(val)
        
        # 3. Super fallback: Regex on the entire kwargs string!
        import re
        kw_str = str(kwargs)
        m = re.search(r"\[CQ:at,qq=(\d+)\]", kw_str)
        if m: return m.group(1)
        m2 = re.search(r"['\"](?:target_user_id|qq|target)['\"]:\s*['\"]?(\d+)['\"]?", kw_str)
        if m2: return m2.group(1)
        
        # 4. Fallback: direct QQ number after command in any text
        m4 = re.search(r"(?:/娶|娶|/强娶|强娶|/甩掉|甩掉)\s*@?\s*(\d+)", kw_str)
        if m4:
            return m4.group(1)
            
        return None

    def _is_explicit_command(self, kwargs: dict) -> bool:
        msg_text = ""
        msg_obj = kwargs.get("message")
        if isinstance(msg_obj, dict):
            msg_text = str(msg_obj.get("processed_plain_text") or "")
            if not msg_text:
                raw_list = msg_obj.get("raw_message")
                if isinstance(raw_list, list):
                    msg_text = "".join([str(seg.get("data", {}).get("text", "")) for seg in raw_list if isinstance(seg, dict) and seg.get("type") == "text"])
        if not msg_text:
            msg_text = str(kwargs.get("raw_message", ""))
        return msg_text.strip().startswith("/")

    def _has_quote(self, kwargs: dict) -> bool:
        """检查消息是否包含引用（回复），如果是则返回 True"""
        msg_obj = kwargs.get("message")
        if isinstance(msg_obj, dict):
            raw_msg_list = msg_obj.get("raw_message")
            if isinstance(raw_msg_list, list):
                for seg in raw_msg_list:
                    if isinstance(seg, dict) and seg.get("type") == "reply":
                        return True
                        
        raw_event = kwargs.get("raw_event") or kwargs.get("message", {}).get("raw_event") or {}
        msg_chain = raw_event.get("message")
        if isinstance(msg_chain, list):
            for seg in msg_chain:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    return True
                    
        return False

    def _is_forced_user(self, sender_qq: str) -> bool:
        cfg = self.config.partner_game
        if not cfg.forced_wives:
            return False
        for rule in cfg.forced_wives:
            parts = rule.replace("：", ":").replace("=", ":").split(":")
            if len(parts) == 2 and parts[0].strip() == str(sender_qq):
                return True
        return False

    async def _resolve_bot_qq(self) -> Optional[str]:
        if self._bot_qq_cache:
            return self._bot_qq_cache
        cfg_qq = (self.config.partner_game.bot_qq or "").strip()
        if cfg_qq:
            self._bot_qq_cache = cfg_qq
            return cfg_qq
        try:
            info = await self.ctx.api.call("adapter.napcat.system.get_login_info")
            if isinstance(info, dict):
                uid = info.get("user_id") or (info.get("data") or {}).get("user_id")
                if uid:
                    self._bot_qq_cache = str(uid)
                    return self._bot_qq_cache
        except Exception as e:
            self.ctx.logger.warning(f"无法自动获取机器人 QQ: {e}")
        return None

    # 通过适配器 API 直接发送，绕过 VLM 识图
    async def _send_napcat(self, group_id: int, message_chain: list) -> Optional[str]:
        try:
            res = await self.ctx.api.call(
                "adapter.napcat.message.send_msg",
                params={
                    "message_type": "group",
                    "group_id": int(group_id),
                    "message": message_chain,
                },
            )
            if isinstance(res, dict):
                data = res.get("data", res)
                msg_id = data.get("message_id")
                if msg_id:
                    return str(msg_id)
        except Exception as e:
            self.ctx.logger.error(f"napcat 发送失败: {e}")
        return None

    async def _send_text_at(self, group_id: str, sender_qq: str, text: str) -> Optional[str]:
        chain = []
        if sender_qq and sender_qq.isdigit():
            chain.append({"type": "at", "data": {"qq": str(sender_qq)}})
            chain.append({"type": "text", "data": {"text": " "}})
        chain.append({"type": "text", "data": {"text": text}})
        return await self._send_napcat(int(group_id), chain)

    async def _send_forward_msg(self, group_id: str, sender_qq: str, title: str, text: str) -> bool:
        """发送合并转发消息，减少刷屏"""
        bot_qq = await self._resolve_bot_qq() or "10000"
        messages = [{
            "type": "node",
            "data": {
                "name": title,
                "uin": str(bot_qq),
                "content": [{"type": "text", "data": {"text": text}}]
            }
        }]
        try:
            await self.ctx.api.call(
                "adapter.napcat.message.send_group_forward_msg",
                params={
                    "group_id": int(group_id),
                    "messages": messages
                }
            )
            return True
        except Exception as e:
            self.ctx.logger.warning(f"发送合并转发失败，降级为普通消息: {e}")
            await self._send_text_at(group_id, sender_qq, f"【{title}】\n{text}")
            return False

    async def _send_wife_message(
        self,
        group_id: str,
        sender_qq: str,
        wife_uid: str,
        wife_nick: str,
        wife_card: str,
        prefix: str,
        suffix: str = "",
    ) -> Optional[str]:
        display = format_user_display(wife_uid, wife_nick, wife_card)
        text = f"{prefix}【{display}】～❤"
        if suffix:
            text += f" {suffix}"

        chain: list = []
        if sender_qq and sender_qq.isdigit():
            chain.append({"type": "at", "data": {"qq": str(sender_qq)}})
            chain.append({"type": "text", "data": {"text": " "}})
        chain.append({"type": "text", "data": {"text": text}})

        if self.config.partner_game.send_avatar:
            size = int(self.config.partner_game.avatar_size or 640)
            b64 = await fetch_avatar_base64(wife_uid, size=size)
            if b64:
                chain.append({"type": "text", "data": {"text": "\n"}})
                chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})

        return await self._send_napcat(int(group_id), chain)


    async def _get_member_nick(self, group_id: str, qq: str) -> str:
        if not qq: return "未知"
        try:
            members = await self.ctx.api.call("adapter.napcat.group.get_group_member_list", group_id=int(group_id), no_cache=True)
            if isinstance(members, list):
                for mem in members:
                    if str(mem.get("user_id")) == str(qq):
                        return mem.get("card") or mem.get("nickname") or qq
        except Exception: pass
        return qq

    async def _send_avatar_msg(self, group_id: str, text: str, qq_to_at: str, qq_for_avatar: str) -> Optional[str]:
        chain = []
        if qq_to_at:
            chain.append({"type": "at", "data": {"qq": str(qq_to_at)}})
            chain.append({"type": "text", "data": {"text": " "}})
        chain.append({"type": "text", "data": {"text": text}})
        if self.config.partner_game.send_avatar and qq_for_avatar:
            size = int(self.config.partner_game.avatar_size or 640)
            from .utils import fetch_avatar_base64
            b64 = await fetch_avatar_base64(qq_for_avatar, size=size)
            if b64:
                chain.append({"type": "text", "data": {"text": "\n"}})
                chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})
        return await self._send_napcat(int(group_id), chain)

    async def _get_partner_nick(self, group_id: str, marriage: dict) -> str:
        """解析伴侣昵称，解决老数据或妻子视角的神秘发起人问题。"""
        nick = marriage.get("partner_nick", "神秘发起人")
        if nick == "神秘发起人":
            nick = await self._get_member_nick(group_id, marriage["partner_uid"])
        return nick

    def _get_user_marriage(self, group_id: str, qq: str) -> dict | None:
        marriages = self._store.data.get("marriages", {})
        grp = marriages.get(group_id, {})
        # check if husband
        if qq in grp:
            return {
                "role": "husband", 
                "partner_uid": grp[qq].get("wife_uid"), 
                "partner_nick": grp[qq].get("wife_nick"),
                "affection": grp[qq].get("affection", 0)
            }
        # check if wife
        for h_qq, w_info in grp.items():
            if str(w_info.get("wife_uid")) == qq:
                return {
                    "role": "wife", 
                    "partner_uid": h_qq, 
                    "partner_nick": "神秘发起人",
                    "affection": w_info.get("affection", 0)
                }
        return None

    def _get_user_data(self, group_id: str, qq: str) -> dict:
        users = self._store.data.setdefault("users", {})
        grp = users.setdefault(group_id, {})
        
        from .utils import today_str
        today = today_str(self.config.partner_game.tz_offset_hours)
        
        user = grp.setdefault(qq, {"money": 1000, "level": 0, "last_allowance": today})
        # 兼容补全
        if "money" not in user:
            user["money"] = 1000
        if "level" not in user:
            user["level"] = 0
            
        # 每日低保
        if user.get("last_allowance") != today:
            user["money"] += 200
            user["last_allowance"] = today
            
        return user

    def _save_user_data(self, group_id: str, qq: str, money: int = None, level: int = None):
        user = self._get_user_data(group_id, qq)
        if money is not None:
            user["money"] = money
        if level is not None:
            user["level"] = level
            
    def _check_daily_limit(self, group_id: str, qq: str, action: str, limit: int) -> bool:
        """检查今日是否超出限次，未超出则直接+1，并返回True。"""
        from .utils import today_str
        import asyncio
        today = today_str(self.config.partner_game.tz_offset_hours)
        if self._store.purge_old(today):
            # 跨日自动增加好感度
            marriages = self._store.data.setdefault("marriages", {})
            for g_id, g_m in marriages.items():
                for h_qq, m_info in g_m.items():
                    m_info["affection"] = m_info.get("affection", 0) + 5
            if self.config.partner_game.persist_enabled:
                asyncio.create_task(self._store.save())

        # 某些操作如果是白名单则豁免
        if action in ["partner_game", "divorce", "dump"]:
            cfg = self.config.partner_game
            no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
            if str(qq) in no_limit_users:
                return True
                
        usage = self._store.data.setdefault("daily_usage", {})
        group_usage = usage.setdefault(group_id, {})
        user_usage = group_usage.setdefault(qq, {})
        count = user_usage.get(action, 0)
        
        if count >= limit:
            return False
            
        user_usage[action] = count + 1
        return True

    # 冷却
    def _check_cooldown(self, group_id: str, sender_qq: str, command: str) -> tuple[bool, int]:
        """返回 (是否命中冷却, 剩余秒数)"""
        key = f"{group_id}_{sender_qq}_{command}"
        now = time.time()
        last_time = self._last_action.get(key, 0.0)
        cooldown = max(0, int(self.config.partner_game.cooldown_seconds or 60))
        remaining = int(cooldown - (now - last_time))
        if remaining > 0:
            return True, remaining
        self._last_action[key] = now
        return False, 0

    def _set_penalty(self, group_id: str, sender_qq: str, command: str, penalty_seconds: int):
        """设置额外惩罚时间（如抢老婆失败自闭 60 秒）"""
        key = f"{group_id}_{sender_qq}_{command}"
        cooldown = max(0, int(self.config.partner_game.cooldown_seconds or 60))
        # 强制封顶60秒自闭，无论传进来的 penalty_seconds 是多少
        penalty_seconds = min(penalty_seconds, 60)
        self._last_action[key] = time.time() + penalty_seconds - cooldown

    # /今日老婆
    @Command(
        "partner_game",
        description="随机抽取今日老婆",
        pattern=r"^\s*(?:/今日老婆|今日老婆|/抽老婆|抽老婆|/老婆|老婆)$"
    )
    async def handle_partner_game(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("好像是私聊呢，去群聊里找你的老婆吧。", stream_id)
            return True, "好像是私聊呢，去群聊里找你的老婆吧。", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "partner_game")
        if is_cd:
            await self._send_text_at(group_id, sender_qq, f"稍微休息一下再来吧~ (冷却中: {rem}秒)")
            return True, "cooldown", 2

        cfg = self.config.partner_game
        today = today_str(cfg.tz_offset_hours)

        # 首先不论是否白名单，强制校验不能开后宫
        marriage = self._get_user_marriage(group_id, sender_qq)
        if marriage:
            role = marriage["role"]
            partner_nick = await self._get_partner_nick(group_id, marriage)
            rel_str = "老婆" if role == "husband" else "老公"
            await self._send_text_at(group_id, sender_qq, f"你已经是【{partner_nick}】的伴侣啦，请好好陪陪你的{rel_str}吧！(可以使用 /约会 增加好感度)")
            return True, "already_wife", 2

        # 检查抽取限次
        if not self._check_daily_limit(group_id, sender_qq, "partner_game", 1):
            await self._send_text_at(group_id, sender_qq, "你今天已经抽取过伴侣了，请明天再来吧！")
            return True, "daily_limit", 2

        # 拉群成员
        try:
            members = await self.ctx.api.call(
                "adapter.napcat.group.get_group_member_list",
                group_id=int(group_id),
                no_cache=True,
            )
        except Exception as e:
            self.ctx.logger.error(f"获取群成员失败: {e}")
            await self._send_text_at(group_id, sender_qq, f"无法获取群成员列表：{e}")
            return True, "api_error", 2

        if not isinstance(members, list) or not members:
            await self._send_text_at(group_id, sender_qq, "群成员列表数据异常。")
            return True, "api_error", 2

        bot_qq = await self._resolve_bot_qq()
        exclude_bot = cfg.exclude_self

        # 筛选候选
        candidates = []
        for m in members:
            uid = str(m.get("user_id", ""))
            if not uid:
                continue
            if cfg.exclude_sender and uid == sender_qq:
                continue
            if exclude_bot and bot_qq and uid == str(bot_qq):
                continue
            if not self._get_user_marriage(group_id, uid):
                candidates.append(m)

        forced_wife_uid = None
        if cfg.forced_wives:
            for rule in cfg.forced_wives:
                # 兼容冒号或等号分割，如 "123456:876543"
                parts = rule.replace("：", ":").replace("=", ":").split(":")
                if len(parts) == 2 and parts[0].strip() == sender_qq:
                    forced_wife_uid = parts[1].strip()
                    break
        
        if forced_wife_uid:
            wife_uid = str(forced_wife_uid)
            wife_card = ""
            wife_nick = "未知昵称"
            found_in_group = False
            # 尝试在群成员中找名字
            for m in members:
                if str(m.get("user_id", "")) == wife_uid:
                    wife_card = str(m.get("card") or "")
                    wife_nick = str(m.get("nickname") or "")
                    found_in_group = True
                    break
            
            # 如果不在群里，尝试通过API获取其QQ昵称
            if not found_in_group:
                try:
                    for api_name in ["adapter.napcat.account.get_stranger_info", "adapter.napcat.get_stranger_info"]:
                        info = await self.ctx.api.call(api_name, user_id=int(wife_uid))
                        self.ctx.logger.info(f"DEBUG: api={api_name} info={info}")
                        if isinstance(info, dict):
                            # 有的返回结构包了一层 data，有的是直接铺平的
                            data = info.get("data") or info
                            nick = data.get("nickname")
                            if nick:
                                wife_nick = str(nick)
                                break
                except Exception as e:
                    self.ctx.logger.error(f"DEBUG API exception: {e}")
        else:
            if not candidates:
                await self._send_text_at(
                    group_id, sender_qq, "群里好像没人了（或者只有你和我啦）..."
                )
                return True, "no_candidates", 2
            chosen = random.choice(candidates)
            wife_uid = str(chosen.get("user_id"))
            wife_card = str(chosen.get("card") or "")
            wife_nick = str(chosen.get("nickname") or "")

        is_bot = (bot_qq and str(wife_uid) == str(bot_qq))
        if is_bot:
            prefix = "你今天的群老婆是我哦~"
        else:
            prefix = "今天你的老婆是"

        await self._send_wife_message(
            group_id, sender_qq, wife_uid, wife_nick, wife_card,
            prefix=prefix,
        )

        marriages = self._store.data.setdefault("marriages", {})
        marriages.setdefault(group_id, {})[sender_qq] = {
            "date": today,
            "wife_uid": wife_uid,
            "wife_nick": wife_nick,
            "wife_card": wife_card,
            "affection": 0
        }
        if cfg.persist_enabled:
            await self._store.save()

        return True, f"wife picked: {wife_card or wife_nick}", 2

    # /离婚
    @Command(
        "partner_game_divorce",
        description="清空今日老婆记录，可重新抽取（每日限一次）",
        pattern=r"^\s*(?:/离婚|离婚)(?:\s+|\[CQ:at|@|\d|$).*"
    )
    async def handle_wife_draw(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("只能在群里离婚喔～", stream_id)
            return True, "只能在群里离婚喔～", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "divorce")
        if is_cd:
            await self._send_text_at(group_id, sender_qq, f"技能冷却中，请等待 {rem} 秒后再试~")
            return True, "cooldown", 2

        cfg = self.config.partner_game
        today = today_str(cfg.tz_offset_hours)

        # 每日限一次
        if not self._check_daily_limit(group_id, sender_qq, "divorce", 1):
            await self._send_text_at(group_id, sender_qq, "每日仅限一次离婚哦，明天再来吧~")
            return True, "limit_reached", 2

        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_text_at(group_id, sender_qq, "你今天还没有老婆呢，不需要离婚～")
            return True, "no_record", 2
            
        if marriage["role"] == "wife":
            await self._send_text_at(group_id, sender_qq, "你是被绑定方，解除关系请使用【/甩掉@对方】命令。")
            return True, "wrong_role", 2
            
        target_qq = marriage["partner_uid"]
        marriages = self._store.data.setdefault("marriages", {})
        grp = marriages.get(group_id, {})
        if sender_qq in grp:
            grp.pop(sender_qq, None)
            if cfg.persist_enabled:
                await self._store.save()
            target_nick = await self._get_member_nick(group_id, target_qq)
            await self._send_avatar_msg(group_id, f"你已成功和【{target_nick}({target_qq})】离婚，现在可以重新抽你的新老婆啦～", sender_qq, target_qq)
            return True, "divorced", 2

    @Command("proposal_reply", pattern=r".*(?:我同意|我拒绝).*")
    async def handle_proposal_reply(self, stream_id: str = "", **kwargs):
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            return False, "Not in group", 0

        # Find matching proposal for this user in this group
        reply_id = None
        for pid, proposal in list(self.pending_proposals.items()):
            if str(proposal.get("group_id")) == str(group_id) and str(proposal.get("target_qq")) == str(sender_qq):
                reply_id = pid
                break
                
        if not reply_id:
            return False, "No valid pending proposal", 0
            
        proposal = self.pending_proposals[reply_id]
            
        # Check timeout
        import time
        timeout = int(self.config.partner_game.proposal_timeout_seconds or 60)
        if time.time() - proposal["timestamp"] > timeout:
            del self.pending_proposals[reply_id]
            await self._send_avatar_msg(group_id, "求婚已经超时失效啦~", sender_qq, "")
            return True, "Timeout", 2
            
        # Extract actual user text
        plain_text = ""
        msg_obj = kwargs.get("message")
        if isinstance(msg_obj, dict):
            plain_text = str(msg_obj.get("processed_plain_text") or "")
            if not plain_text:
                raw_list = msg_obj.get("raw_message")
                if isinstance(raw_list, list):
                    plain_text = "".join([str(seg.get("data", {}).get("text", "")) for seg in raw_list if isinstance(seg, dict) and seg.get("type") == "text"])
        if not plain_text:
            plain_text = str(kwargs.get("raw_message", ""))
            
        idx_agree = plain_text.rfind("我同意")
        idx_reject = plain_text.rfind("我拒绝")
        
        if idx_reject > idx_agree:
            del self.pending_proposals[reply_id]
            target_nick = await self._get_member_nick(group_id, sender_qq)
            await self._send_avatar_msg(group_id, f"很遗憾，对方【{target_nick}({sender_qq})】拒绝了你的求婚...", proposal["sender_qq"], sender_qq)
            return True, "Rejected", 2
            
        if idx_agree > idx_reject:
            del self.pending_proposals[reply_id]
                
            cfg = self.config.partner_game
            today = today_str(cfg.tz_offset_hours)
            
            if self._get_user_marriage(group_id, proposal["sender_qq"]) or self._get_user_marriage(group_id, sender_qq):
                await self._send_avatar_msg(group_id, "发生变故，你们中有人已经有伴侣了！", sender_qq, "")
                return True, "Already married", 2

            marriages = self._store.data.setdefault("marriages", {})
            marriages.setdefault(group_id, {})[proposal["sender_qq"]] = {
                "date": today,
                "wife_uid": sender_qq,
                "wife_nick": proposal["target_nick"],
                "wife_card": "",
                "affection": 0
            }
            if cfg.persist_enabled:
                await self._store.save()
                
            await self._send_avatar_msg(group_id, "恭喜！对方同意了你的求婚，你们现在是伴侣啦~❤", proposal["sender_qq"], sender_qq)
            return True, "Accepted", 2

        return False, "No match", 0

    @Command("marry", pattern=r"^\s*(?:/娶|娶)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_marry(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "marry")
        if is_cd:
            await self._send_avatar_msg(group_id, f"技能冷却中，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            self.ctx.logger.warning(f"marry command could not extract QQ. kwargs: {kwargs}")
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！请确保使用真实的 @ 功能，或者直接输入对方的 QQ 号。", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你不能娶自己哦~", sender_qq, "")
            return True, "Self marry", 2
            
        cfg = self.config.partner_game
        
        sender_marriage = self._get_user_marriage(group_id, sender_qq)
        if sender_marriage and str(sender_marriage.get("partner_uid")) == target_qq:
            await self._send_avatar_msg(group_id, "你们已经是伴侣了哦，不需要再重复绑定啦~", sender_qq, "")
            return True, "Already partners", 2
            
        if sender_marriage:
            await self._send_avatar_msg(group_id, "你今天已经有伴侣了，不能再娶别人啦！", sender_qq, "")
            return True, "Has wife", 2

        if self._get_user_marriage(group_id, target_qq):
            await self._send_avatar_msg(group_id, "对方今天已经名花有主啦！如果你非要硬来，可以尝试使用【/抢老婆 @对方】！", sender_qq, "")
            return True, "Target taken", 2
            
        # Get target info
        target_nick = await self._get_member_nick(group_id, target_qq)
        if not sender_nick:
            sender_nick = await self._get_member_nick(group_id, sender_qq)
            
        msg_text = f"【{sender_nick}({sender_qq})】向你发起了求婚~\n请回复本条消息并发送“我同意”或“我拒绝”。"
        
        # We manually build the chain to ensure it behaves exactly like a proposal
        chain = []
        chain.append({"type": "at", "data": {"qq": str(target_qq)}})
        chain.append({"type": "text", "data": {"text": "\n" + msg_text}})
        if self.config.partner_game.send_avatar:
            size = int(self.config.partner_game.avatar_size or 640)
            from .utils import fetch_avatar_base64
            b64 = await fetch_avatar_base64(sender_qq, size=size)
            if b64:
                chain.append({"type": "text", "data": {"text": "\n"}})
                chain.append({"type": "image", "data": {"file": f"base64://{b64}"}})

        msg_id = await self._send_napcat(int(group_id), chain)
        
        if msg_id:
            import time
            self.pending_proposals[msg_id] = {
                "group_id": group_id,
                "sender_qq": sender_qq,
                "target_qq": target_qq,
                "target_nick": target_nick,
                "timestamp": time.time()
            }
        
        return True, "Proposed", 2

    @Command("force_marry", pattern=r"^\s*(?:/强娶|强娶)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_force_marry(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "force_marry")
        if is_cd:
            cd_cfg = int(self.config.partner_game.cooldown_seconds or 60)
            msg = "刚刚遭遇了挫折，还在自闭受罚中" if rem > cd_cfg else "强娶技能冷却中"
            await self._send_avatar_msg(group_id, f"{msg}，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        import random, time
        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            self.ctx.logger.warning(f"force_marry command could not extract QQ. kwargs: {kwargs}")
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！请确保使用真实的 @ 功能，或者直接输入对方的 QQ 号。", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你不能强娶自己哦~", sender_qq, "")
            return True, "Self marry", 2
            
        cfg = self.config.partner_game
        
        sender_marriage = self._get_user_marriage(group_id, sender_qq)
        if sender_marriage and str(sender_marriage.get("partner_uid")) == target_qq:
            await self._send_avatar_msg(group_id, "你们已经是伴侣了哦，不需要再重复绑定啦~", sender_qq, "")
            return True, "Already partners", 2
            
        if sender_marriage:
            await self._send_avatar_msg(group_id, "你今天已经有伴侣了，休想开后宫！", sender_qq, "")
            return True, "Has wife", 2

        if self._get_user_marriage(group_id, target_qq):
            await self._send_avatar_msg(group_id, "对方今天已经名花有主啦，强娶失败！想硬来的话，可以尝试使用【/抢老婆 @对方】！", sender_qq, "")
            return True, "Target taken", 2
            
        # 确保有足够的罚款金币
        sender_data = self._get_user_data(group_id, sender_qq)
        if sender_data["money"] < 300:
            await self._send_avatar_msg(group_id, "你的存款不足 300 金币，连治安罚款都交不起，还是先去【/打工】吧！", sender_qq, "")
            return True, "No money", 2
            
        prob = cfg.force_marry_probability
        is_forced = self._is_forced_user(sender_qq)
        
        if is_forced:
            prob = 0.0
            
        prob = max(0.0, min(1.0, prob))
            
        if random.random() < prob:
            target_nick = await self._get_member_nick(group_id, target_qq)
            from .utils import today_str
            today = today_str(cfg.tz_offset_hours)
            marriages = self._store.data.setdefault("marriages", {})
            marriages.setdefault(group_id, {})[sender_qq] = {
                "date": today,
                "wife_uid": target_qq,
                "wife_nick": target_nick,
                "wife_card": "",
                "affection": 0
            }
            if cfg.persist_enabled:
                await self._store.save()
            
            if not sender_nick:
                sender_nick = await self._get_member_nick(group_id, sender_qq)
            await self._send_avatar_msg(group_id, f"霸道强娶成功！恭喜【{sender_nick}({sender_qq})】凭实力得到了【{target_nick}({target_qq})】！", sender_qq, target_qq)
        else:
            fine = random.randint(100, 300)
            comp = fine // 2
            
            target_data = self._get_user_data(group_id, target_qq)
            target_data["money"] += comp
            sender_data["money"] = max(0, sender_data["money"] - fine)
            
            if cfg.persist_enabled:
                await self._store.save()
                
            self._set_penalty(group_id, sender_qq, "force_marry", 60)
            await self._send_avatar_msg(group_id, f"强娶失败！你强扭瓜的行为有违风纪，被治安处当场制服并罚款 {fine} 金币，进入1分钟自闭期！\n（其中 {comp} 金币已作为精神损失费赔偿给对方）", sender_qq, target_qq)
            
        return True, "Force Marry Done", 2

    @Command("my_partner", pattern=r"^\s*(?:/我的伴侣|我的伴侣)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_my_partner(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "my_partner")
        if is_cd:
            await self._send_avatar_msg(group_id, f"技能冷却中，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        target_qq = self._extract_at_qq(kwargs)
        query_qq = target_qq if target_qq else sender_qq

        marriage = self._get_user_marriage(group_id, query_qq)
        
        if query_qq == sender_qq:
            if not marriage:
                await self._send_avatar_msg(group_id, "你目前还是单身贵族哦~", sender_qq, "")
                return True, "Single", 2
                
            partner_uid = marriage["partner_uid"]
            partner_nick = await self._get_member_nick(group_id, partner_uid)
                
            if marriage["role"] == "husband":
                text = f"你当前的老婆是：【{partner_nick}({partner_uid})】"
            else:
                text = f"你当前属于：【{partner_nick}({partner_uid})】"
        else:
            target_nick = await self._get_member_nick(group_id, query_qq)
            if not marriage:
                await self._send_avatar_msg(group_id, f"【{target_nick}】目前还是单身贵族哦~", sender_qq, "")
                return True, "Single", 2
                
            partner_uid = marriage["partner_uid"]
            partner_nick = await self._get_member_nick(group_id, partner_uid)
                
            if marriage["role"] == "husband":
                text = f"【{target_nick}】当前的老婆是：【{partner_nick}({partner_uid})】"
            else:
                text = f"【{target_nick}】当前的老公是：【{partner_nick}({partner_uid})】"
                
        await self._send_avatar_msg(group_id, text, sender_qq, partner_uid if marriage else "")
        return True, "Has partner", 2

    @Command("dump_partner", pattern=r"^\s*(?:/甩掉|甩掉)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_dump(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "dump_partner")
        if is_cd:
            await self._send_avatar_msg(group_id, f"技能冷却中，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            self.ctx.logger.warning(f"dump_partner command could not extract QQ. kwargs: {kwargs}")
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！请确保使用真实的 @ 功能，或者直接输入对方的 QQ 号。", sender_qq, "")
            return True, "No target error", 2
        
        cfg = self.config.partner_game
        from .utils import today_str
        today = today_str(cfg.tz_offset_hours)
        
        if not self._check_daily_limit(group_id, sender_qq, "dump", 1):
            await self._send_avatar_msg(group_id, "每日仅限一次甩掉操作哦~", sender_qq, "")
            return True, "limit_reached", 2
                
        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_avatar_msg(group_id, "你连伴侣都没有，甩掉空气吗？", sender_qq, "")
            return True, "Single", 2
            
        if marriage["role"] == "husband":
            await self._send_avatar_msg(group_id, "你是主动发起方，解除关系请使用【/离婚】命令。", sender_qq, "")
            return True, "Wrong role", 2
            
        if str(marriage["partner_uid"]) != str(target_qq):
            await self._send_avatar_msg(group_id, "TA不是你的伴侣哦！", sender_qq, "")
            return True, "Wrong target", 2
            
        # Execute dump
        marriages = self._store.data.get("marriages", {})
        if group_id in marriages and target_qq in marriages[group_id]:
            marriages[group_id].pop(target_qq, None)
            if cfg.persist_enabled:
                await self._store.save()
            target_nick = await self._get_member_nick(group_id, target_qq)
            await self._send_avatar_msg(group_id, f"你已成功甩掉了【{target_nick}({target_qq})】，现在恢复自由身了~", sender_qq, target_qq)
            
        return True, "Dumped", 2

    @Command("rob_wife", pattern=r"^\s*(?:/抢老婆|抢老婆)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_rob_wife(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "rob_wife")
        if is_cd:
            cd_cfg = int(self.config.partner_game.cooldown_seconds or 60)
            msg = "刚刚遭遇了挫折，还在自闭受罚中" if rem > cd_cfg else "抢夺技能冷却中"
            await self._send_avatar_msg(group_id, f"{msg}，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！请确保使用了真实的 @ 或输入了QQ号。", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你连自己都不放过吗？无法抢自己的老婆！", sender_qq, "")
            return True, "Self rob", 2
            
        cfg = self.config.partner_game
        
        sender_marriage = self._get_user_marriage(group_id, sender_qq)
        if sender_marriage and str(sender_marriage.get("partner_uid")) == target_qq:
            await self._send_avatar_msg(group_id, "连自己的伴侣都不放过吗？你们早就在一起啦！", sender_qq, "")
            return True, "Rob own partner", 2
            
        if sender_marriage:
            await self._send_avatar_msg(group_id, "你已经有伴侣了，休想当黄毛！", sender_qq, "")
            return True, "Has wife", 2

        target_marriage = self._get_user_marriage(group_id, target_qq)
        if not target_marriage:
            await self._send_avatar_msg(group_id, "对方还是个单身狗，你抢个寂寞啊！", sender_qq, "")
            return True, "Target single", 2
            
        if target_marriage["role"] == "husband":
            husband_qq = target_qq
            wife_qq = str(target_marriage["partner_uid"])
            wife_nick = str(target_marriage["partner_nick"])
        else:
            wife_qq = target_qq
            husband_qq = str(target_marriage["partner_uid"])
            wife_nick = await self._get_member_nick(group_id, wife_qq)
            
        husband_nick = await self._get_member_nick(group_id, husband_qq)
            
        # 确保有足够的罚款金币
        sender_data = self._get_user_data(group_id, sender_qq)
        if sender_data["money"] < 500:
            await self._send_avatar_msg(group_id, "你的存款不足 500 金币，连治安罚款都交不起，还是先去【/打工】吧！", sender_qq, "")
            return True, "No money", 2
            
        prob = cfg.rob_wife_probability
        is_forced = self._is_forced_user(sender_qq)
        husband_data = self._get_user_data(group_id, husband_qq)
        wife_data = self._get_user_data(group_id, wife_qq)
        affection = target_marriage.get("affection", 0)
        
        l1 = sender_data["level"]
        l2 = max(husband_data["level"], wife_data["level"])
        p1 = l1 * (l1 // 4 + 1)
        p2 = l2 * (l2 // 4 + 1)
        delta_p = p1 - p2
        
        if is_forced:
            prob = 0.0
        else:
            # 底蕴加成和护盾扣减 (新公式: 0.5% per power diff, -1% per 20 affection)
            prob = prob + (delta_p * 0.005) - (affection // 20 * 0.01)
            # 保底机制: 永远在 10% 到 90% 之间
            prob = max(0.10, min(0.90, prob))
        
        import random
        
        if random.random() < prob:
            from .utils import today_str
            today = today_str(cfg.tz_offset_hours)
            marriages = self._store.data.setdefault("marriages", {})
            
            # 删除原老公的老婆记录
            if husband_qq in marriages.get(group_id, {}):
                wife_card = marriages[group_id][husband_qq].get("wife_card", "")
                del marriages[group_id][husband_qq]
            else:
                wife_card = ""
                
            # 挂载到抢夺者头上
            marriages.setdefault(group_id, {})[sender_qq] = {
                "date": today,
                "wife_uid": wife_qq,
                "wife_nick": wife_nick,
                "wife_card": wife_card,
                "affection": 0
            }
            if cfg.persist_enabled:
                await self._store.save()
            
            if not sender_nick:
                sender_nick = await self._get_member_nick(group_id, sender_qq)
            await self._send_avatar_msg(group_id, f"黄毛出现！【{sender_nick}({sender_qq})】拔剑而出，成功从【{husband_nick}({husband_qq})】手中抢走了TA的伴侣【{wife_nick}({wife_qq})】！", sender_qq, wife_qq)
        else:
            fine = random.randint(200, 500)
            comp = fine // 2
            
            husband_data["money"] += comp
            sender_data["money"] = max(0, sender_data["money"] - fine)
            
            injury_msg = ""
            # Random chance to get injured if picking a fight and failing
            if not is_forced and random.random() < 0.2 and sender_data["level"] > 0:
                sender_data["level"] -= 1
                injury_msg = "\n并且你被反杀成重伤，境界跌落了1层！"
                
            if cfg.persist_enabled:
                await self._store.save()
                
            self._set_penalty(group_id, sender_qq, "rob_wife", 60)
            await self._send_avatar_msg(group_id, f"抢夺失败！你强扭瓜的行为有违风纪，被治安处当场制服并罚款 {fine} 金币，进入1分钟自闭期！{injury_msg}\n（其中 {comp} 金币已作为正当防卫奖励赔偿给原配老公）", sender_qq, husband_qq)
            
        return True, "Rob Wife Done", 2

    @Command("rob_chance", pattern=r"^\s*(?:/抢老婆胜率|/抢夺胜率|抢老婆胜率|抢夺胜率|/抢老婆概率|/抢夺概率|抢老婆概率|抢夺概率).*")
    async def handle_rob_chance(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！请确保护指明了目标QQ，例如【/抢老婆胜率 @张三】。", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你无法抢自己老婆，因此胜率为0%！", sender_qq, "")
            return True, "Self rob", 2
            
        sender_marriage = self._get_user_marriage(group_id, sender_qq)
        if sender_marriage and str(sender_marriage.get("partner_uid")) == target_qq:
            await self._send_avatar_msg(group_id, "你们已经是伴侣了！不需要抢！", sender_qq, "")
            return True, "Rob own partner", 2

        target_marriage = self._get_user_marriage(group_id, target_qq)
        if not target_marriage:
            await self._send_avatar_msg(group_id, "对方目前还是单身狗，无法使用【抢老婆】机制，胜率无从计算（你可以试试【/强娶】）。", sender_qq, "")
            return True, "Target single", 2
            
        if target_marriage["role"] == "husband":
            husband_qq = target_qq
            wife_qq = str(target_marriage["partner_uid"])
        else:
            wife_qq = target_qq
            husband_qq = str(target_marriage["partner_uid"])
            
        husband_data = self._get_user_data(group_id, husband_qq)
        wife_data = self._get_user_data(group_id, wife_qq)
        sender_data = self._get_user_data(group_id, sender_qq)
        affection = target_marriage.get("affection", 0)
        
        cfg = self.config.partner_game
        base_prob = cfg.rob_wife_probability
        is_forced = self._is_forced_user(sender_qq)
        
        l1 = sender_data["level"]
        l2 = max(husband_data["level"], wife_data["level"])
        p1 = l1 * (l1 // 4 + 1)
        p2 = l2 * (l2 // 4 + 1)
        delta_p = p1 - p2
        
        # Calculate modifiers
        delta_bonus = delta_p * 0.005
        affection_penalty = (affection // 20) * 0.01
        
        # Absolute numbers for display
        base_display = round(base_prob * 100, 2)
        delta_display = round(delta_bonus * 100, 2)
        aff_display = round(affection_penalty * 100, 2)
        
        if is_forced:
            final_prob = 0.0
            final_display = 0.0
        else:
            final_prob = base_prob + delta_bonus - affection_penalty
            final_prob = max(0.10, min(0.90, final_prob))
            final_display = round(final_prob * 100, 2)
            
        target_nick = await self._get_member_nick(group_id, target_qq)
        
        msg = f"📊 【抢夺胜率精算 - 目标：{target_nick}】 📊\n"
        msg += f"1. 基础胜率：{base_display}%\n"
        
        msg += f"2. 底蕴压制："
        if delta_p > 0:
            msg += f"+{delta_display}% (你战力 {p1} 高于 夫妻最高战力 {p2})\n"
        elif delta_p < 0:
            msg += f"{delta_display}% (你战力 {p1} 低于 夫妻最高战力 {p2})\n"
        else:
            msg += f"0% (双方战力持平 {p1})\n"
            
        msg += f"3. 恩爱护盾：-{aff_display}% (当前好感度 {affection} 点)\n"
        
        if is_forced:
            msg += f"4. 特殊机制：黑幕强制锁定！\n"
        else:
            msg += f"4. 综合结算保底：系统锁定胜率在 10% ~ 90% 之间\n"
            
        msg += f"---\n"
        msg += f"🔥 最终抢夺成功率：【{final_display}%】！"

        await self._send_avatar_msg(group_id, msg, sender_qq, target_qq)
        return True, "Rob Chance Done", 2

    @Command("rob_money", pattern=r"^\s*(?:/打劫|打劫)(?:\s+|\[CQ:at|@|\d|$).*")
    async def handle_rob_money(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            await self.ctx.send.text("请在群聊中使用该命令喔～", stream_id)
            return True, "Not in group", 2

        is_cd, rem = self._check_cooldown(group_id, sender_qq, "rob_money")
        if is_cd:
            await self._send_avatar_msg(group_id, f"技能冷却中，请等待 {rem} 秒后再试~", sender_qq, "")
            return True, "cooldown", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            if not self._is_explicit_command(kwargs):
                return False, "Not a command", 0
            await self._send_avatar_msg(group_id, "你想打劫谁？请 @ 目标！", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你不能打劫自己！", sender_qq, "")
            return True, "Self rob", 2
            
        sender_marriage = self._get_user_marriage(group_id, sender_qq)
        if sender_marriage and str(sender_marriage.get("partner_uid")) == target_qq:
            await self._send_avatar_msg(group_id, "连自己的伴侣都打劫，你还是人吗？", sender_qq, "")
            return True, "Rob own partner", 2
            
        sender_data = self._get_user_data(group_id, sender_qq)
        target_data = self._get_user_data(group_id, target_qq)
        
        # 保护低保户
        if target_data["money"] < 100:
            await self._send_avatar_msg(group_id, "对方穷得叮当响（资产低于100），你实在下不去手！", sender_qq, "")
            return True, "Target too poor", 2
            
        # 确保有足够的罚款金币
        if sender_data["money"] < 300:
            await self._send_avatar_msg(group_id, "你的存款不足 300 金币，连治安罚款都交不起，还是先去【/打工】吧！", sender_qq, "")
            return True, "No money", 2

        # 每日限制 3 次
        cfg = self.config.partner_game
        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        if sender_qq not in no_limit_users:
            if not self._check_daily_limit(group_id, sender_qq, "rob_money", 3):
                await self._send_avatar_msg(group_id, "你今天已经打劫太多次了，官府正在通缉你，明天再来吧！", sender_qq, "")
                return True, "Daily limit", 2
            
        prob = getattr(cfg, "rob_money_probability", 0.35)
        is_forced = self._is_forced_user(sender_qq)
        
        l1 = sender_data["level"]
        l2 = target_data["level"]
        p1 = l1 * (l1 // 4 + 1)
        p2 = l2 * (l2 // 4 + 1)
        delta_p = p1 - p2
        
        if is_forced:
            prob = 0.0
        else:
            # 底蕴加成 (新公式: 0.5% per power diff, no affection shield)
            prob = prob + (delta_p * 0.005)
            # 保底机制: 永远在 10% 到 90% 之间
            prob = max(0.10, min(0.90, prob))
        
        import random
        target_nick = await self._get_member_nick(group_id, target_qq)
        
        if random.random() < prob:
            # 抢走 10% ~ 25%
            ratio = random.uniform(0.10, 0.25)
            stolen = int(target_data["money"] * ratio)
            stolen = max(50, min(1500, stolen))
            # 不超过目标实际拥有
            stolen = min(target_data["money"], stolen)
            
            target_data["money"] -= stolen
            sender_data["money"] += stolen
            
            if cfg.persist_enabled:
                await self._store.save()
                
            await self._send_avatar_msg(group_id, f"打劫成功！【{sender_nick}({sender_qq})】凭借强大的战力威压，从【{target_nick}({target_qq})】身上强行搜刮了 {stolen} 金币！", sender_qq, target_qq)
        else:
            fine = random.randint(100, 300)
            comp = fine // 2
            
            target_data["money"] += comp
            sender_data["money"] = max(0, sender_data["money"] - fine)
            
            if cfg.persist_enabled:
                await self._store.save()
                
            self._set_penalty(group_id, sender_qq, "rob_money", 60)
            await self._send_avatar_msg(group_id, f"打劫失败！【{target_nick}({target_qq})】战力爆发奋起反抗，不仅将你击退，官府还对你罚款了 {fine} 金币，并关入1分钟自闭小黑屋！\n（其中 {comp} 金币已作为正当防卫精神损失费赔偿给对方）", sender_qq, target_qq)
            
        return True, "Rob Money Done", 2

    @Command("rob_money_chance", pattern=r"^\s*(?:/打劫胜率|/打劫概率|打劫胜率|打劫概率).*")
    async def handle_rob_money_chance(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs): return False, "Quote ignored", 0
        if not self.config.plugin.enabled: return False, "Plugin Disabled", 0

        group_id, sender_qq, sender_nick = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return True, "Not in group", 2

        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            await self._send_avatar_msg(group_id, "未识别到有效的 @ 目标！", sender_qq, "")
            return True, "No target error", 2
            
        if target_qq == sender_qq:
            await self._send_avatar_msg(group_id, "你无法打劫自己，因此胜率为0%！", sender_qq, "")
            return True, "Self rob", 2
            
        sender_data = self._get_user_data(group_id, sender_qq)
        target_data = self._get_user_data(group_id, target_qq)
        
        cfg = self.config.partner_game
        base_prob = getattr(cfg, "rob_money_probability", 0.35)
        is_forced = self._is_forced_user(sender_qq)
        
        l1 = sender_data["level"]
        l2 = target_data["level"]
        p1 = l1 * (l1 // 4 + 1)
        p2 = l2 * (l2 // 4 + 1)
        delta_p = p1 - p2
        
        delta_bonus = delta_p * 0.005
        base_display = round(base_prob * 100, 2)
        delta_display = round(delta_bonus * 100, 2)
        
        if is_forced:
            final_prob = 0.0
            final_display = 0.0
        else:
            final_prob = base_prob + delta_bonus
            final_prob = max(0.10, min(0.90, final_prob))
            final_display = round(final_prob * 100, 2)
            
        target_nick = await self._get_member_nick(group_id, target_qq)
        
        msg = f"📊 【打劫胜率精算 - 目标：{target_nick}】 📊\n"
        msg += f"1. 基础胜率：{base_display}%\n"
        
        msg += f"2. 底蕴压制："
        if delta_p > 0:
            msg += f"+{delta_display}% (你战力 {p1} 高于 对方战力 {p2})\n"
        elif delta_p < 0:
            msg += f"{delta_display}% (你战力 {p1} 低于 对方战力 {p2})\n"
        else:
            msg += f"0% (双方战力持平 {p1})\n"
            
        if is_forced:
            msg += f"3. 特殊机制：黑幕强制锁定！\n"
        else:
            msg += f"3. 综合结算保底：系统锁定胜率在 10% ~ 90% 之间\n"
            
        msg += f"---\n"
        msg += f"🔥 最终打劫成功率：【{final_display}%】！"

        await self._send_avatar_msg(group_id, msg, sender_qq, target_qq)
        return True, "Rob Money Chance Done", 2

    @Command("partner_game_help", pattern=r"^(?:/抽老婆帮助|/今日老婆帮助|/老婆帮助|抽老婆帮助)$", aliases=["/今日老婆帮助", "/抽老婆帮助"])
    async def handle_help(self, stream_id: str = "", **kwargs):
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
        if not self.config.plugin.enabled: return False, "Plugin Disabled", 0
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return False, "Not in group", 0

        prob_force = int(self.config.partner_game.force_marry_probability * 100)
        prob_rob = int(self.config.partner_game.rob_wife_probability * 100)
        help_text = (
            "💖 抽老婆插件系统指南 💖\n"
            "本系统包含【经济】、【修仙】与【好感度】三大模块。\n"
            "---\n"
            "【互动指令】\n"
            "1. /今日老婆：随机抽取今日缘分。\n"
            "2. /娶 @某人：向心仪的TA求婚。\n"
            "3. /强娶 @某人：霸道宣誓主权，有概率失败受罚（仅扣金币，不涉战力）。\n"
            "4. /抢老婆 @某人：化身黄毛，挑战夫妻共同防守战力！失败可能掉级！\n"
            "5. /打劫 @某人：直接抢钱（每日3次），纯看双方战力差，失败受罚！\n"
            "6. /我的伴侣：查看对象照片。\n"
            "7. /离婚：主动解除伴侣关系（仅限发起方老公使用）。\n"
            "8. /甩掉 @某人：主动把现任老公甩掉（仅限被绑定方老婆使用）。\n"
            "【养成指令】\n"
            "9. /打工：打工赚金币（每日3次）。\n"
            "10. /修炼：花金币突破境界（每日3次）。\n"
            "11. /约会：花金币陪伴侣，提升好感度（每日3次）。\n"
            "12. /上交存款 <金额>：将你的金币上交给伴侣。\n"
            "13. /我的状态：查看个人境界、金币与次数。\n"
            "【群排行榜】\n"
            "14. /战力榜：查看本群修仙大能。\n"
            "15. /财富榜：查看本群首富。\n"
            "16. /好感榜：查看本群模范夫妻。\n"
            "【进阶指令】\n"
            "17. /打劫胜率 @某人：精算打劫对方的战力差与成功率。\n"
            "18. /抢老婆胜率 @某人：智能测算你抢夺对方老婆的成功率及公式数据。\n"
            "19. /游戏机制：详细查看当前的各项几率、掉落、数值加成和经济系统。\n"
            "【管理员指令】\n"
            "20. /重置 @某人：强制重置其伴侣状态与抽取次数（限管理员）。\n"
            "21. /全服补偿 <金额>：为本群所有已知玩家发放金币补偿（限管理员）。\n"
            "22. /发钱 @某人 <金额>：为指定玩家增加或扣除金币（支持负数，限管理员）。\n"
            "---\n"
            f"🎯 当前基础强娶: {prob_force}% | 基础抢夺: {prob_rob}%\n"
            "💡 提示：抢老婆与打劫时计算【底蕴战力=等级×(境界数+1)】，战力差每1点提供0.5%胜率加成(双向保底10%)。无论打劫、强娶还是抢夺，失败罚款的一半将作为精神损失费直接赔偿给防守方！"
        )
        await self._send_forward_msg(group_id, sender_qq, "伴侣系统操作指南", help_text)
        return True, "help", 2

    # ---------------- RPG 指令 ---------------- #

    @Command("work", pattern=r"^\s*(?:/打工|打工)$")
    async def handle_work(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            return True, "Not in group", 2

        if not self._check_daily_limit(group_id, sender_qq, "work", 3):
            await self._send_avatar_msg(group_id, "你今天打工太累了，身体吃不消啦，明天再来吧！", sender_qq, "")
            return True, "limit", 2

        import random
        user_data = self._get_user_data(group_id, sender_qq)
        level = user_data.get("level", 0)
        base_earn = random.randint(50, 100)
        earn = base_earn + (level * 10)
        user_data["money"] += earn
        
        if self.config.partner_game.persist_enabled:
            await self._store.save()
            
        await self._send_avatar_msg(group_id, f"打工成功！凭借 Level {level} 的境界加成，你辛苦赚到了 {earn} 金币，当前余额：{user_data['money']} 金币。", sender_qq, "")
        return True, "Work done", 2

    @Command("date", pattern=r"^\s*(?:/约会|约会)$")
    async def handle_date(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            return True, "Not in group", 2

        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_avatar_msg(group_id, "你连伴侣都没有，一个人约会多可怜啊！", sender_qq, "")
            return True, "Single", 2

        user_data = self._get_user_data(group_id, sender_qq)
        if user_data["money"] < 50:
            await self._send_avatar_msg(group_id, "你的存款连 50 金币都没有，拿什么给伴侣买礼物？快去【/打工】吧！", sender_qq, "")
            return True, "No money", 2

        if not self._check_daily_limit(group_id, sender_qq, "date", 3):
            await self._send_avatar_msg(group_id, "伴侣今天约会累了，不想再出去了，明天再来吧！", sender_qq, "")
            return True, "limit", 2

        user_data["money"] -= 50
        import random
        add_aff = random.randint(10, 20)
        
        marriages = self._store.data.setdefault("marriages", {})
        grp_m = marriages.setdefault(group_id, {})
        # Find the actual record to update affection
        if marriage["role"] == "husband":
            rec = grp_m.get(sender_qq)
        else:
            rec = grp_m.get(marriage["partner_uid"])
            
        if rec:
            rec["affection"] = rec.get("affection", 0) + add_aff
            
        if self.config.partner_game.persist_enabled:
            await self._store.save()
            
        partner_nick = await self._get_partner_nick(group_id, marriage)
        total_aff = rec["affection"] if rec else add_aff
        await self._send_avatar_msg(group_id, f"你花费 50 金币与【{partner_nick}】去约会，感情升温了！\n好感度 +{add_aff}，当前总好感度：{total_aff}", sender_qq, "")
        return True, "Date done", 2

    @Command("cultivate", pattern=r"^\s*(?:/修炼|修炼)$")
    async def handle_cultivate(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            return True, "Not in group", 2
            
        user_data = self._get_user_data(group_id, sender_qq)
        level = user_data["level"]
        
        if level >= 35:
            await self._send_avatar_msg(group_id, "你已达渡劫圆满，此界再无你可修炼之物！", sender_qq, "")
            return True, "Max level", 2
            
        major = level // 4
        cost = (major + 1) * 50
        
        if user_data["money"] < cost:
            await self._send_avatar_msg(group_id, f"修炼需要购买丹药，当前境界单次修炼需消耗 {cost} 金币，你的存款不足！", sender_qq, "")
            return True, "No money", 2

        if not self._check_daily_limit(group_id, sender_qq, "cultivate", 3):
            await self._send_avatar_msg(group_id, "你今天修炼过度，经脉胀痛，请明日再来！", sender_qq, "")
            return True, "limit", 2

        user_data["money"] -= cost
        
        is_major_breakthrough = (level % 4 == 3)
        import random
        
        realms = ["炼气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫"]
        sub_realms = ["前期", "中期", "后期", "圆满"]
        
        def get_realm_name(l):
            if l >= 36: return "仙人"
            return f"{realms[l//4]}{sub_realms[l%4]}"
            
        if is_major_breakthrough:
            # 40% success
            if random.random() < 0.4:
                user_data["level"] += 1
                msg = f"突破成功！天降祥瑞，你成功迈入了【{get_realm_name(user_data['level'])}】！"
            else:
                # Failed, 30% chance drop
                if random.random() < 0.3 and user_data["level"] > 0:
                    user_data["level"] -= 1
                    msg = "突破失败！你急于求成导致走火入魔，根基受损，境界跌落了 1 层！"
                else:
                    msg = "突破失败！瓶颈太过坚固，你气血翻涌，未能突破。"
        else:
            # 80% success
            if random.random() < 0.8:
                user_data["level"] += 1
                msg = f"修炼顺利！你稳扎稳打，达到了【{get_realm_name(user_data['level'])}】。"
            else:
                msg = "修炼未果。你今日心神不宁，灵气流失，只白白消耗了丹药。"
                
        if self.config.partner_game.persist_enabled:
            await self._store.save()
            
        await self._send_avatar_msg(group_id, f"消耗 {cost} 金币修炼。\n{msg}", sender_qq, "")
        return True, "Cultivate done", 2

    @Command("status", pattern=r"^\s*(?:/我的状态|我的状态)$")
    async def handle_status(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Disabled", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id:
            return True, "Not in group", 2

        user_data = self._get_user_data(group_id, sender_qq)
        marriage = self._get_user_marriage(group_id, sender_qq)
        
        realms = ["炼气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫"]
        sub_realms = ["前期", "中期", "后期", "圆满"]
        level = user_data["level"]
        realm_name = f"{realms[level//4]}{sub_realms[level%4]}" if level < 36 else "仙人"
        
        msg = f"【个人状态板】\n境界：{realm_name}\n金币：{user_data['money']}\n"
        
        if marriage:
            rel = "老公" if marriage["role"] == "wife" else "老婆"
            aff = marriage.get("affection", 0)
            real_nick = await self._get_member_nick(group_id, marriage['partner_uid'])
            msg += f"伴侣：{real_nick} ({rel})\n"
            msg += f"好感：{aff} 点 (可抵消 {aff // 20}% 抢夺风险)\n"
        else:
            msg += "伴侣：单身贵族\n"
            
        usage = self._store.data.get("daily_usage", {}).get(group_id, {}).get(sender_qq, {})
        msg += f"\n今日已打工：{usage.get('work', 0)}/3\n"
        msg += f"今日已修炼：{usage.get('cultivate', 0)}/3\n"
        msg += f"今日已约会：{usage.get('date', 0)}/3\n"
        
        await self._send_avatar_msg(group_id, msg, sender_qq, sender_qq)
        return True, "Status done", 2

    @Command("transfer", pattern=r"^\s*(?:/上交存款|上交存款)\s+(\d+)$")
    async def handle_transfer(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Disabled", 0
            
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0

        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq:
            return True, "Not in group", 2

        import re
        msg_text = str(kwargs.get("message", {}).get("processed_plain_text", ""))
        if not msg_text:
            msg_text = str(kwargs.get("raw_message", ""))
        
        match = re.match(r"^\s*(?:/上交存款|上交存款)\s+(\d+)$", msg_text)
        if not match:
            return True, "Invalid amount format", 2
            
        amount = int(match.group(1))
        if amount <= 0:
            await self._send_avatar_msg(group_id, "上交金额必须大于 0！", sender_qq, "")
            return True, "Invalid amount", 2

        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_avatar_msg(group_id, "你都没有伴侣，想把钱上交给自己吗？", sender_qq, "")
            return True, "Single", 2

        user_data = self._get_user_data(group_id, sender_qq)
        if user_data["money"] < amount:
            await self._send_avatar_msg(group_id, f"你只有 {user_data['money']} 金币，不够上交 {amount} 金币哦！", sender_qq, "")
            return True, "No money", 2

        partner_uid = marriage["partner_uid"]
        partner_data = self._get_user_data(group_id, partner_uid)
        
        user_data["money"] -= amount
        partner_data["money"] += amount
        
        if self.config.partner_game.persist_enabled:
            await self._store.save()
            
        partner_nick = await self._get_partner_nick(group_id, marriage)
        await self._send_avatar_msg(group_id, f"转账成功！你向【{partner_nick}】上交了 {amount} 金币，对方一定感受到了你的深情厚谊~\n当前余额：{user_data['money']} 金币", sender_qq, partner_uid)
        return True, "Transfer done", 2

    @Command("rank_power", pattern=r"^\s*(?:/战力榜|战力榜)$")
    async def handle_rank_power(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id: return True, "Not in group", 2
        
        users = self._store.data.get("users", {}).get(group_id, {})
        if not users:
            await self._send_avatar_msg(group_id, "当前群聊暂无修仙者记录。", sender_qq, "")
            return True, "No data", 2
            
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("level", 0), reverse=True)[:10]
        realms = ["炼气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫"]
        sub_realms = ["前期", "中期", "后期", "圆满"]
        
        msg = "🏆 【修仙战力榜】 🏆\n"
        for i, (qq, data) in enumerate(sorted_users, 1):
            nick = await self._get_member_nick(group_id, qq)
            l = data.get("level", 0)
            r_name = f"{realms[l//4]}{sub_realms[l%4]}" if l < 36 else "仙人"
            msg += f"{i}. {nick} ({r_name})\n"
            
        await self._send_avatar_msg(group_id, msg.strip(), sender_qq, "")
        return True, "Rank Power", 2

    @Command("rank_money", pattern=r"^\s*(?:/财富榜|财富榜)$")
    async def handle_rank_money(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id: return True, "Not in group", 2
        
        users = self._store.data.get("users", {}).get(group_id, {})
        if not users:
            await self._send_avatar_msg(group_id, "当前群聊暂无财富记录。", sender_qq, "")
            return True, "No data", 2
            
        sorted_users = sorted(users.items(), key=lambda x: x[1].get("money", 0), reverse=True)[:10]
        msg = "💰 【群聊财富榜】 💰\n"
        for i, (qq, data) in enumerate(sorted_users, 1):
            nick = await self._get_member_nick(group_id, qq)
            msg += f"{i}. {nick} ({data.get('money', 0)} 金币)\n"
            
        await self._send_avatar_msg(group_id, msg.strip(), sender_qq, "")
        return True, "Rank Money", 2

    @Command("rank_affection", pattern=r"^\s*(?:/好感榜|好感榜)$")
    async def handle_rank_affection(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id: return True, "Not in group", 2
        
        marriages = self._store.data.get("marriages", {}).get(group_id, {})
        if not marriages:
            await self._send_avatar_msg(group_id, "当前群聊暂无伴侣记录。", sender_qq, "")
            return True, "No data", 2
            
        # Each marriage is stored under the husband's qq.
        # We want to display both names.
        sorted_m = sorted(marriages.items(), key=lambda x: x[1].get("affection", 0), reverse=True)[:10]
        msg = "💕 【模范夫妻好感榜】 💕\n"
        for i, (h_qq, data) in enumerate(sorted_m, 1):
            w_qq = data.get("wife_uid")
            if not w_qq: continue
            h_nick = await self._get_member_nick(group_id, h_qq)
            w_nick = await self._get_member_nick(group_id, w_qq)
            aff = data.get("affection", 0)
            msg += f"{i}. {h_nick} & {w_nick} ({aff} 点)\n"
            
        if msg == "💕 【模范夫妻好感榜】 💕\n":
            msg += "暂无有效数据。"
            
        await self._send_avatar_msg(group_id, msg.strip(), sender_qq, "")
        return True, "Rank Affection", 2

    @Command("admin_reset", pattern=r"^\s*(?:/重置|重置).*")
    async def handle_admin_reset(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return True, "Not in group", 2
        
        cfg = self.config.partner_game
        admin_users = [str(x) for x in (cfg.admin_users or [])]
        if str(sender_qq) not in admin_users:
            await self._send_avatar_msg(group_id, "你没有权限使用该指令！此操作仅限专属管理员使用。", sender_qq, "")
            return True, "No auth", 2
            
        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            await self._send_avatar_msg(group_id, "请通过 @ 提及你想重置的用户！", sender_qq, "")
            return True, "No target", 2
            
        marriages = self._store.data.setdefault("marriages", {})
        grp = marriages.get(group_id, {})
        
        # Check if target is husband
        if target_qq in grp:
            grp.pop(target_qq, None)
            
        # Check if target is wife
        for h_qq, w_info in list(grp.items()):
            if str(w_info.get("wife_uid")) == target_qq:
                grp.pop(h_qq, None)
                
        # Clear daily usage
        usage = self._store.data.get("daily_usage", {}).get(group_id, {}).get(target_qq, {})
        for key in ["partner_game", "divorce", "dump"]:
            usage.pop(key, None)
            
        if cfg.persist_enabled:
            await self._store.save()
            
        target_nick = await self._get_member_nick(group_id, target_qq)
        await self._send_avatar_msg(group_id, f"管理员操作成功！已重置【{target_nick}({target_qq})】的伴侣关系及抽取/离婚/甩掉次数！其战力与财富未受影响。", sender_qq, target_qq)
        return True, "Admin reset done", 2

    @Command("admin_compensate", pattern=r"^\s*(?:/全服补偿|全服补偿)\s+(\d+)$")
    async def handle_admin_compensate(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return True, "Not in group", 2
        
        cfg = self.config.partner_game
        admin_users = [str(x) for x in (cfg.admin_users or [])]
        if str(sender_qq) not in admin_users:
            await self._send_avatar_msg(group_id, "你没有权限使用该指令！此操作仅限专属管理员使用。", sender_qq, "")
            return True, "No auth", 2
            
        msg_text = str(kwargs.get("message", {}).get("processed_plain_text", kwargs.get("raw_message", "")))
        import re
        match = re.search(r"(?:/全服补偿|全服补偿)\s+(\d+)", msg_text)
        if not match:
            return True, "No amount", 2
            
        amount = int(match.group(1))
        
        users = self._store.data.get("users", {}).get(group_id, {})
        count = 0
        for uid, udata in users.items():
            udata["money"] = udata.get("money", 0) + amount
            count += 1
            
        if cfg.persist_enabled:
            await self._store.save()
            
        await self._send_avatar_msg(group_id, f"管理员操作成功！已为本群 {count} 位已知玩家发放补偿金 {amount} 金币！", sender_qq, "")
        return True, "Compensate done", 2

    @Command("admin_give_money", pattern=r"^\s*(?:/发钱|/扣钱|发钱|扣钱)\s+.*?(\-?\d+)$")
    async def handle_admin_give_money(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return True, "Not in group", 2
        
        cfg = self.config.partner_game
        admin_users = [str(x) for x in (cfg.admin_users or [])]
        if str(sender_qq) not in admin_users:
            await self._send_avatar_msg(group_id, "你没有权限使用该指令！此操作仅限专属管理员使用。", sender_qq, "")
            return True, "No auth", 2
            
        target_qq = self._extract_at_qq(kwargs)
        if not target_qq:
            await self._send_avatar_msg(group_id, "请通过 @ 提及你想修改资金的用户！", sender_qq, "")
            return True, "No target", 2
            
        msg_text = str(kwargs.get("message", {}).get("processed_plain_text", kwargs.get("raw_message", "")))
        import re
        match = re.search(r"(?:/发钱|/扣钱|发钱|扣钱)\s+.*?(\-?\d+)", msg_text)
        if not match:
            return True, "No amount", 2
            
        amount = int(match.group(1))
        
        target_data = self._get_user_data(group_id, target_qq)
        target_data["money"] = max(0, target_data["money"] + amount)
        
        if cfg.persist_enabled:
            await self._store.save()
            
        target_nick = await self._get_member_nick(group_id, target_qq)
        action = "增加" if amount >= 0 else "扣除"
        await self._send_avatar_msg(group_id, f"管理员操作成功！已为【{target_nick}({target_qq})】{action}了 {abs(amount)} 金币！其当前余额为 {target_data['money']} 金币。", sender_qq, target_qq)
        return True, "Give money done", 2

    @Command("game_info", pattern=r"^\s*(?:/游戏机制|游戏机制)$")
    async def handle_game_info(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Disabled", 0
        if self._has_quote(kwargs):
            return False, "Quote ignored", 0
            
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id: return True, "Not in group", 2
        
        prob_force = int(self.config.partner_game.force_marry_probability * 100)
        prob_rob = int(self.config.partner_game.rob_wife_probability * 100)
        
        info_text = (
            "📜 【伴侣大乱斗·核心机制说明】 📜\n"
            "---\n"
            "⚔️【修仙战力系统】\n"
            "• 等级上限：36级（炼气期至渡劫圆满，每4级为一个大境界）。\n"
            "• 修炼消耗：每日限3次，每次消耗 (当前大境界数 + 1) × 50 金币。\n"
            "• 修炼突破：小境界突破成功率 80%（失败无惩罚）；大境界突破成功率 40%，且失败时有 30% 概率走火入魔掉落 1 级。\n"
            "• 战斗掉级：进行【/抢老婆】失败时，若境界>0，有 20% 概率被反杀掉落 1 级。\n"
            "---\n"
            "💰【经济财富系统】\n"
            "• 初始资金：1000 金币。每日首次互动发放 200 金币低保。\n"
            "• 打工收入：基础 50~100 金币 + (当前等级 × 10) 金币加成。\n"
            "• 约会消耗：每次消耗 50 金币，增加 10~20 点好感度。\n"
            "---\n"
            "😈【互动犯罪机制】\n"
            f"• 强娶：基础概率 {prob_force}%。资金门槛 300，失败罚款 100~300 金币（50%赔给防守方）。\n"
            f"• 抢老婆：基础概率 {prob_rob}%。资金门槛 500，失败罚款 200~500 金币（50%赔给防守方原配），并有20%概率掉级。\n"
            f"• 打劫金币：基础概率 {getattr(self.config.partner_game, 'rob_money_probability', 0.35)*100}%。资金门槛 300，成功抢走目标 10%~25% 资产（最高1500）。失败罚款 100~300 金币（50%赔给防守方）。低保户（资产<100）不可被打劫。\n"
            "• 犯罪概率浮动公式：\n"
            "  1. 战力压制：采用【底蕴战力=等级×(大境界数+1)】，(进攻方战力 - 防守方最高战力) × 0.5%。\n"
            "  2. 好感抵消（仅限抢老婆）：夫妻好感度每 20 点，减免 1% 成功率。\n"
            "  3. 绝对保底：所有加成计算后，成功率强制锁定在 10% ~ 90% 之间。"
        )
        await self._send_forward_msg(group_id, sender_qq, "游戏机制详细说明", info_text)
        return True, "Game info", 2

def create_plugin():
    return PartnerGamePlugin()
