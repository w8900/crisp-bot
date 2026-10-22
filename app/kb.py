"""知识库:加载 kb/ 下的 Markdown,按二级标题分块,字符二元组重叠度检索(对中文友好、零依赖)。"""
import logging
import re

from . import config

log = logging.getLogger("crisp-bot.kb")

_chunks: list[dict] = []
_escalations: list[dict] = []


def load() -> int:
    global _chunks, _escalations
    _chunks, _escalations = [], []
    if not config.KB_DIR.exists():
        log.warning("知识库目录不存在: %s", config.KB_DIR)
        return 0
    for path in sorted(config.KB_DIR.glob("**/*.md")):
        if path.name.startswith("."):
            continue  # 跳过 macOS ._* 等隐藏文件
        # 「人工介入」库是操作类转人工场景,单独存放,不参与普通检索
        target = _escalations if path.stem.startswith("人工介入") else _chunks
        text = path.read_text(encoding="utf-8", errors="ignore")
        parts = re.split(r"(?m)^##\s+", text)
        if parts[0].strip():
            target.append({"source": path.stem, "text": parts[0].strip()})
        for part in parts[1:]:
            heading = part.partition("\n")[0].strip()
            target.append({"source": f"{path.stem} · {heading}", "text": part.strip()})
    return len(_chunks) + len(_escalations)


def escalations() -> list[dict]:
    return _escalations


def _norm(title: str) -> str:
    """标题归一化:忽略空白、标点和全半角差异,便于跨文件匹配。"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower())


def _split(text: str) -> tuple[str, list[tuple[str, str]]]:
    """拆成 (文件头部, [(标题, 正文块)])。"""
    parts = re.split(r"(?m)^##\s+", text)
    return parts[0], [(p.partition("\n")[0].strip(), p) for p in parts[1:]]


def remove_entries(titles: list[str]) -> list[str]:
    """按标题跨文件删除条目(用于投喂话题里「删掉这条答案」),返回被删标题。"""
    targets = {_norm(t) for t in titles if t and t.strip()}
    if not targets:
        return []
    removed: list[str] = []
    for path in sorted(config.KB_DIR.glob("**/*.md")):
        if path.name.startswith("."):
            continue
        head, blocks = _split(path.read_text(encoding="utf-8", errors="ignore"))
        kept = []
        for heading, block in blocks:
            if _norm(heading) in targets:
                removed.append(f"{path.stem} · {heading}")
            else:
                kept.append(block)
        if len(kept) != len(blocks):
            path.write_text(head + "".join(f"## {b}" for b in kept), encoding="utf-8")
    if removed:
        load()
    return removed


# 同一标题跨文件重复时的保留优先级(数字越大越权威)
_PRIORITY = {"纠错": 4, "人工投喂": 3, "人工介入": 3, "人工回复提炼": 2}


def dedupe() -> list[str]:
    """同标题条目只保留最权威的一条(纠错 > 人工投喂 > 提炼 > 常见问题;同档取最新)。"""
    best: dict[str, tuple[int, int, str]] = {}  # 标题 → (优先级, 序号, 文件)
    order = 0
    files: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    for path in sorted(config.KB_DIR.glob("**/*.md")):
        if path.name.startswith("."):
            continue
        head, blocks = _split(path.read_text(encoding="utf-8", errors="ignore"))
        files[str(path)] = (head, blocks)
        prio = _PRIORITY.get(path.stem, 1)
        for heading, _ in blocks:
            order += 1
            key = _norm(heading)
            if key not in best or (prio, order) > best[key][:2]:
                best[key] = (prio, order, str(path))

    dropped: list[str] = []
    order = 0
    for path_str, (head, blocks) in files.items():
        kept = []
        for heading, block in blocks:
            order += 1
            key = _norm(heading)
            if best.get(key) == (best[key][0], order, path_str):
                kept.append(block)
            else:
                dropped.append(heading)
        if len(kept) != len(blocks):
            from pathlib import Path

            Path(path_str).write_text(head + "".join(f"## {b}" for b in kept), encoding="utf-8")
    if dropped:
        load()
    return dropped


def titles() -> list[str]:
    return [c["source"] for c in _chunks]


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s.lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def retrieve(query: str, k: int = 4) -> list[dict]:
    q = _bigrams(query)
    if not q:
        return []
    scored = []
    for c in _chunks:
        overlap = len(q & _bigrams(c["text"])) / len(q)
        if overlap > 0.05:
            # 人工审定条目(纠错/人工投喂)相关性加权,但不无条件置顶
            if c["source"].startswith(("纠错", "人工投喂")):
                overlap *= 1.25
            scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:k]]
