from app.genre import DEFAULT_GENRE, dimensions_for, is_registry_only
from app.scoring import score_document

# 公司标准模板的元信息块原样：括号别名标签 + 无标题的 Markdown 版本表。
# 这两种写法此前都识别不了，导致标准模板自己被判"缺版本历史 + 标签不合规"。
STANDARD_METADATA = """# **文档元信息（Metadata）**
- 版本号：V1.0
- 适用范围：研发体系
- 标签（Tags）：知识库，文档标准，文档规范

| 版本号 | 修订日期 | 修改内容 | 修改人 |
|---------|------------|------------|---------|
| V1.0 | 2025-08-13 | 初始版本发布 | 韩文瀚 |
"""


def test_v11_score_returns_auditable_dimensions():
    result = score_document("接口文档.md", "摘要\n一句话。\n\n1、开始\n内容\n", genre="规范制度")
    assert result["ai_score"] < 100
    assert result["verdict"] in {"pass", "return"}  # 人工复核挡位已取消
    assert set(result["dimensions"]) == {"metadata", "abstract", "structure",
                                         "retrieval", "format", "rag", "quality"}
    assert result["findings"]


def test_cleaner_document_uses_expected_filename_and_score_range():
    content = """文档信息
版本：V1.1
标签：接口、发布、规范
适用范围：研发中心
版本历史
版本 日期 修改 作者
V1.1 2026-08-03 首次发布 张三
摘要
接口发布规范说明接口发布前的检查事项。
1. 目的
本文说明接口发布规范。
【重点】发布前完成接口校验。
2. 流程
本文说明发布流程。
术语表
API：应用程序接口。
参考资料：https://example.test
"""
    result = score_document("接口发布规范_V1.1.md", content, genre="规范制度")
    assert 0 <= result["ai_score"] <= 100
    assert result["fingerprint"]


def test_bracketed_tags_and_untitled_version_table_are_recognized():
    """标准模板不该被自己的标准判不合格。"""
    metadata = score_document("知识库文档标准_通用_V1.1.adoc", STANDARD_METADATA,
                              "native_doc", genre="规范制度")["dimensions"]["metadata"]
    assert metadata["deduction"] == 0
    assert metadata["findings"] == []


def test_dimension_cap_truncates_deduction_but_never_the_finding_list():
    """cap 用尽后问题照常上报（deduction=0 + capped），否则用户只看得到第一个
    问题，改完分数不动，整改闭环断裂。"""
    metadata = score_document("测试文档.md", "随便一点正文内容。",
                              genre="规范制度")["dimensions"]["metadata"]
    assert metadata["deduction"] == metadata["cap"] == 5
    rules = {f["rule"] for f in metadata["findings"]}
    assert {"1.1", "1.2a", "1.3", "1.4"} <= rules
    capped = [f for f in metadata["findings"] if f["capped"]]
    assert capped and all(f["deduction"] == 0 for f in capped)


def test_genre_selects_dimensions_and_normalizes_the_range():
    """同一份内容，不同文体用不同尺子；分数对适用维度归一化后可比。"""
    scores = {}
    for genre in ("规范制度", "方案设计", "通知公告"):
        result = score_document("某文档.docx", STANDARD_METADATA, "document", genre=genre)
        assert set(result["applicable_dimensions"]) == set(dimensions_for(genre, "document"))
        assert result["applicable_cap"] == sum(
            result["dimensions"][k]["cap"] for k in result["applicable_dimensions"])
        scores[genre] = result["ai_score"]
    # 约束越弱、适用维度越少，同一份内容得分越高——这正是"不是所有文档
    # 都有那么严格的要求"的落点。
    assert scores["通知公告"] >= scores["规范制度"]


def test_sheet_and_slide_drop_dimensions_their_form_cannot_support():
    assert set(dimensions_for("规范制度", "sheet")) == {"format", "quality"}
    slide = set(dimensions_for("规范制度", "slide"))
    assert "metadata" not in slide and "abstract" not in slide and "rag" not in slide
    assert "structure" in slide  # 演示文稿仍然要有层次


def test_registry_only_genres_produce_no_score():
    result = score_document("物料BOM清单.xlsx", "型号 数量", "sheet", genre="数据台账")
    assert is_registry_only("数据台账")
    assert result["registry_only"] is True
    assert result["ai_score"] is None and result["verdict"] == "registered"


def test_default_genre_is_the_middle_tier():
    """72% 的文档从文件名判不出文体，兜底档不能是最严的那一档。"""
    assert DEFAULT_GENRE == "其他知识文档"
    assert "metadata" not in dimensions_for(DEFAULT_GENRE, "document")
