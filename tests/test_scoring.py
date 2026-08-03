from app.scoring import score_document


def test_v11_score_returns_auditable_dimensions():
    result = score_document("接口文档.md", "摘要\n一句话。\n\n1、开始\n内容\n")
    assert result["ai_score"] < 100
    assert result["verdict"] in {"pass", "manual_review", "return"}
    assert set(result["dimensions"]) == {"metadata", "abstract", "structure", "retrieval", "format", "rag", "quality"}
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
    result = score_document("接口发布规范_V1.1.md", content)
    assert 0 <= result["ai_score"] <= 100
    assert result["fingerprint"]
