"""核心编排:Crisp 事件 ⇄ TG 话题群 的双向桥接 + AI 自动回复。"""
import asyncio
import html as html_lib
import logging
import random
import re
import time
from collections import deque
from datetime import datetime

from . import config, crisp, db, kb, learn, llm, tg

log = logging.getLogger("crisp-bot")

# 我们主动发到 Crisp 的消息(AI 回复 / TG 人工回复)也会触发 message:received
# webhook,用最近发送记录去重,避免自己镜像自己
_recent_sent: dict[str, deque] = {}
_create_lock = asyncio.Lock()


_seen_fingerprints: dict = {}


def _dup_event(data: dict) -> bool:
    """按 Crisp 消息指纹去重(生产/开发 webhook 并存时同一事件会投递两次)。"""
    fp = data.get("fingerprint")
    if not fp:
        return False
    now = time.time()
    for k, t in list(_seen_fingerprints.items()):
        if now - t > 300:
            _seen_fingerprints.pop(k, None)
    if fp in _seen_fingerprints:
        return True
    _seen_fingerprints[fp] = now
    return False


def _note_sent(session_id: str, content: str):
    q = _recent_sent.setdefault(session_id, deque(maxlen=20))
    q.append((content, time.time()))


def _was_sent_by_us(session_id: str, content: str) -> bool:
    q = _recent_sent.get(session_id)
    if not q:
        return False
    now = time.time()
    return any(c == content and now - t < 600 for c, t in q)


# ---------- Crisp → TG ----------

async def handle_crisp_event(payload: dict):
    try:
        if config.CRISP_WEBSITE_ID and payload.get("website_id") != config.CRISP_WEBSITE_ID:
            return
        event = payload.get("event")
        data = payload.get("data") or {}
        if event == "message:send":
            await _on_user_message(data)
        elif event == "message:received":
            await _on_operator_message(data)
    except Exception:
        log.exception("处理 Crisp 事件失败: %s", payload.get("event"))


_COUNTRIES = {
    "CN": "中国", "HK": "香港", "TW": "台湾", "MO": "澳门", "US": "美国", "JP": "日本",
    "KR": "韩国", "SG": "新加坡", "MY": "马来西亚", "TH": "泰国", "VN": "越南", "PH": "菲律宾",
    "ID": "印尼", "IN": "印度", "GB": "英国", "DE": "德国", "FR": "法国", "NL": "荷兰",
    "RU": "俄罗斯", "CA": "加拿大", "AU": "澳大利亚", "AE": "阿联酋", "TR": "土耳其",
}
_meta_cache: dict[str, tuple[float, dict]] = {}


async def _fetch_user_info(session_id: str) -> dict:
    """读取访客资料(带缓存):昵称/邮箱/国家。"""
    cached = _meta_cache.get(session_id)
    if cached and time.time() - cached[0] < config.META_CACHE_SECONDS:
        return cached[1]
    try:
        meta = await crisp.get_meta(session_id)
    except Exception:
        log.exception("获取会话 meta 失败: %s", session_id)
        return {}
    geo = (meta.get("device") or {}).get("geolocation") or {}
    country = geo.get("country") or ""
    info = {"昵称": meta.get("nickname"), "邮箱": meta.get("email"), "国家": _COUNTRIES.get(country, country)}
    result = {k: str(v) for k, v in info.items() if v}
    _meta_cache[session_id] = (time.time(), result)
    return result


_BASIC_KEYS = ("邮箱", "国家", "来源页面")


async def _fetch_source_page(session_id: str) -> str:
    """访客发起会话时所在的页面地址(需要插件的 conversation:pages 读权限)。"""
    try:
        pages = await crisp.get_pages(session_id)
    except Exception:
        log.exception("获取访客来源页面失败: %s", session_id)
        return ""
    for p in pages:  # 最新的页面在前
        url = str(p.get("page_url") or "").strip()
        if url:
            return url
    return ""


def _format_info(info: dict, html: bool = False) -> str:
    def value(key: str, raw: str) -> str:
        if not html:
            return raw
        escaped = html_lib.escape(raw)
        if key == "邮箱":
            return f"<code>{escaped}</code>"
        if key == "来源页面" and raw.startswith(("http://", "https://")):
            return f'<a href="{escaped}">{escaped}</a>'
        return escaped

    return "\n".join(
        f"{k}: {value(k, info[k])}"
        for k in _BASIC_KEYS
        if info.get(k)
    )


