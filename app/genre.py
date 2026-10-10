"""文体（genre）判定与约束档位。

分类分三层（2026-08-21 定稿，取代"一把尺子量所有文档"）：

  载体 form   扩展名决定（见 app.fileclass）——哪些维度**物理上**适用。
              表格没有章节引导句，演示文稿没有摘要段。
  文体 genre  正文语义决定，模型判定优先；文件名只作弱先验。
              53,908 个真实节点实测：命名可识别文体的比例只有 27.9%，
              所以文件名和扩展名都不能当主判据。
  档位 tier   由文体映射，决定适用维度与形式分权重。

档位语义：
  A 强约束（治理资产）  要被反复检索执行、错了有后果 —— 全套形式要求
  B 中约束（工程资产）  要能被别人接手 —— 结构、可检索、术语一致
  C 弱约束（过程记录）  时效性内容 —— 只看要素齐全，不评摘要/RAG/术语表
  D 仅登记              不出分，只记归档状态

实证依据：同一批真实文档，用 V1.1 全维度评分数跨度只有 6 分（75–81 全部
通过）；按文体裁剪维度并归一化后跨度拉开到 42 分，且排序符合人的判断——
通知按通知的标准合格，制度按制度的标准确实缺版本历史与术语表。
"""
from __future__ import annotations

import re

# 档位 -> 适用评分维度（app.scoring.RULE_CATALOG 的键）。
# B 档刻意不含 metadata：工程资产要结构和可检索性，但强求版本历史表
# 只会制造噪声——这一点在原型验证里直接决定了分数排序是否讲得通。
TIER_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "A": ("metadata", "abstract", "structure", "retrieval", "format", "rag", "quality"),
    "B": ("structure", "retrieval", "format", "quality"),
    "C": ("format", "quality"),
    "D": (),
}

# 形式分（确定性规则分）在总分里的权重，其余归模型内容分。
# 约束越弱，形式越不重要——会议纪要写得规不规范远不如决议清不清楚重要。
TIER_FORM_WEIGHT: dict[str, float] = {"A": 0.40, "B": 0.25, "C": 0.10, "D": 0.0}

TIER_LABEL: dict[str, str] = {
    "A": "强约束·治理资产", "B": "中约束·工程资产",
    "C": "弱约束·过程记录", "D": "仅登记",
}

# 载体能支撑的维度上限：与档位取交集后才是真正参与评分的维度。
FORM_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "native_doc": ("metadata", "abstract", "structure", "retrieval", "format", "rag", "quality"),
    "document": ("metadata", "abstract", "structure", "retrieval", "format", "rag", "quality"),
    "text": ("metadata", "abstract", "structure", "retrieval", "format", "rag", "quality"),
    # 表格提取出的是无序单元格文本，文档形态的维度全部不适用。
    "sheet": ("format", "quality"),
    # 演示文稿有层次和检索需求，但没有摘要段和 RAG 切片语义。
    "slide": ("structure", "retrieval", "format", "quality"),
}
DEFAULT_FORM_DIMENSIONS: tuple[str, ...] = ("format", "quality")

GENRE_TIERS: dict[str, str] = {
    # A 强约束
    "规范制度": "A", "操作手册": "A", "需求规格": "A",
    # B 中约束
    "方案设计": "B", "测试用例": "B", "培训资料": "B", "其他知识文档": "B",
    # C 弱约束
    "测试报告": "C", "会议纪要": "C", "数据报告": "C",
    "汇报总结": "C", "通知公告": "C", "问题记录": "C",
    # D 仅登记
    "数据台账": "D", "合同法务": "D", "参考资料": "D",
}


# 每个文体的专属评审要点。与 GENRE_TIERS 同处一个文件，是为了让"文体清单"
# 只有一个源头——组织里三份部门标准一字不差的教训说明，同一套标准抄成多份
# 之后就再没人同步过。模型提词由 app.integrations 从这里生成，不另抄一份。
GENRE_RUBRICS: dict[str, str] = {
    "规范制度": "适用范围明确、版本与生效信息完整、条款可执行不含糊、与现行流程无冲突",
    "操作手册": "步骤完整可跟随、关键处有示例说明、异常与回退处理、面向读者无前置知识断层",
    "需求规格": "需求边界清楚、验收标准可判定、优先级与依赖明确、条目之间不自相矛盾",
    "方案设计": "背景与目标清楚、方案对比或选型依据充分、实施计划可落地、风险与对策",
    "测试用例": "前置条件与环境说明、步骤可复现、每步预期结果与判定标准明确、覆盖点清晰",
    "培训资料": "学习目标明确、由浅入深、有例子或练习、术语首次出现有解释",
    "其他知识文档": "按通用四维从严评估",
    "测试报告": "结论先行且明确、测试环境与范围交代完整、数据支撑结论、遗留问题与风险如实列出",
    "会议纪要": "决议明确不含糊、每项决议有责任人与时限、遗留议题清楚",
    "数据报告": "数据来源与统计口径说明、结论与数据一致、图表可读",
    "汇报总结": "周期与范围清楚、进展与问题分开陈述、下一步可执行",
    "通知公告": "生效时间与适用对象明确、要求具体、附出处或原文链接",
    "问题记录": "现象与复现步骤清楚、影响范围明确、根因与处理结论、状态可追踪",
    "数据台账": "结构化数据资产，仅登记不评分",
    "合同法务": "走独立的合同风险审查流程，本服务仅登记不评分",
    "参考资料": "外部或引用资料，仅登记不评分",
}

DEFAULT_GENRE = "其他知识文档"

