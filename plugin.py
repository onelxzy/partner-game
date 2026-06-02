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

    def _get_user_marriage(self, group_id: str, qq: str) -> dict | None:
        daily = self._store.data.get("daily", {})
        grp = daily.get(group_id, {})
        # check if husband
        if qq in grp:
            return {"role": "husband", "partner_uid": grp[qq].get("wife_uid"), "partner_nick": grp[qq].get("wife_nick")}
        # check if wife
        for h_qq, w_info in grp.items():
            if str(w_info.get("wife_uid")) == qq:
                # 获取 husband nick
                return {"role": "wife", "partner_uid": h_qq, "partner_nick": "神秘发起人"} # 可以通过 API 查或者仅返回 uid
        return None

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
        """设置额外惩罚时间（如抢老婆失败自闭 5 分钟）"""
        key = f"{group_id}_{sender_qq}_{command}"
        cooldown = max(0, int(self.config.partner_game.cooldown_seconds or 60))
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

        if self._store.purge_old(today) and cfg.persist_enabled:
            await self._store.save()

        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        daily = self._store.data.setdefault("daily", {})

        # 首先不论是否白名单，强制校验不能开后宫
        marriage = self._get_user_marriage(group_id, sender_qq)
        if marriage:
            if marriage["role"] == "wife":
                partner_nick = marriage["partner_nick"]
                await self._send_text_at(group_id, sender_qq, f"你已经是【{partner_nick}】的伴侣啦，不能再开后宫了！(可以使用 /甩掉 解除关系)")
                return True, "already_wife", 2

        # 每日已抽：复读，或是已经被抽（白名单豁免）
        if sender_qq not in no_limit_users:
            rec = daily.get(group_id, {}).get(sender_qq)
            if isinstance(rec, dict) and rec.get("date") == today:
                await self._send_wife_message(
                    group_id, sender_qq,
                    str(rec.get("wife_uid", "未知")),
                    str(rec.get("wife_nick", "")),
                    str(rec.get("wife_card", "")),
                    prefix="你今天已经有老婆了哦，她是",
                    suffix="要好好对待她哦~",
                )
                return True, "already_picked", 2

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

        daily.setdefault(group_id, {})[sender_qq] = {
            "date": today,
            "wife_uid": wife_uid,
            "wife_nick": wife_nick,
            "wife_card": wife_card,
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
    async def handle_divorce(self, stream_id: str = "", **kwargs):
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

        if self._store.purge_old(today) and cfg.persist_enabled:
            await self._store.save()

        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        daily = self._store.data.setdefault("daily", {})
        divorce_usage = self._store.data.setdefault("divorce_usage", {})

        # 每日限一次
        if sender_qq not in no_limit_users:
            if divorce_usage.get(group_id, {}).get(sender_qq) == today:
                await self._send_text_at(
                    group_id, sender_qq, "每日仅限一次离婚哦，明天再来吧~"
                )
                return True, "limit_reached", 2

        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_text_at(group_id, sender_qq, "你今天还没有老婆呢，不需要离婚～")
            return True, "no_record", 2
            
        if marriage["role"] == "wife":
            await self._send_text_at(group_id, sender_qq, "你是被绑定方，解除关系请使用【/甩掉@对方】命令。")
            return True, "wrong_role", 2
            
        target_qq = marriage["partner_uid"]
        grp = daily.get(group_id, {})
        if sender_qq in grp:
            grp.pop(sender_qq, None)
            divorce_usage.setdefault(group_id, {})[sender_qq] = today
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

            daily = self._store.data.setdefault("daily", {})
            daily.setdefault(group_id, {})[proposal["sender_qq"]] = {
                "date": today,
                "wife_uid": sender_qq,
                "wife_nick": proposal["target_nick"],
                "wife_card": "",
            }
            if cfg.persist_enabled:
                await self._store.save()
                
            await self._send_avatar_msg(group_id, "恭喜！对方同意了你的求婚，你们现在是伴侣啦~❤", proposal["sender_qq"], sender_qq)
            return True, "Accepted", 2

        return False, "No match", 0

    @Command("marry", pattern=r"^\s*(?:/娶|娶).*")
    async def handle_marry(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

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
        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        
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

    @Command("force_marry", pattern=r"^\s*(?:/强娶|强娶).*")
    async def handle_force_marry(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

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
        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        
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
            
        prob = cfg.force_marry_probability
        if random.random() < prob:
            target_nick = await self._get_member_nick(group_id, target_qq)
            from .utils import today_str
            today = today_str(cfg.tz_offset_hours)
            daily = self._store.data.setdefault("daily", {})
            daily.setdefault(group_id, {})[sender_qq] = {
                "date": today,
                "wife_uid": target_qq,
                "wife_nick": target_nick,
                "wife_card": "",
            }
            if cfg.persist_enabled:
                await self._store.save()
            await self._send_avatar_msg(group_id, f"霸道总裁附体！你成功强娶了【{target_nick}({target_qq})】！", sender_qq, target_qq)
        else:
            self._set_penalty(group_id, sender_qq, "force_marry", 300)
            await self._send_avatar_msg(group_id, "强娶失败！对方给了你一巴掌，你进入了5分钟的自闭冷却期。", sender_qq, target_qq)
            
        return True, "Force Marry Done", 2

    @Command("my_partner", pattern=r"^(?:/我的伴侣|我的伴侣)$")
    async def handle_my_partner(self, stream_id: str = "", **kwargs):
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

        marriage = self._get_user_marriage(group_id, sender_qq)
        if not marriage:
            await self._send_avatar_msg(group_id, "你目前还是单身贵族哦~", sender_qq, "")
            return True, "Single", 2
            
        partner_uid = marriage["partner_uid"]
        partner_nick = await self._get_member_nick(group_id, partner_uid)
            
        if marriage["role"] == "husband":
            text = f"你当前的老婆是：【{partner_nick}({partner_uid})】"
        else:
            text = f"你当前属于：【{partner_nick}({partner_uid})】"
            
        await self._send_avatar_msg(group_id, text, sender_qq, partner_uid)
        return True, "Has partner", 2

    @Command("dump_partner", pattern=r"^\s*(?:/甩掉|甩掉).*")
    async def handle_dump(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

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
        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        dump_usage = self._store.data.setdefault("dump_usage", {})
        
        if sender_qq not in no_limit_users:
            if dump_usage.get(group_id, {}).get(sender_qq) == today:
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
        daily = self._store.data.get("daily", {})
        if group_id in daily and target_qq in daily[group_id]:
            daily[group_id].pop(target_qq, None)
            dump_usage.setdefault(group_id, {})[sender_qq] = today
            if cfg.persist_enabled:
                await self._store.save()
            target_nick = await self._get_member_nick(group_id, target_qq)
            await self._send_avatar_msg(group_id, f"你已成功甩掉了【{target_nick}({target_qq})】，现在恢复自由身了~", sender_qq, target_qq)
            
        return True, "Dumped", 2

    @Command("rob_wife", pattern=r"^\s*(?:/抢老婆|抢老婆).*")
    async def handle_rob_wife(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled:
            return False, "Plugin Disabled", 0

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
        no_limit_users = [str(x) for x in (cfg.no_limit_users or [])]
        
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
            
        prob = cfg.rob_wife_probability
        import random
        
        if random.random() < prob:
            from .utils import today_str
            today = today_str(cfg.tz_offset_hours)
            daily = self._store.data.setdefault("daily", {})
            
            # 删除原老公的老婆记录
            if husband_qq in daily.get(group_id, {}):
                wife_card = daily[group_id][husband_qq].get("wife_card", "")
                del daily[group_id][husband_qq]
            else:
                wife_card = ""
                
            # 挂载到抢夺者头上
            daily.setdefault(group_id, {})[sender_qq] = {
                "date": today,
                "wife_uid": wife_qq,
                "wife_nick": wife_nick,
                "wife_card": wife_card,
            }
            if cfg.persist_enabled:
                await self._store.save()
            
            if not sender_nick:
                sender_nick = await self._get_member_nick(group_id, sender_qq)
            await self._send_avatar_msg(group_id, f"黄毛出现！【{sender_nick}({sender_qq})】拔剑而出，成功从【{husband_nick}({husband_qq})】手中抢走了TA的伴侣【{wife_nick}({wife_qq})】！", sender_qq, wife_qq)
        else:
            self._set_penalty(group_id, sender_qq, "rob_wife", 300)
            await self._send_avatar_msg(group_id, f"抢夺失败！【{husband_nick}({husband_qq})】誓死守卫了爱情，将你一脚踢飞，你进入了5分钟的自闭冷却期。", sender_qq, husband_qq)
            
        return True, "Rob Wife Done", 2

    @Command("partner_game_help", pattern=r"^(?:/抽老婆帮助|/今日老婆帮助|/老婆帮助|抽老婆帮助)$", aliases=["/今日老婆帮助", "/抽老婆帮助"])
    async def handle_help(self, stream_id: str = "", **kwargs):
        if not self.config.plugin.enabled: return False, "Plugin Disabled", 0
        group_id, sender_qq, _ = self._extract_ctx(kwargs)
        if not group_id or not sender_qq: return False, "Not in group", 0

        prob_force = int(self.config.partner_game.force_marry_probability * 100)
        prob_rob = int(self.config.partner_game.rob_wife_probability * 100)
        help_text = (
            "💖 抽老婆插件功能指南 💖\n"
            "1. 【/今日老婆】或【抽老婆】：随机抽取今日老婆。\n"
            "2. 【/娶 @某人】：向某人求婚，需对方回复“我同意”。\n"
            "3. 【/强娶 @某人】：霸道强娶某人，有概率失败自闭。\n"
            "4. 【/抢老婆 @某人】：当一回黄毛，概率抢走别人的老婆！\n"
            "5. 【/我的伴侣】：查看自己当前的伴侣是谁。\n"
            "6. 【/离婚】：老公主动解除关系。\n"
            "7. 【/甩掉 @某人】：老婆主动甩掉现任老公。\n"
            "---\n"
            f"🎯 当前强娶成功率: {prob_force}% | 抢老婆成功率: {prob_rob}%"
        )
        await self._send_text_at(group_id, sender_qq, help_text)
        return True, "help", 2

def create_plugin():
    return PartnerGamePlugin()