def _iso_timestamp(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value or "").timestamp()
    except (TypeError, ValueError):
        return 0


async def _topic_is_stale(sess: dict) -> bool:
    """Crisp 长期复用 session 时，判断是否应为本次回访新建 TG 话题。"""
    rollover_after = config.TOPIC_ROLLOVER_HOURS * 3600
    if rollover_after <= 0:
        return False
    topic_created = _iso_timestamp(
        sess.get("topic_created_at") or sess.get("created_at")
    )
    last_activity = await db.last_activity_at(sess["session_id"])
    activity_anchor = max(topic_created, last_activity)
    return bool(activity_anchor and time.time() - activity_anchor >= rollover_after)


async def _send_new_session_notice(topic_id: int, info: dict, session_id: str):
    page = await _fetch_source_page(session_id)
    if page:
        info = {**info, "来源页面": page}
    await tg.send(
        topic_id,
        f"🆕 新会话\n{_format_info(info, html=True)}\n\n"
        "直接在本话题回复即可以客服身份发给访客(会自动暂停 AI)。\n"
        "命令: /ai on|off  /close  /info",
        parse_mode="HTML",
    )


async def _ensure_session(
    session_id: str,
    rollover_if_stale: bool = False,
    force_new_topic: bool = False,
) -> dict:
    sess = await db.get_session(session_id)
    if sess and not force_new_topic and (
        not rollover_if_stale or not await _topic_is_stale(sess)
    ):
        return sess
    async with _create_lock:
        sess = await db.get_session(session_id)
        if sess and not force_new_topic and (
            not rollover_if_stale or not await _topic_is_stale(sess)
        ):
            return sess
        info = await _fetch_user_info(session_id)
        nickname = info.get("昵称") or "访客"
        email = info.get("邮箱") or ""
        title = f"{nickname} {email}".strip() or session_id[:24]
        topic_id = await tg.create_topic(title)
        if sess:
            sess = await db.update_session_topic(
                session_id, topic_id, nickname, email
            )
            log.info(
                "回访会话已创建新 TG 话题: session=%s topic=%s",
                session_id,
                topic_id,
            )
        else:
            sess = await db.create_session(
                session_id, topic_id, nickname, email
            )
        await _send_new_session_notice(topic_id, info, session_id)
        return sess


async def _to_topic(session_id: str, topic_id: int, func, *args, **kwargs):
    """向会话话题发送;话题被人工删除时自动重建话题再发一次。"""
    try:
        return await func(topic_id, *args, **kwargs)
    except tg.TopicGone:
        log.warning("话题已不存在,为会话重建: session=%s 旧topic=%s", session_id, topic_id)
        sess = await _ensure_session(session_id, force_new_topic=True)
        return await func(sess["topic_id"], *args, **kwargs)


def _parse_content(data: dict) -> tuple[str | None, str | None]:
    """解析消息内容 → (文本, 图片URL);非文本非图片返回 (占位符, None)。"""
    mtype = data.get("type", "text")
    content = data.get("content")
    if mtype == "text" and isinstance(content, str):
        return content, None
    if isinstance(content, dict) and content.get("url") and str(content.get("type", "")).startswith("image"):
        return None, content["url"]
    return f"[{mtype} 消息]", None


async def _on_user_message(data: dict):
    session_id = data.get("session_id")
    if not session_id or _dup_event(data):
        return
    mtype = data.get("type", "text")
    text, image_url = _parse_content(data)

    sess = await _ensure_session(session_id, rollover_if_stale=True)
    if image_url:
        await db.add_message(session_id, "user", "[访客发来一张图片]")
        await _to_topic(
            session_id, sess["topic_id"], tg.send_photo,
            image_url,
            "🔵【访客消息】\n访客发来图片",
        )
    else:
        await db.add_message(session_id, "user", text)
        await _to_topic(
            session_id, sess["topic_id"], tg.send,
            f"🔵【访客消息】\n{text}",
        )

    if not image_url and mtype != "text":
        return
    if not sess["ai_enabled"]:
        # 人工接管超时后自动恢复 AI
        resume_after = config.AI_RESUME_MINUTES * 60
        last_human = sess.get("last_human_at") or 0
        if resume_after <= 0 or time.time() - last_human < resume_after:
            return
        await db.set_ai(session_id, True)
        sess["ai_enabled"] = 1
        await _to_topic(
            session_id, sess["topic_id"], tg.send,
            f"⏰ 距上次人工回复已超过 {config.AI_RESUME_MINUTES:.0f} 分钟,AI 自动恢复接管",
        )

    _queue_answer(session_id, text, image_url)


