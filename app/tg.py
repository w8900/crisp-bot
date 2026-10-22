"""Telegram Bot API:话题管理、消息推送、长轮询。"""
import asyncio
import logging

import httpx

from . import config

log = logging.getLogger("crisp-bot.tg")

# 长轮询 50s,客户端超时要比它长
_client = httpx.AsyncClient(
    base_url=f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}",
    timeout=65,
)


class TopicGone(RuntimeError):
    """目标话题已被删除或不可用(人工在群里删掉了话题)。"""


# 话题被删除/关闭时 Telegram 的报错措辞
_TOPIC_GONE = ("message thread not found", "topic_deleted", "topic deleted", "topic_closed")


async def api(method: str, **params):
    r = await _client.post(f"/{method}", json=params)
    data = r.json()
    if not data.get("ok"):
        desc = str(data.get("description") or "")
        if params.get("message_thread_id") and any(s in desc.lower() for s in _TOPIC_GONE):
            raise TopicGone(desc)
        raise RuntimeError(f"Telegram {method} 失败: {desc}")
    return data["result"]


async def create_topic(name: str) -> int:
    topic_name = f"[{config.BOT_NAME}] {name}" if config.BOT_NAME else name
    res = await api("createForumTopic", chat_id=config.TG_GROUP_ID, name=topic_name[:96])
    return res["message_thread_id"]


async def send(thread_id: int | None, text: str, parse_mode: str | None = None) -> list[int]:
    """发消息到指定话题(thread_id 为 None 时发到 General),返回消息 id 列表。"""
    text = text or "(空)"
    if config.BOT_NAME:
        text = f"【{config.BOT_NAME}】\n{text}"
    ids = []
    for i in range(0, len(text), 4000):
        params = {"chat_id": config.TG_GROUP_ID, "text": text[i : i + 4000]}
        if thread_id:
            params["message_thread_id"] = thread_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        res = await api("sendMessage", **params)
        ids.append(res["message_id"])
    return ids


async def file_url(file_id: str) -> str | None:
    """把 TG 文件 id 换成可下载的直链(仅内部使用,URL 含 bot token)。"""
    try:
        res = await api("getFile", file_id=file_id)
        return f"https://api.telegram.org/file/bot{config.TG_BOT_TOKEN}/{res['file_path']}"
    except Exception:
        log.exception("getFile 失败: %s", file_id)
        return None


async def get_admins() -> list[dict]:
    """群管理员的 user 对象列表,附带 status(creator/administrator)。"""
    try:
        res = await api("getChatAdministrators", chat_id=config.TG_GROUP_ID)
        return [
            {**m["user"], "status": m.get("status", "")}
            for m in res
            if m.get("user")
        ]
    except Exception:
        log.exception("getChatAdministrators 失败")
        return []


async def send_photo(thread_id: int | None, url: str, caption: str = ""):
    """把图片(URL)发到话题;失败降级为文字链接。"""
    if config.BOT_NAME:
        caption = f"【{config.BOT_NAME}】\n{caption}".strip()
    params = {"chat_id": config.TG_GROUP_ID, "photo": url, "caption": caption[:1000]}
    if thread_id:
        params["message_thread_id"] = thread_id
    try:
        await api("sendPhoto", **params)
    except TopicGone:
        raise  # 话题没了,交给上层重建后重发
    except Exception:
        log.warning("sendPhoto 失败,降级为链接: %s", url)
        await send(thread_id, f"{caption}\n[图片] {url}".strip())


BOT_COMMANDS = [
    ("test", "测试全部模型接口连通性和额度"),
    ("ai", "本会话 AI 开关:/ai on 或 /ai off"),
    ("close", "结案本会话并恢复 AI"),
    ("info", "查看本会话访客资料"),
    ("learn", "立即从人工回复中学习"),
    ("kbtopic", "打开知识库投喂话题"),
]


async def register_commands():
    """把命令注册到群聊的「/」菜单,方便随时点选。"""
    try:
        await api(
            "setMyCommands",
            commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS],
            scope={"type": "chat", "chat_id": config.TG_GROUP_ID},
        )
    except Exception:
        log.exception("注册命令菜单失败")


async def poll(handler):
    """长轮询群消息,逐条交给 handler(msg) 处理。"""
    offset = 0
    while True:
        try:
            updates = await api(
                "getUpdates", offset=offset, timeout=50, allowed_updates=["message"]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("getUpdates 失败,3 秒后重试")
            await asyncio.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg:
                continue
            try:
                await handler(msg)
            except Exception:
                log.exception("处理 TG 消息失败: %s", msg.get("text"))