# 模型可能返回近义写法；归一到 GENRE_TIERS 的规范名，避免因用词漂移
# 掉回默认档（掉档会让一份制度被当成普通知识文档评分）。
GENRE_ALIASES: dict[str, str] = {
    "制度规范": "规范制度", "规章制度": "规范制度", "管理制度": "规范制度",
    "技术标准": "规范制度", "规范": "规范制度", "制度": "规范制度",
    "流程": "操作手册", "流程SOP": "操作手册", "作业指导书": "操作手册",
    "SOP": "操作手册", "用户手册": "操作手册", "使用说明": "操作手册",
    "需求文档": "需求规格", "产品需求": "需求规格", "PRD": "需求规格",
    "技术方案": "方案设计", "设计文档": "方案设计", "架构设计": "方案设计",
    "方案": "方案设计", "培训材料": "培训资料", "课件": "培训资料",
    "周报": "汇报总结", "月报": "汇报总结", "日报": "汇报总结",
    "工作总结": "汇报总结", "通知": "通知公告", "公告": "通知公告",
    "发布说明": "通知公告", "变更说明": "通知公告",
    "问题跟踪": "问题记录", "缺陷记录": "问题记录", "故障记录": "问题记录",
    "台账": "数据台账", "数据表": "数据台账", "清单": "数据台账",
    "合同": "合同法务", "协议": "合同法务",
    "参考文档": "参考资料", "外部资料": "参考资料",
    "其他": "其他知识文档", "知识文档": "其他知识文档",
}

# 文件名弱先验：仅在模型未判定时使用（模型关闭或调用失败）。
# 顺序即优先级，先具体后宽泛——"测试报告"必须排在"问题"之前，
# "管理办法"必须排在"方案"之前。
GENRE_NAME_HINTS: tuple[tuple[str, str], ...] = (
    ("合同法务", r"合同|协议|法务|风险审查|授权书"),
    ("测试用例", r"测试用例|用例|测试项"),
    ("测试报告", r"测试报告|验证报告|测试结果|验证结果|测试记录"),
    ("需求规格", r"需求规格|需求说明|需求文档|PRD|SRS"),
    ("规范制度", r"制度|规范|规程|准则|管理办法|管理方法|管理规定|实施细则|条例|标准"),
    ("操作手册", r"操作手册|作业指导|指导书|SOP|操作说明|使用说明|用户手册|流程"),
    ("会议纪要", r"纪要|会议记录|评审记录"),
    ("汇报总结", r"周报|月报|日报|汇报|总结|复盘|述职|工作计划"),
    ("通知公告", r"通知|公告|变更说明|更新说明|版本说明|发布说明|release|changelog"),
    ("问题记录", r"缺陷|bug|故障|异常|排查|问题清单|问题记录"),
    ("培训资料", r"培训|课件|教程|试题|题库|考题|讲解|分享"),
    ("数据台账", r"台账|BOM|物料|登记表|统计表|收集表|明细表"),
    ("数据报告", r"数据报告|分析报告|统计报告"),
    ("方案设计", r"方案|设计|架构|详设|概设"),
)

_NAME_HINTS = tuple((genre, re.compile(pattern, re.I)) for genre, pattern in GENRE_NAME_HINTS)


def normalize_genre(raw: str) -> str:
    """把模型返回或人工填写的文体名归一到规范名；无法识别返回空串。"""
    value = (raw or "").strip()
    if not value:
        return ""
    if value in GENRE_TIERS:
        return value
    return GENRE_ALIASES.get(value, "")


def genre_from_name(name: str) -> str:
    """文件名弱先验。命中率约 28%，只在没有模型判定时兜底。"""
    base = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", name or "")
    base = re.sub(r"_V\d+(?:\.\d+)*$", "", base, flags=re.I)
    for genre, pattern in _NAME_HINTS:
        if pattern.search(base):
            return genre
    return ""


def classify(name: str, model_genre: str = "") -> tuple[str, str]:
    """返回 (genre, source)。source ∈ model / filename / default。

    source 必须随评审实例留痕：文体决定了用哪套标准，评分的可追溯性
    依赖于"当时判成了什么文体、凭什么判的"。
    """
    resolved = normalize_genre(model_genre)
    if resolved:
        return resolved, "model"
    resolved = genre_from_name(name)
    if resolved:
        return resolved, "filename"
    return DEFAULT_GENRE, "default"


def tier_of(genre: str) -> str:
    return GENRE_TIERS.get(genre or "", GENRE_TIERS[DEFAULT_GENRE])


def is_registry_only(genre: str) -> bool:
    """D 档：不出分、不推送，只登记归档状态。"""
    return tier_of(genre) == "D"


def dimensions_for(genre: str, form: str = "document") -> tuple[str, ...]:
    """档位适用维度 ∩ 载体可支撑维度，保持 RULE_CATALOG 的声明顺序。"""
    allowed = set(TIER_DIMENSIONS[tier_of(genre)])
    supported = set(FORM_DIMENSIONS.get(form or "document", DEFAULT_FORM_DIMENSIONS))
    both = allowed & supported
    return tuple(key for key in TIER_DIMENSIONS["A"] if key in both)


def form_weight_for(genre: str) -> float:
    return TIER_FORM_WEIGHT[tier_of(genre)]


def catalog() -> list[dict]:
    """配置页/文档用的有序文体表。"""
    return [{"genre": g, "tier": t, "tier_label": TIER_LABEL[t],
             "dimensions": list(dimensions_for(g)), "form_weight": TIER_FORM_WEIGHT[t]}
            for g, t in GENRE_TIERS.items()]