# ---------- 回答合并(降低 LLM 调用):访客连发消息只答一次 ----------

_answer_timers: dict[str, asyncio.Task] = {}
_pending_msgs: dict[str, list[str]] = {}

_PUNCT_RE = re.compile(r"[\s,,。.!!??~～]+")
_GREETINGS = {"你好", "您好", "在吗", "在么", "在不在", "有人吗", "有人么", "哈喽",
              "你好呀", "您好呀", "hi", "hello", "客服"}


_GREET_REPLIES = {
    "casual": [
        "您好,请问有什么可以帮您?",
        "您好呀,有什么问题直接跟我说哈~",
        "在的哈,您说。",
        "您好,遇到什么问题了吗?我帮您看下。",
        "在呢,有什么可以帮您的哈?",
    ],
    "formal": [
        "您好,请问有什么可以帮到您?",
        "您好,很高兴为您服务,请讲。",
        "您好,请问您遇到了什么问题呢?",
        "您好,请描述一下您需要协助的问题,我来为您处理。",
    ],
}


def _is_greeting(t: str) -> bool:
    return _PUNCT_RE.sub("", t.lower()) in _GREETINGS


def _queue_answer(session_id: str, text: str | None, image_url: str | None = None):
    _pending_msgs.setdefault(session_id, []).append({"text": text, "image": image_url})
    old = _answer_timers.get(session_id)
    if old and not old.done():
        old.cancel()
    _answer_timers[session_id] = asyncio.create_task(_flush_answer(session_id))


async def _flush_answer(session_id: str):
    try:
        await asyncio.sleep(max(config.ANSWER_DEBOUNCE_SECONDS, 0))
    except asyncio.CancelledError:
        return
    pending = _pending_msgs.pop(session_id, [])
    texts = [m["text"] for m in pending if m.get("text")]
    images = [m["image"] for m in pending if m.get("image")]
    if not texts and not images:
        return
    sess = await db.get_session(session_id)
    if not sess or not sess["ai_enabled"]:
        return  # 等待期间被人工接管,丢给人工处理
    if not images and all(_is_greeting(t) for t in texts):
        # 纯寒暄:随机话术池,不消耗 LLM,也避免每次回复一字不差
        pool = _GREET_REPLIES.get(config.PERSONA_STYLE, _GREET_REPLIES["casual"])
        greet = random.choice(pool)
        _note_sent(session_id, greet)
        await crisp.send_text(session_id, greet)
        await db.add_message(session_id, "assistant", greet)
        await _to_topic(
            session_id, sess["topic_id"], tg.send,
            "🟢【AI 回复】\n"
            "类型：寒暄\n"
            "────────────\n"
            f"{greet}",
        )
        return
    query = "\n".join(texts) or "访客发来了图片,请识别并处理"
    await _ai_reply(sess, session_id, query, images)


