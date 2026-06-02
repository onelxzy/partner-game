"""今日老婆插件工具函数与持久化。"""
from __future__ import annotations

import os
import json
import time
import base64
import asyncio
import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("partner_game")

async def fetch_avatar_base64(qq: str, size: int = 640, timeout_sec: int = 10) -> Optional[str]:
    """下载指定 QQ 的头像并返回 base64。失败返回 None。"""
    urls = [
        f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s={size}",
        f"https://q.qlogo.cn/g?b=qq&nk={qq}&s={size}",
    ]
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            return base64.b64encode(data).decode("utf-8")
                except Exception as e:
                    logger.debug(f"头像下载失败 {url}: {e}")
                    continue
    except Exception as e:
        logger.warning(f"头像下载会话错误: {e}")
    return None

def today_str(tz_offset_hours: int = 8) -> str:
    """根据时区偏移返回 YYYY-MM-DD。"""
    offset = max(-12, min(14, int(tz_offset_hours)))
    ts = time.time() + offset * 3600
    return time.strftime("%Y-%m-%d", time.gmtime(ts))

def format_user_display(uid: str, nick: str, card: str) -> str:
    nick = nick or "未知昵称"
    if card and card != nick:
        return f"{nick}{{{card}}}({uid})"
    return f"{nick}({uid})"

class PersistStore:
    """持久化今日抽取记录与离婚使用次数。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {"daily": {}, "divorce_usage": {}}
        self._load_sync()

    def _load_sync(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    self.data = {
                        "daily": d.get("daily", {}) or {},
                        "divorce_usage": d.get("divorce_usage", {}) or {},
                    }
        except Exception as e:
            logger.error(f"加载持久化失败: {e}")
            self.data = {"daily": {}, "divorce_usage": {}}

    async def save(self) -> None:
        async with self.lock:
            def _write():
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            try:
                await asyncio.to_thread(_write)
            except Exception as e:
                logger.error(f"保存持久化失败: {e}")

    def purge_old(self, today: str) -> bool:
        """清理非今日记录，返回是否有改动。"""
        changed = False
        daily = self.data.setdefault("daily", {})
        divorce = self.data.setdefault("divorce_usage", {})

        for gid, members in list(daily.items()):
            for uid in list(members.keys()):
                rec = members.get(uid) or {}
                if rec.get("date") != today:
                    members.pop(uid, None)
                    changed = True
            if not members:
                daily.pop(gid, None)
                changed = True

        for gid, usage in list(divorce.items()):
            for uid in list(usage.keys()):
                if usage.get(uid) != today:
                    usage.pop(uid, None)
                    changed = True
            if not usage:
                divorce.pop(gid, None)
                changed = True

        return changed
