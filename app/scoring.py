"""Versioned, explainable implementation of 知识库文档评分标准_通用-V1.1.

It intentionally uses deterministic heuristics so each deduction is auditable. A configured
LLM may enrich findings later, but can never silently replace the stored rule score.

Every rule's *parameters* (enabled, deduction points, per-rule caps, thresholds) are
configurable per scope (global default / per-department override) via RULE_CATALOG-shaped
config dicts; the detection logic itself stays in code. Defaults reproduce V1.1 exactly.
"""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, asdict


RULE_VERSION = "V1.1"
PASS_SCORE, RETURN_SCORE = 70, 60

# Single source of truth for configurable rule parameters. A rule with "max" is
# count-type (deduction = points × occurrences, capped at max); without it the
# rule is single-shot. "params" are detection thresholds with UI labels and
# clamp ranges. Dimension caps bound the total deduction per dimension.
RULE_CATALOG: dict[str, dict] = {
    "metadata": {"label": "元数据完整性", "cap": 5, "document_only": True, "rules": {
        "1.1": {"label": "版本号标注（Vx.x）", "points": 5},
        "1.2a": {"label": "版本历史存在", "points": 3},
        "1.2b": {"label": "版本历史字段完整（版本/日期/修改/作者，每缺一项）", "points": 1, "max": 4},
        "1.3": {"label": "标签数量范围", "points": 2, "params": {
            "tag_min": {"label": "标签数下限", "default": 3, "min": 0, "max": 20},
            "tag_max": {"label": "标签数上限", "default": 8, "min": 1, "max": 50}}},
        "1.4": {"label": "适用范围标注", "points": 2},
    }},
    "abstract": {"label": "摘要质量", "cap": 2, "document_only": True, "rules": {
        "2.1": {"label": "摘要句数", "points": 1, "params": {
            "max_sentences": {"label": "摘要句数上限", "default": 5, "min": 1, "max": 50}}},
        "2.2": {"label": "摘要与正文关键词重合度", "points": 1, "params": {
            "min_overlap_pct": {"label": "重合度下限%", "default": 20, "min": 0, "max": 100}}},
    }},
    "structure": {"label": "结构完整性", "cap": 10, "document_only": True, "rules": {
        "3.1": {"label": "多级编号结构", "points": 4, "params": {
            "min_headings": {"label": "编号标题数下限", "default": 2, "min": 1, "max": 20}}},
        "3.2": {"label": "章节编号层级", "points": 2},
        "3.3": {"label": "章节引导句（每处缺失）", "points": 2, "max": 6},
        "3.4": {"label": "段落长度控制（每个超长段落）", "points": 2, "max": 6, "params": {
            "para_chars": {"label": "段落字数上限", "default": 150, "min": 50, "max": 2000}}},
    }},
    "retrieval": {"label": "可检索性", "cap": 5, "document_only": True, "rules": {
        "4.1": {"label": "标题核心词在正文出现（每个生僻词）", "points": 1, "max": 5, "params": {
            "min_occurrences": {"label": "最少出现次数", "default": 2, "min": 1, "max": 10}}},
        "4.2": {"label": "英文缩写首次释义（每个未释义）", "points": 1, "max": 3},
        "4.3": {"label": "重点标记（【重点】/Note）", "points": 1},
    }},
    "format": {"label": "命名与格式", "cap": 5, "document_only": False, "rules": {
        "5.1": {"label": "文件名版本格式（标题_Vx.x）", "points": 3},
        "5.2": {"label": "文件名特殊字符", "points": 1},
        "5.3": {"label": "日期格式统一 YYYY-MM-DD（每处）", "points": 1, "max": 2},
        "5.4": {"label": "数据单位大小写一致", "points": 1},
    }},
    "rag": {"label": "RAG 优化", "cap": 3, "document_only": True, "rules": {
        "6.1": {"label": "相邻主题语义连贯", "points": 3, "params": {
            "min_incoherent": {"label": "弱关联句对阈值", "default": 4, "min": 1, "max": 50}}},
        "6.2": {"label": "技术文档术语表", "points": 1},
        "6.3": {"label": "数据结论可追溯引用", "points": 1},
    }},
    "quality": {"label": "错别字与问题表述", "cap": 10, "document_only": False, "rules": {
        "7.1": {"label": "错别字与重复用词（每处）", "points": 1, "max": 4},
        "7.2": {"label": "术语表达一致性（每组不一致）", "points": 2, "max": 4},
        "7.3": {"label": "版本信息与历史一致", "points": 2},
    }},
}