async def _ai_reply(sess: dict, session_id: str, query: str, images: list[str] | None = None):
    history = await db.get_history(session_id)
    chunks = kb.retrieve(query)
    user_info = await _fetch_user_info(session_id)
    result = await llm.answer(history, chunks, user_info, images)
    ans, conf = result["answer"], result["confidence"]
    sources = "、".join(result["sources"]) or "无"

    if ans and conf >= config.CONFIDENCE_THRESHOLD:
        _note_sent(session_id, ans)
        await crisp.send_text(session_id, ans)
        await db.add_message(session_id, "assistant", ans)
        msg_ids = await _to_topic(
            session_id, sess["topic_id"], tg.send,
            "🟢【AI 回复】\n"
            f"置信度：{conf:.2f} ｜ 依据：{sources}\n"
            "────────────\n"
            f"{ans}",
        )
        await db.add_ai_answer(msg_ids, session_id, query, ans, result["sources"])
    else:
        mention = await _mention_all()
        mention_line = f"{mention}\n" if mention else ""
        body = (
            "🔴【等待人工】\n"
            f"{mention_line}"
            f"置信度：{conf:.2f} ｜ 未发送给访客\n"
            "────────────\n"
            f"<pre>{html_lib.escape(ans or '(无草稿)')}</pre>"
        )
        try:
            await _to_topic(session_id, sess["topic_id"], tg.send, body, parse_mode="HTML")
        except Exception:
            # HTML 解析失败就退回纯文本(只 @ 有用户名的)
            admin_line = f"@{config.TG_ADMIN}\n" if config.TG_ADMIN else ""
            await _to_topic(
                session_id, sess["topic_id"], tg.send,
                "🔴【等待人工】\n"
                f"{admin_line}"
                f"置信度：{conf:.2f} ｜ 未发送给访客\n"
                "────────────\n"
                f"{ans or '(无草稿)'}",
            )
        if config.LOW_CONF_REPLY:
            _note_sent(session_id, config.LOW_CONF_REPLY)
            await crisp.send_text(session_id, config.LOW_CONF_REPLY)
            await db.add_message(session_id, "assistant", config.LOW_CONF_REPLY)


# ---------- 转人工时 @ 群管理员(10 分钟缓存) ----------

_admin_cache: tuple[float, str] | None = None


async def _mention_all() -> str:
    global _admin_cache
    if _admin_cache and time.time() - _admin_cache[0] < 600:
        return _admin_cache[1]
    parts = []
    for u in await tg.get_admins():
        if u.get("is_bot"):
            continue
        if config.MENTION_SKIP_CREATOR and u.get("status") == "creator":
            continue
        if u.get("username"):
            parts.append(f"@{u['username']}")
        else:
            name = html_lib.escape(u.get("first_name") or "同事")
            parts.append(f'<a href="tg://user?id={u["id"]}">{name}</a>')
    if not parts and config.TG_ADMIN:
        parts = [f"@{config.TG_ADMIN}"]
    mentions = " ".join(parts)
    _admin_cache = (time.time(), mentions)
    return mentions


# ---------- 人工回复后延时自动学习(防抖) ----------

_learn_timer: asyncio.Task | None = None


def _schedule_learn():
    global _learn_timer
    if config.LEARN_DEBOUNCE_MINUTES <= 0:
        return
    if _learn_timer and not _learn_timer.done():
        _learn_timer.cancel()
    _learn_timer = asyncio.create_task(_delayed_learn())


async def _delayed_learn():
    try:
        await asyncio.sleep(config.LEARN_DEBOUNCE_MINUTES * 60)
    except asyncio.CancelledError:
        return
    await learn.run_and_notify(force=True)


# ---------- 暂停会话巡检:超时自动恢复并补答积压消息 ----------

async def resume_watcher():
    while True:
        await asyncio.sleep(30)
        try:
            await _check_paused_sessions()
        except Exception:
            log.exception("暂停会话巡检失败")


async def _check_paused_sessions():
    resume_after = config.AI_RESUME_MINUTES * 60
    if resume_after <= 0:
        return
    for sess in await db.paused_sessions():
        session_id = sess["session_id"]
        if time.time() - (sess["last_human_at"] or 0) < resume_after:
            continue
        await db.set_ai(session_id, True)
        sess = dict(sess)
        sess["ai_enabled"] = 1
        last = await db.last_message(session_id)
        # 只有「最后一条是访客发的文本且没人回」才需要补答,否则静默恢复
        if not last or last["role"] != "user" or last["content"].startswith("["):
            continue
        await _to_topic(
            session_id, sess["topic_id"], tg.send,
            f"⏰ 距上次人工回复已超过 {config.AI_RESUME_MINUTES:.0f} 分钟,AI 自动恢复接管,补答积压消息",
        )
        await _ai_reply(sess, session_id, last["content"])


