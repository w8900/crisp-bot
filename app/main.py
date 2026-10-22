"""入口:FastAPI 接收 Crisp webhook + 后台任务跑 TG 长轮询。

启动: uvicorn app.main:app --host 0.0.0.0 --port 8300
"""
import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Response

from . import bot, config, db, kb, learn, llm, tg

log = logging.getLogger("crisp-bot")


def _verify_signature(headers, body: bytes) -> bool:
    """支持逗号分隔的多把密钥(生产/开发钩子过渡期并存)。"""
    secrets = [s.strip() for s in config.CRISP_WEBHOOK_SECRET.split(",") if s.strip()]
    if not secrets:
        return True
    ts = headers.get("x-crisp-request-timestamp", "")
    sig = headers.get("x-crisp-signature", "")
    signed = f"[{ts};{body.decode('utf-8', 'replace')}]".encode()
    for i, secret in enumerate(secrets, 1):
        expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expect, sig):
            log.debug("webhook 验签通过(密钥 #%d)", i)
            return True
    return False


def _report_text(day_label: str, s: dict) -> str:
    return (
        f"📊 客服日报（{day_label}，近 24 小时）\n"
        f"新会话：{s['new_sessions']}\n"
        f"访客消息：{s['visitor_messages']}\n"
        f"AI 回复：{s['ai_replies']}\n"
        f"人工回复：{s['human_replies']} 条 / {s['human_sessions']} 个会话\n"
        f"低置信度转人工：{s['low_conf']}\n"
        f"关键词人工介入：{s['keyword_handoff']}\n"
        f"AI 好评：{s['ai_good']}\n"
        f"AI 差评：{s['ai_bad']}\n"
        f"AI 完善：{s['ai_improved']}\n"
        f"知识候选：{s['kb_candidates']}\n"
        f"当前待学习：{s['pending_learning']}"
    )


async def _daily_report_loop():
    """每天在配置时区的指定整点,把当日统计推送到 TG 群(General)。"""
    tz = timezone(timedelta(hours=config.DAILY_REPORT_TZ))
    log.info("日报调度已启动：UTC%+g 每天 %02d:00", config.DAILY_REPORT_TZ, config.DAILY_REPORT_HOUR)
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=config.DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        log.info("下次日报推送时间：%s", target.isoformat())
        await asyncio.sleep((target - now).total_seconds())
        try:
            since = target - timedelta(hours=24)
            stats = await db.daily_stats(since.astimezone(timezone.utc).isoformat())
            await tg.send(None, _report_text(target.strftime("%m-%d"), stats))
            log.info("每日报告推送成功：%s", target.strftime("%Y-%m-%d"))
        except Exception:
            log.exception("每日总结推送失败")


async def _keycheck_loop():
    """定时实测主/备用 LLM 密钥,结果通报到 TG 群(General)。"""
    while True:
        try:
            results = await llm.ping_endpoints()
            await tg.send(
                None,
                llm.format_ping(
                    results,
                    f"🔑 密钥巡检(每 {config.KEY_CHECK_INTERVAL_HOURS:.0f} 小时)",
                ),
            )
        except Exception:
            log.exception("密钥巡检失败")
        await asyncio.sleep(config.KEY_CHECK_INTERVAL_HOURS * 3600)


async def _cleanup_loop():
    while True:
        try:
            if config.DATA_RETENTION_HOURS > 0:
                n = await db.purge_old(config.DATA_RETENTION_HOURS)
                if n:
                    log.info("已清理 %d 条过期本地记录(保留 %.0f 小时)", n, config.DATA_RETENTION_HOURS)
        except Exception:
            log.exception("清理过期数据失败")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await db.init()
    await tg.register_commands()
    n = kb.load()
    log.info("知识库已加载 %d 个条目", n)
    poll_task = asyncio.create_task(tg.poll(bot.handle_tg_message))
    learn_task = asyncio.create_task(learn.loop())
    resume_task = asyncio.create_task(bot.resume_watcher())
    cleanup_task = asyncio.create_task(_cleanup_loop())
    tasks = [poll_task, learn_task, resume_task, cleanup_task]
    if config.KEY_CHECK_INTERVAL_HOURS > 0:
        tasks.append(asyncio.create_task(_keycheck_loop()))
    if config.DAILY_REPORT_HOUR >= 0:
        tasks.append(asyncio.create_task(_daily_report_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook/crisp")
async def crisp_webhook(request: Request):
    body = await request.body()
    if not _verify_signature(request.headers, body):
        log.warning(
            "webhook 验签失败 ts=%s sig=%s body[:80]=%s",
            request.headers.get("x-crisp-request-timestamp"),
            request.headers.get("x-crisp-signature"),
            body[:80],
        )
        return Response(status_code=401)
    try:
        payload = json.loads(body)
    except Exception:
        return Response(status_code=400)
    # 立刻返回 200,异步处理,避免 Crisp 侧超时重试
    asyncio.create_task(bot.handle_crisp_event(payload))
    return {"success": True}
