"""Crisp REST API 客户端(以客服/插件身份操作会话)。"""
import httpx

from . import config

_client = httpx.AsyncClient(
    base_url="https://api.crisp.chat/v1",
    auth=(config.CRISP_IDENTIFIER, config.CRISP_KEY),
    headers={"X-Crisp-Tier": config.CRISP_TIER},
    timeout=15,
)


def _conv(session_id: str, path: str = "") -> str:
    return f"/website/{config.CRISP_WEBSITE_ID}/conversation/{session_id}{path}"


async def send_text(session_id: str, text: str):
    r = await _client.post(
        _conv(session_id, "/message"),
        json={"type": "text", "from": "operator", "origin": "chat", "content": text},
    )
    r.raise_for_status()


async def get_meta(session_id: str) -> dict:
    r = await _client.get(_conv(session_id, "/meta"))
    r.raise_for_status()
    return r.json().get("data") or {}


async def get_pages(session_id: str) -> list[dict]:
    """访客浏览过的页面(最新在前);插件缺少 pages 读权限时返回空列表。"""
    r = await _client.get(_conv(session_id, "/pages"))
    if not r.is_success:
        return []
    return r.json().get("data") or []


async def set_state(session_id: str, state: str = "resolved"):
    r = await _client.patch(_conv(session_id, "/state"), json={"state": state})
    r.raise_for_status()