async def _on_operator_message(data: dict):
    """客服直接在 Crisp 后台回复:镜像到 TG 并暂停 AI(跳过我们自己发的)。"""
    session_id = data.get("session_id")
    if not session_id or _dup_event(data):
        return
    mtype = data.get("type", "text")
    text, image_url = _parse_content(data)
    if image_url:
        sess = await _ensure_session(session_id)
        await db.add_message(session_id, "human", "[客服发送了一张图片]")
        await db.touch_human(session_id)
        paused = await db.pause_ai_if_enabled(session_id)
        await _to_topic(
            session_id, sess["topic_id"], tg.send_photo,
            image_url,
            "🟠【人工回复】\n客服发送图片",
        )
        if paused:
            await _to_topic(session_id, sess["topic_id"], tg.send, "⏸ AI 自动回复已暂停（/ai on 恢复）")
        return
    if _was_sent_by_us(session_id, text):
        return
    sess = await _ensure_session(session_id)
    await db.add_message(session_id, "human", text)
    if mtype == "text":
        question = await db.last_user_message(session_id)
        await db.add_kb_candidate(session_id, question, text)
        _schedule_learn()
    await db.touch_human(session_id)
    paused = await db.pause_ai_if_enabled(session_id)
    await _to_topic(
        session_id, sess["topic_id"], tg.send,
        "🟠【人工回复】\n"
        f"{text}"
        + ("\n⏸ AI 自动回复已暂停（/ai on 恢复）" if paused else ""),
    )


# ---------- 知识库投喂话题 ----------

_kb_topic_cache: int | None = None
_feed_history: deque = deque(maxlen=12)

KB_TOPIC_WELCOME = (
    "📖 这里是知识库投喂话题,内容不会发给任何访客。直接和我说:\n"
    "· 投喂:「新品上架,下单满 99 包邮」\n"
    "· 问答式:「问:支持支付宝吗 答:支持」\n"
    "· 查询:「知识库里续费是怎么说的?」\n"
    "· 修正:「续费的答案改成 xxx」\n"
    "我会整理成条目入库并复述给你确认,收录内容优先级与人工纠错同级。"
)


async def _get_kb_topic() -> int | None:
    global _kb_topic_cache
    if _kb_topic_cache is None:
        v = await db.get_setting("kb_topic_id")
        _kb_topic_cache = int(v) if v else 0
    return _kb_topic_cache or None


async def _setup_kb_topic():
    global _kb_topic_cache
    existing = await _get_kb_topic()
    if existing:
        await tg.send(existing, "📖 投喂话题就是这里,直接说就行。")
        return
    tid = await tg.create_topic("📖 知识库投喂")
    await db.set_setting("kb_topic_id", str(tid))
    _kb_topic_cache = tid
    await tg.send(tid, KB_TOPIC_WELCOME)


async def _handle_kb_feed(thread_id: int, text: str, image_url: str | None = None):
    _feed_history.append(("user", f"[发送了图片] {text}".strip() if image_url else text))
    result = await llm.kb_manage(
        list(_feed_history), kb.retrieve(text, k=6), [image_url] if image_url else None
    )
    reply = result["reply"] or "(没有理解到内容,再说一次?)"
    entries = result["entries"]
    deletes = result.get("deletes") or []
    # 删除:显式要求作废的 + 同标题旧条目(新条目覆盖旧条目,避免重复堆积)
    to_remove = [*deletes, *(e["title"] for e in entries)]
    removed = kb.remove_entries(to_remove) if to_remove else []
    if entries:
        _FILES = {
            "knowledge": ("人工投喂.md", "# 人工投喂(管理员审定,与其他条目冲突时优先)\n\n"),
            "operation": ("人工介入.md", "# 人工介入(操作类场景,命中即转人工,AI 不自答)\n\n"),
        }
        detail_lines = []
        for etype in ("knowledge", "operation"):
            batch = [e for e in entries if e.get("type", "knowledge") == etype]
            if not batch:
                continue
            fname, header_text = _FILES[etype]
            path = config.KB_DIR / fname
            header = "" if path.exists() else header_text
            with open(path, "a", encoding="utf-8") as f:
                f.write(header + "\n\n".join(f"## {e['title']}\n\n{e['content']}" for e in batch) + "\n\n")
            tag = "🤝 " if etype == "operation" else "· "
            detail_lines += [f"{tag}{e['title']}" for e in batch]
        n = kb.load()
        detail = "\n".join(detail_lines)
        reply += f"\n\n✅ 已入库 {len(entries)} 条(知识库现有 {n} 个条目):\n{detail}"
    if removed:
        reply += "\n\n🗑 已删除旧条目 %d 条:\n%s" % (
            len(removed),
            "\n".join(f"· {r}" for r in removed[:10]),
        )
    elif deletes:
        reply += "\n\n⚠️ 没找到要删除的条目(标题需与知识库里的完全一致):\n" + "\n".join(
            f"· {d}" for d in deletes[:5]
        )
    await tg.send(thread_id, reply)
    _feed_history.append(("assistant", reply))