def _clamp(value, lo: float, hi: float, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    number = min(max(number, lo), hi)
    return int(number) if float(number).is_integer() else number


def effective_config(overrides: dict | None = None) -> dict:
    """Catalog defaults deep-merged with (and sanitized against) ``overrides``.

    Unknown keys are dropped and values clamped, so a stored config can never
    crash or distort the engine beyond its intended parameter space. The result
    is always complete — rules added to the catalog later pick up defaults.
    """
    raw = overrides if isinstance(overrides, dict) else {}
    config: dict = {
        "pass_score": _clamp(raw.get("pass_score"), 0, 100, PASS_SCORE),
        "return_score": _clamp(raw.get("return_score"), 0, 100, RETURN_SCORE),
        # None = follow the deployment default (KG_SCORE_RULE_WEIGHT).
        "rule_weight": None if raw.get("rule_weight") in (None, "") else _clamp(raw.get("rule_weight"), 0, 1, None),
        "dimensions": {},
    }
    config["return_score"] = min(config["return_score"], config["pass_score"])
    raw_dims = raw.get("dimensions") if isinstance(raw.get("dimensions"), dict) else {}
    for dim_key, dim_spec in RULE_CATALOG.items():
        raw_dim = raw_dims.get(dim_key) if isinstance(raw_dims.get(dim_key), dict) else {}
        raw_rules = raw_dim.get("rules") if isinstance(raw_dim.get("rules"), dict) else {}
        rules: dict = {}
        for rule_key, rule_spec in dim_spec["rules"].items():
            raw_rule = raw_rules.get(rule_key) if isinstance(raw_rules.get(rule_key), dict) else {}
            entry: dict = {
                "enabled": bool(raw_rule.get("enabled", True)),
                "points": _clamp(raw_rule.get("points"), 0, 100, rule_spec["points"]),
            }
            if "max" in rule_spec:
                entry["max"] = _clamp(raw_rule.get("max"), 0, 100, rule_spec["max"])
            if rule_spec.get("params"):
                raw_params = raw_rule.get("params") if isinstance(raw_rule.get("params"), dict) else {}
                entry["params"] = {pk: _clamp(raw_params.get(pk), ps["min"], ps["max"], ps["default"])
                                   for pk, ps in rule_spec["params"].items()}
            rules[rule_key] = entry
        config["dimensions"][dim_key] = {"cap": _clamp(raw_dim.get("cap"), 0, 100, dim_spec["cap"]), "rules": rules}
    return config


def catalog_dict() -> list[dict]:
    """Ordered, JSON-ready catalog for the configuration page."""
    return [{"key": dim_key, "label": dim["label"], "cap": dim["cap"], "document_only": dim["document_only"],
             "rules": [{"key": rule_key, "label": rule["label"], "points": rule["points"],
                        "max": rule.get("max"), "count_type": "max" in rule,
                        "params": [{"key": pk, "label": ps["label"], "default": ps["default"],
                                    "min": ps["min"], "max": ps["max"]} for pk, ps in rule.get("params", {}).items()]}
                       for rule_key, rule in dim["rules"].items()]}
            for dim_key, dim in RULE_CATALOG.items()]


def verdict_for(score: float, config: dict | None = None) -> str:
    cfg = config or {}
    passing, returning = cfg.get("pass_score", PASS_SCORE), cfg.get("return_score", RETURN_SCORE)
    return "pass" if score >= passing else "manual_review" if score >= returning else "return"


@dataclass
class Finding:
    rule: str
    deduction: int
    message: str


def _chars(text: str) -> set[str]:
    return {c for c in text.lower() if "一" <= c <= "鿿" or c.isalnum()}


def _section(text: str, names: tuple[str, ...]) -> str:
    match = re.search(r"(?:^|\n)\s*(?:#+\s*)?(?:" + "|".join(names) + r")\s*[:：]?\s*\n?(.{0,800})", text, re.I)
    return match.group(1) if match else ""


def score_document(name: str, content: str, file_class: str = "document", config: dict | None = None) -> dict:
    """Return only structured results; callers must not persist ``content``.

    ``file_class`` selects the applicable rule subset: spreadsheets have no
    abstract/chapters/RAG shape, so judging them on document dimensions would
    be systematically unfair — they keep only naming/format and wording rules
    (the model reviewer carries the content judgement).

    ``config`` is an effective_config()-shaped parameter set (None = V1.1
    defaults); detection logic is fixed, only its parameters vary.
    """
    cfg = effective_config(config)

    def param(dim: str, rule: str, key: str):
        return cfg["dimensions"][dim]["rules"][rule]["params"][key]

    findings: list[Finding] = []
    dimensions: dict[str, dict] = {}
    document_shaped = file_class not in ("sheet",)

    def chapter(key: str, checks: list[tuple[str, int, str]]):
        """``checks``: (rule_key, occurrence_count, message); 0 = compliant."""
        dim_cfg = cfg["dimensions"][key]
        local: list[Finding] = []
        remaining = dim_cfg["cap"]
        for rule_key, count, message in checks:
            rule_cfg = dim_cfg["rules"][rule_key]
            if count <= 0 or not rule_cfg["enabled"] or not remaining:
                continue
            take = rule_cfg["points"] * count
            if "max" in rule_cfg:
                take = min(take, rule_cfg["max"])
            take = min(take, remaining)
            if take <= 0:
                continue
            local.append(Finding(rule_key, take, message))
            remaining -= take
        findings.extend(local)
        dimensions[key] = {"label": RULE_CATALOG[key]["label"], "deduction": sum(x.deduction for x in local),
                           "cap": dim_cfg["cap"], "findings": [asdict(x) for x in local]}

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    meta = _section(content, ("文档信息", "基本信息", "元数据")) + "\n" + content[:1000]
    tags_match = re.search(r"(?:标签|Tags)\s*[:：]\s*([^\n]+)", meta, re.I)
    tags = re.split(r"[,，、;；\s]+", tags_match.group(1).strip()) if tags_match else []
    history = _section(content, ("版本历史", "修订历史", "变更记录"))
    history_fields = sum(1 for x in ("版本", "日期", "修改", "作者") if x in history)
    if document_shaped:
        tag_count = len([x for x in tags if x])
        chapter("metadata", [
            ("1.1", int(not re.search(r"(?:版本号|版本)\s*[:：]\s*V\d+\.\d+", meta, re.I)), "未识别到 Vx.x 版本号。"),
            ("1.2a", int(not history), "缺少版本历史表或版本记录。"),
            ("1.2b", (4 - history_fields) if history and history_fields < 4 else 0, "版本历史字段不完整。"),
            ("1.3", int(tag_count < param("metadata", "1.3", "tag_min") or len(tags) > param("metadata", "1.3", "tag_max")),
             f"标签数量不在 {param('metadata', '1.3', 'tag_min')}–{param('metadata', '1.3', 'tag_max')} 个范围内。"),
            ("1.4", int(not re.search(r"(?:适用范围|范围)\s*[:：]\s*\S+", meta)), "缺少适用范围。"),
        ])
    abstract = _section(content, ("摘要", "概述", "简介"))
    body = content.replace(abstract, "", 1)
    overlap = len(_chars(abstract) & _chars(body)) / max(1, len(_chars(abstract))) if abstract else 0
    if document_shaped: chapter("abstract", [
        ("2.1", int(bool(abstract) and len(re.findall(r"[。！？!?]", abstract)) > param("abstract", "2.1", "max_sentences")),
         f"摘要超过 {param('abstract', '2.1', 'max_sentences')} 句。"),
        ("2.2", int(not abstract or overlap < param("abstract", "2.2", "min_overlap_pct") / 100),
         f"摘要与正文关键词重合度低于 {param('abstract', '2.2', 'min_overlap_pct')}%。"),
    ])
    headings = [line for line in lines if re.match(r"^(?:#+\s*)?(?:\d+(?:\.\d+){0,3}|[一二三四五六七八九十]+、)", line)]
    long_paras = [line for line in lines if not line.startswith("#")
                  and len(re.sub(r"\s", "", line)) > param("structure", "3.4", "para_chars")]
    intro_missing = 0
    for i, heading in enumerate(headings):
        try:
            next_line = lines[lines.index(heading) + 1]
            if len(next_line) < 12:
                intro_missing += 1
        except IndexError:
            intro_missing += 1
    min_headings = param("structure", "3.1", "min_headings")
    if document_shaped: chapter("structure", [
        ("3.1", int(len(headings) < min_headings), "未识别到多级编号结构。"),
        ("3.2", int(len(headings) >= min_headings and not any("." in x for x in headings)), "章节编号层级不清晰。"),
        ("3.3", intro_missing, "部分章节缺少引导句。"),
        ("3.4", len(long_paras), f"存在超过 {param('structure', '3.4', 'para_chars')} 个中文字符的长段落。"),
    ])
    title_terms = [x for x in re.split(r"[\s_\-（()【】]+", re.sub(r"_V\d+\.\d+$", "", name)) if len(x) >= 2]
    uncommon = [x for x in title_terms if content.lower().count(x.lower()) < param("retrieval", "4.1", "min_occurrences")]
    abbrs = set(re.findall(r"\b[A-Z]{2,}\b", content))
    undefined = [x for x in abbrs if not re.search(r"[一-鿿A-Za-z]{2,}[（(]" + re.escape(x) + r"[)）]|" + re.escape(x) + r"[（(][一-鿿A-Za-z]{2,}[)）]", content)]
    if document_shaped: chapter("retrieval", [
        ("4.1", len(uncommon), f"标题核心词在正文出现不足 {param('retrieval', '4.1', 'min_occurrences')} 次。"),
        ("4.2", len(undefined), "存在未在首次出现处释义的英文缩写。"),
        ("4.3", int("【重点】" not in content and "Note:" not in content), "缺少重点或 Note 标记。"),
    ])
    dates = re.findall(r"\b\d{4}[./]\d{1,2}[./]\d{1,2}\b", content)
    chapter("format", [
        ("5.1", int(not re.search(r"_V\d+\.\d+(?:\.[A-Za-z0-9]+)?$", name, re.I)), "文件名不符合 标题_Vx.x 格式。"),
        ("5.2", int(bool(re.search(r"[<>:\\|?*]", name))), "文件名包含不建议的特殊字符。"),
        ("5.3", len(dates), "日期未统一使用 YYYY-MM-DD。"),
        ("5.4", int(bool(re.search(r"\bMb\b|\bGB\b", content)) and bool(re.search(r"\bMB\b|\bGb\b", content))), "数据单位大小写不一致。"),
    ])
    sentences = [x.strip() for x in re.split(r"[。！？!?\n]", content) if len(x.strip()) > 12]
    incoherent = sum(1 for a, b in zip(sentences, sentences[1:]) if len(_chars(a) & _chars(b)) / max(1, len(_chars(a) | _chars(b))) < 0.04)
    tech = bool(re.search(r"API|接口|架构|部署|代码|技术", content, re.I))
    has_data = bool(re.search(r"\d+(?:\.\d+)?%|数据|结论", content))
    if document_shaped: chapter("rag", [
        ("6.1", int(incoherent >= param("rag", "6.1", "min_incoherent")), "相邻主题语义关联弱，建议拆分主题。"),
        ("6.2", int(tech and not re.search(r"术语表|名词解释|Glossary", content, re.I)), "技术类文档缺少术语表。"),
        ("6.3", int(has_data and not re.search(r"参考资料|引用|来源|https?://", content, re.I)), "数据或结论缺少可追溯引用。"),
    ])
    typo_hits = sum(content.count(x) for x in ("的的", "了了", "以及及", "因该", "在再"))
    term_inconsistent = int("代码评审" in content and "代码审查" in content) + int("知识库" in content and "知识库库" in content)
    contradiction = int(bool(re.search(r"版本\s*[:：]\s*V(\d+\.\d+)", meta)) and bool(history) and not re.search(r"V\d+\.\d+", history))
    chapter("quality", [
        ("7.1", typo_hits, "检测到疑似错别字或重复用词。"),
        ("7.2", term_inconsistent, "检测到术语表达不一致。"),
        ("7.3", contradiction, "版本信息与历史记录存在矛盾。"),
    ])
    deduction = sum(item["deduction"] for item in dimensions.values())
    score = max(0, 100 - deduction)
    return {"ai_score": score, "verdict": verdict_for(score, cfg), "dimensions": dimensions, "findings": [asdict(x) for x in findings], "fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""}
