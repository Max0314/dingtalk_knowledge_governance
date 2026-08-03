"""Versioned, explainable implementation of 知识库文档评分标准_通用-V1.1.

It intentionally uses deterministic heuristics so each deduction is auditable. A configured
LLM may enrich findings later, but can never silently replace the stored rule score.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, asdict


RULE_VERSION = "V1.1"
PASS_SCORE, RETURN_SCORE = 70, 60


@dataclass
class Finding:
    rule: str
    deduction: int
    message: str


def _chars(text: str) -> set[str]:
    return {c for c in text.lower() if "\u4e00" <= c <= "\u9fff" or c.isalnum()}


def _section(text: str, names: tuple[str, ...]) -> str:
    match = re.search(r"(?:^|\n)\s*(?:#+\s*)?(?:" + "|".join(names) + r")\s*[:：]?\s*\n?(.{0,800})", text, re.I)
    return match.group(1) if match else ""


def score_document(name: str, content: str) -> dict:
    """Return only structured results; callers must not persist ``content``."""
    findings: list[Finding] = []
    dimensions: dict[str, dict] = {}

    def chapter(key: str, label: str, cap: int, checks: list[tuple[bool, int, str, str]]):
        local: list[Finding] = []
        remaining = cap
        for violated, points, rule, message in checks:
            if violated and remaining:
                take = min(points, remaining)
                local.append(Finding(rule, take, message))
                remaining -= take
        findings.extend(local)
        dimensions[key] = {"label": label, "deduction": sum(x.deduction for x in local), "cap": cap, "findings": [asdict(x) for x in local]}

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    meta = _section(content, ("文档信息", "基本信息", "元数据")) + "\n" + content[:1000]
    tags_match = re.search(r"(?:标签|Tags)\s*[:：]\s*([^\n]+)", meta, re.I)
    tags = re.split(r"[,，、;；\s]+", tags_match.group(1).strip()) if tags_match else []
    history = _section(content, ("版本历史", "修订历史", "变更记录"))
    history_fields = sum(1 for x in ("版本", "日期", "修改", "作者") if x in history)
    chapter("metadata", "元数据完整性", 5, [
        (not re.search(r"(?:版本号|版本)\s*[:：]\s*V\d+\.\d+", meta, re.I), 5, "1.1", "未识别到 Vx.x 版本号。"),
        (not history, 3, "1.2", "缺少版本历史表或版本记录。"),
        (bool(history) and history_fields < 4, 4 - history_fields, "1.2", "版本历史字段不完整。"),
        (len([x for x in tags if x]) < 3 or len(tags) > 8, 2, "1.3", "标签数量不在 3–8 个范围内。"),
        (not re.search(r"(?:适用范围|范围)\s*[:：]\s*\S+", meta), 2, "1.4", "缺少适用范围。"),
    ])
    abstract = _section(content, ("摘要", "概述", "简介"))
    body = content.replace(abstract, "", 1)
    overlap = len(_chars(abstract) & _chars(body)) / max(1, len(_chars(abstract))) if abstract else 0
    chapter("abstract", "摘要质量", 2, [
        (bool(abstract) and len(re.findall(r"[。！？!?]", abstract)) > 5, 1, "2.1", "摘要超过 5 句。"),
        (not abstract or overlap < 0.20, 1, "2.2", "摘要与正文关键词重合度低于 20%。"),
    ])
    headings = [line for line in lines if re.match(r"^(?:#+\s*)?(?:\d+(?:\.\d+){0,3}|[一二三四五六七八九十]+、)", line)]
    long_paras = [line for line in lines if not line.startswith("#") and len(re.sub(r"\s", "", line)) > 150]
    intro_missing = 0
    for i, heading in enumerate(headings):
        try:
            next_line = lines[lines.index(heading) + 1]
            if len(next_line) < 12:
                intro_missing += 1
        except IndexError:
            intro_missing += 1
    chapter("structure", "结构完整性", 10, [
        (len(headings) < 2, 4, "3.1", "未识别到多级编号结构。"),
        (len(headings) >= 2 and not any("." in x for x in headings), 2, "3.2", "章节编号层级不清晰。"),
        (intro_missing > 0, min(6, intro_missing * 2), "3.3", "部分章节缺少引导句。"),
        (bool(long_paras), min(6, len(long_paras) * 2), "3.4", "存在超过 150 个中文字符的长段落。"),
    ])
    title_terms = [x for x in re.split(r"[\s_\-（()【】]+", re.sub(r"_V\d+\.\d+$", "", name)) if len(x) >= 2]
    uncommon = [x for x in title_terms if content.lower().count(x.lower()) < 2]
    abbrs = set(re.findall(r"\b[A-Z]{2,}\b", content))
    undefined = [x for x in abbrs if not re.search(r"[\u4e00-\u9fffA-Za-z]{2,}[（(]" + re.escape(x) + r"[)）]|" + re.escape(x) + r"[（(][\u4e00-\u9fffA-Za-z]{2,}[)）]", content)]
    chapter("retrieval", "可检索性", 5, [
        (bool(uncommon), min(5, len(uncommon)), "4.1", "标题核心词在正文出现不足 2 次。"),
        (bool(undefined), min(3, len(undefined)), "4.2", "存在未在首次出现处释义的英文缩写。"),
        ("【重点】" not in content and "Note:" not in content, 1, "4.3", "缺少重点或 Note 标记。"),
    ])
    dates = re.findall(r"\b\d{4}[./]\d{1,2}[./]\d{1,2}\b", content)
    chapter("format", "命名与格式", 5, [
        (not re.search(r"_V\d+\.\d+(?:\.[A-Za-z0-9]+)?$", name, re.I), 3, "5.1", "文件名不符合 标题_Vx.x 格式。"),
        (bool(re.search(r"[<>:\\|?*]", name)), 1, "5.2", "文件名包含不建议的特殊字符。"),
        (bool(dates), min(2, len(dates)), "5.3", "日期未统一使用 YYYY-MM-DD。"),
        ((bool(re.search(r"\bMb\b|\bGB\b", content)) and bool(re.search(r"\bMB\b|\bGb\b", content))), 1, "5.4", "数据单位大小写不一致。"),
    ])
    sentences = [x.strip() for x in re.split(r"[。！？!?\n]", content) if len(x.strip()) > 12]
    incoherent = sum(1 for a, b in zip(sentences, sentences[1:]) if len(_chars(a) & _chars(b)) / max(1, len(_chars(a) | _chars(b))) < 0.04)
    tech = bool(re.search(r"API|接口|架构|部署|代码|技术", content, re.I))
    has_data = bool(re.search(r"\d+(?:\.\d+)?%|数据|结论", content))
    chapter("rag", "RAG 优化", 3, [
        (incoherent >= 4, min(3, incoherent), "6.1", "相邻主题语义关联弱，建议拆分主题。"),
        (tech and not re.search(r"术语表|名词解释|Glossary", content, re.I), 1, "6.2", "技术类文档缺少术语表。"),
        (has_data and not re.search(r"参考资料|引用|来源|https?://", content, re.I), 1, "6.3", "数据或结论缺少可追溯引用。"),
    ])
    typo_hits = sum(content.count(x) for x in ("的的", "了了", "以及及", "因该", "在再"))
    term_inconsistent = int("代码评审" in content and "代码审查" in content) + int("知识库" in content and "知识库库" in content)
    contradiction = int(bool(re.search(r"版本\s*[:：]\s*V(\d+\.\d+)", meta)) and bool(history) and not re.search(r"V\d+\.\d+", history))
    chapter("quality", "错别字与问题表述", 10, [
        (typo_hits > 0, min(4, typo_hits), "7.1", "检测到疑似错别字或重复用词。"),
        (term_inconsistent > 0, min(4, term_inconsistent * 2), "7.2", "检测到术语表达不一致。"),
        (contradiction > 0, min(2, contradiction * 2), "7.3", "版本信息与历史记录存在矛盾。"),
    ])
    deduction = sum(item["deduction"] for item in dimensions.values())
    score = max(0, 100 - deduction)
    verdict = "pass" if score >= PASS_SCORE else "manual_review" if score >= RETURN_SCORE else "return"
    return {"ai_score": score, "verdict": verdict, "dimensions": dimensions, "findings": [asdict(x) for x in findings], "fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""}