async def _handle_keycheck(thread_id: int | None):
    """手动测试:并发实测全部 LLM 端点并回结果(/test 或 /keycheck)。"""
    await tg.send(thread_id, "🔑 正在测试全部模型接口,约 10~45 秒…")
    results = await llm.ping_endpoints()
    await tg.send(thread_id, llm.format_ping(results, "🔑 接口测试(手动)"))


# ---------- TG → Crisp ----------

def _extract_photo(msg: dict) -> str | None:
    """取消息里的图片 file_id:相册图片取最大尺寸;以文件方式发送的图片、静态贴纸也认。"""
    photos = msg.get("photo") or []
    if photos:
        return photos[-1]["file_id"]
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id")
    sticker = msg.get("sticker") or {}
    if sticker and not sticker.get("is_animated") and not sticker.get("is_video"):
        return sticker.get("file_id")
    return None


async def handle_tg_message(msg: dict):
    if msg.get("chat", {}).get("id") != config.TG_GROUP_ID:
        return
    thread_id = msg.get("message_thread_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    photo_id = _extract_photo(msg)
    if not text and not photo_id:
        return
    cmd0 = text.split()[0].split("@")[0].lower() if text else ""
    if cmd0 == "/kbtopic":
        await _setup_kb_topic()
        return
    if cmd0 in ("/test", "/keycheck"):  # 任意话题(含 General)可用,当场测试全部接口
        await _handle_keycheck(thread_id)
        return
    if not thread_id:
        return
    if thread_id == await _get_kb_topic():
        log.info("投喂消息: text=%r photo=%s 字段=%s", text[:60], bool(photo_id), sorted(msg.keys()))
        unreadable = {"video", "animation", "video_note", "voice", "audio", "sticker", "document"}
        if not photo_id and unreadable & set(msg):
            await tg.send(thread_id, "⚠️ 这个附件格式我读不了,请以图片形式发送(相册选图或直接粘贴截图)")
            return
        image_url = await tg.file_url(photo_id) if photo_id else None
        await _handle_kb_feed(thread_id, text, image_url)
        return
    if not text:
        return  # 会话话题里暂只转发文字给访客
    sess = await db.get_by_topic(thread_id)
    if not sess:
        return

    cmd_word = text.split()[0].split("@")[0].lower()
    if cmd_word in ("/good", "/bad", "/add"):
        await _handle_rating(thread_id, msg.get("reply_to_message"), text)
        return
    if text.startswith("/"):
        await _handle_command(sess, thread_id, text)
        return

    session_id = sess["session_id"]
    try:
        await crisp.send_text(session_id, text)
    except Exception:
        log.exception("TG 人工回复发送到 Crisp 失败: %s", session_id)
        await tg.send(thread_id, "❌ 人工回复发送失败，请重试")
        return
    _note_sent(session_id, text)
    await db.add_message(session_id, "human", text)
    # 学习闭环:人工答案与其对应的用户问题一起入库,待提炼进知识库
    question = await db.last_user_message(session_id)
    await db.add_kb_candidate(session_id, question, text)
    _schedule_learn()

    await db.touch_human(session_id)
    paused = await db.pause_ai_if_enabled(session_id)
    if paused:
        await tg.send(thread_id, "✅ 人工回复发送成功\n⏸ AI 自动回复已暂停（/ai on 恢复）")


async def _handle_rating(thread_id: int, reply: dict | None, text: str):
    """引用回复某条 🤖 消息 + /good 或 /bad [纠正答案],对 AI 回答打分/纠错。"""
    if not reply:
        await tg.send(
            thread_id,
            "用法:引用回复某条 🤖 AI 回答,再发:\n"
            "/good — 好答案,巩固进知识库\n"
            "/bad 正确答案 — 答错了,整条替换\n"
            "/add 补充内容 — 答得对但不全,合并完善",
        )
        return
    rec = await db.get_ai_answer_by_msg(reply.get("message_id"))
    if not rec:
        await tg.send(thread_id, "⚠️ 这条不是可打分的 AI 回答,请引用回复 🤖 开头的消息")
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    extra = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/good":
        await db.rate_ai_answer(rec["id"], "good")
        await db.add_kb_candidate(rec["session_id"], rec["question"], rec["answer"])
        _schedule_learn()
        await tg.send(thread_id, "👍 已标记好答案,稍后自动巩固进知识库")
    elif cmd == "/add":
        if not extra:
            await tg.send(thread_id, "用法:引用回复 AI 回答,发 /add 要补充的内容")
            return
        await db.rate_ai_answer(rec["id"], "improved")
        merged = await llm.merge_answer(rec["question"], rec["answer"], extra)
        if not merged:
            await tg.send(thread_id, "❌ 合并失败,请稍后重试")
            return
        removed = _append_correction(rec["question"], merged, rec["sources"] or "")
        await tg.send(
            thread_id,
            f"✏️ 回答模版已更新并立即生效,之后按此口径回复:\n\n{merged}"
            + _removed_note(removed),
        )
    else:  # /bad
        await db.rate_ai_answer(rec["id"], "bad")
        if extra:
            removed = _append_correction(rec["question"], extra, rec["sources"] or "")
            await tg.send(
                thread_id,
                "👎 回答模版已替换并立即生效,后续同类问题按新模版回复" + _removed_note(removed),
            )
        else:
            await tg.send(thread_id, "👎 已标记差答案。想直接纠正,引用回复:/bad 正确答案内容")


def _append_correction(question: str, correction: str, stale_sources: str = "") -> list[str]:
    """纠错答案写入纠错库并热加载;同时清掉产生错误答案的旧条目,避免旧话术继续命中。"""
    title = (question or "未知问题").strip().splitlines()[0][:60]
    # 「文件 · 标题」形式的来源取标题部分;同标题旧条目一并覆盖
    stale = [s.split(" · ", 1)[-1].strip() for s in (stale_sources or "").split("|") if s.strip()]
    removed = kb.remove_entries([title, *stale])
    path = config.KB_DIR / "纠错.md"
    header = "" if path.exists() else "# 纠错(人工修正,与其他条目冲突时以本文件为准)\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{header}## {title}\n\n{correction}\n\n")
    kb.load()
    return removed


