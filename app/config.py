import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# 优先加载工作目录的 .env(多实例共用代码时,各实例在自己目录下运行)
_cwd_env = Path.cwd() / ".env"
load_dotenv(_cwd_env if _cwd_env.exists() else BASE_DIR / ".env")

# 实例名称：区分多套 Crisp 客服机器人，并提供给 LLM 作为本站身份。
BOT_NAME = os.getenv("BOT_NAME", "").strip()

# Crisp
CRISP_IDENTIFIER = os.getenv("CRISP_IDENTIFIER", "")
CRISP_KEY = os.getenv("CRISP_KEY", "")
CRISP_TIER = os.getenv("CRISP_TIER", "plugin")
CRISP_WEBSITE_ID = os.getenv("CRISP_WEBSITE_ID", "")
CRISP_WEBHOOK_SECRET = os.getenv("CRISP_WEBHOOK_SECRET", "")

# Telegram
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_GROUP_ID = int(os.getenv("TG_GROUP_ID", "0") or "0")
TG_ADMIN = os.getenv("TG_ADMIN", "").lstrip("@")
# 转人工 @ 全体管理员时跳过群主(群主不想被每条低置信度消息打扰)
MENTION_SKIP_CREATOR = os.getenv("MENTION_SKIP_CREATOR", "0") not in ("", "0", "false", "False")

# LLM(OpenAI 协议,base_url 可指向 new-api 中转)
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.5")
LLM_PROTOCOL = os.getenv("LLM_PROTOCOL", "openai").strip().lower()
# 备用 LLM:主密钥调用失败时按序自动切换(LLM_API_KEY_BACKUP、LLM_API_KEY_BACKUP2…);
# base_url/model 不填则复用主的
LLM_BACKUPS: list[dict] = []
for _i in ("", "2", "3", "4"):
    _key = os.getenv(f"LLM_API_KEY_BACKUP{_i}", "")
    if _key:
        LLM_BACKUPS.append(
            {
                "api_key": _key,
                "base_url": os.getenv(f"LLM_BASE_URL_BACKUP{_i}", ""),
                "model": os.getenv(f"LLM_MODEL_BACKUP{_i}", ""),
                # openai(默认) 或 anthropic:后者走 Claude 原生 /v1/messages
                "protocol": os.getenv(f"LLM_PROTOCOL_BACKUP{_i}", "openai").strip().lower(),
            }
        )

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
# 人工接管后,距最后一条人工回复超过该分钟数则 AI 自动恢复;0 = 不自动恢复
AI_RESUME_MINUTES = float(os.getenv("AI_RESUME_MINUTES", "30"))
# 自动学习:每隔多少小时把人工回答提炼进知识库;积累多少条候选才启动一轮
LEARN_INTERVAL_HOURS = float(os.getenv("LEARN_INTERVAL_HOURS", "6"))
LEARN_MIN_CANDIDATES = int(os.getenv("LEARN_MIN_CANDIDATES", "3"))
# 人工回复后多少分钟自动触发一轮学习(防抖,连续回复只触发最后一次);0 = 关闭
LEARN_DEBOUNCE_MINUTES = float(os.getenv("LEARN_DEBOUNCE_MINUTES", "5"))
# 访客连发消息合并:收到消息后等待 N 秒再统一回答;0 = 立即逐条回答
ANSWER_DEBOUNCE_SECONDS = float(os.getenv("ANSWER_DEBOUNCE_SECONDS", "8"))
# 访客资料(meta)缓存秒数,期间不重复调 Crisp API
META_CACHE_SECONDS = float(os.getenv("META_CACHE_SECONDS", "300"))
# Crisp 浏览器可能长期复用同一 session；静默超过此时长后回访时新建 TG 话题。
TOPIC_ROLLOVER_HOURS = float(os.getenv("TOPIC_ROLLOVER_HOURS", "72"))
# 客服人设风格: casual(口语化) / formal(正式官方)
PERSONA_STYLE = os.getenv("PERSONA_STYLE", "casual")
# 本地对话文字保留小时数,过期自动删除(Crisp 云端有完整记录);0 = 永久保留
DATA_RETENTION_HOURS = float(os.getenv("DATA_RETENTION_HOURS", "24"))
# 每隔多少小时巡检主/备用 LLM 密钥连通性并在 TG 通报;0 = 关闭
KEY_CHECK_INTERVAL_HOURS = float(os.getenv("KEY_CHECK_INTERVAL_HOURS", "6"))
# 巡检时每个端点真实消耗的 token 上限(出字才能验证额度是否够用)
KEY_CHECK_MAX_TOKENS = int(os.getenv("KEY_CHECK_MAX_TOKENS", "2048"))
# 每日总结:推送整点(按 DAILY_REPORT_TZ 时区,默认北京时间 23 点);-1 = 关闭
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "23"))
DAILY_REPORT_TZ = float(os.getenv("DAILY_REPORT_TZ", "8"))
LOW_CONF_REPLY = os.getenv("LOW_CONF_REPLY", "")

KB_DIR = Path(os.getenv("KB_DIR", str(BASE_DIR / "kb")))
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db")))
PORT = int(os.getenv("PORT", "8300"))