def _removed_note(removed: list[str]) -> str:
    if not removed:
        return ""
    return "\n\n🗑 同时清掉了导致旧答案的条目 %d 条:\n%s" % (
        len(removed),
        "\n".join(f"· {r}" for r in removed[:8]),
    )


async def _handle_command(sess: dict, thread_id: int, text: str):
    session_id = sess["session_id"]
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd == "/ai":
        if arg == "on":
            await db.set_ai(session_id, True)
            await tg.send(thread_id, "▶️ AI 自动回复已恢复")
        elif arg == "off":
            await db.set_ai(session_id, False)
            await tg.send(thread_id, "⏸ AI 自动回复已暂停")
        else:
            state = "开启" if sess["ai_enabled"] else "暂停"
            await tg.send(thread_id, f"当前 AI 状态: {state}(用 /ai on 或 /ai off 切换)")
    elif cmd == "/close":
        await crisp.set_state(session_id, "resolved")
        await db.set_ai(session_id, True)
        await tg.send(thread_id, "✅ 会话已结案(resolved),AI 已恢复")
    elif cmd == "/learn":
        await tg.send(thread_id, "📚 正在提炼人工回答…")
        await learn.run_and_notify(force=True, thread_id=thread_id)
    elif cmd == "/info":
        info = await _fetch_user_info(session_id)
        page = await _fetch_source_page(session_id)
        if page:
            info = {**info, "来源页面": page}
        await tg.send(
            thread_id,
            f"session: {session_id}\n{_format_info(info)}\n"
            f"AI: {'开启' if sess['ai_enabled'] else '暂停'}",
        )
    else:
        await tg.send(thread_id, "未知命令。可用: /ai on|off  /close  /info  /learn")
