import io
import os
import zipfile

os.environ["KG_DATABASE_URL"] = "sqlite:///./runtime/test_knowledge_governance.db"
os.environ["KG_DEMO_MODE"] = "true"

from app.config import get_settings
from app.content import extract_text
from app.db import Document, SessionLocal, Workspace, init_db
from app.service import run_review


def zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_docx_extraction():
    data = zip_bytes({"word/document.xml":
                      "<w:document xmlns:w='ns'><w:body><w:p><w:r><w:t>第一段标题</w:t></w:r></w:p>"
                      "<w:p><w:r><w:t>第二段正文内容</w:t></w:r></w:p></w:body></w:document>"})
    text = extract_text("docx", data)
    assert "第一段标题" in text and "第二段正文内容" in text
    assert text.index("第一段标题") < text.index("第二段正文内容")


def test_xlsx_extraction():
    data = zip_bytes({
        "xl/sharedStrings.xml": "<sst xmlns='http://x'><si><t>表头甲</t></si><si><t>数值乙</t></si></sst>",
        "xl/worksheets/sheet1.xml": "<worksheet xmlns='http://x'><sheetData><row><c><is><t>行内丙</t></is></c></row></sheetData></worksheet>",
    })
    text = extract_text("xlsx", data)
    assert "表头甲" in text and "数值乙" in text and "行内丙" in text


def test_pptx_extraction():
    data = zip_bytes({"ppt/slides/slide1.xml":
                      "<p:sld xmlns:a='http://a' xmlns:p='http://p'><a:t>幻灯片标题</a:t></p:sld>"})
    assert "幻灯片标题" in extract_text("pptx", data)


def test_plain_text_and_fallbacks():
    assert extract_text("txt", "中文文本".encode("utf-8")) == "中文文本"
    assert extract_text("md", "中文gbk".encode("gb18030")) == "中文gbk"
    assert extract_text("doc", b"legacy binary") == ""      # unsupported legacy format
    assert extract_text("docx", b"not a zip at all") == ""  # malformed archive degrades


def test_sheet_class_uses_reduced_rule_subset():
    from app.scoring import score_document

    cells = "序号\n物料编码\n数量\n交期"
    sheet = score_document("GPON再生产订单推进表任务模板 2.xlsx", cells, "sheet")
    assert set(sheet["dimensions"]) <= {"format", "quality"}  # no abstract/structure/RAG penalties
    document = score_document("GPON再生产订单推进表任务模板 2.xlsx", cells, "document")
    assert "structure" in document["dimensions"]
    assert sheet["ai_score"] >= document["ai_score"]


def test_fetch_degrades_without_numeric_id():
    import asyncio

    from app.content import fetch_document_content

    init_db()
    settings = get_settings().model_copy(update={"content_extract_enabled": True,
                                                 "wiki_storage_space_id": "2932890480"})
    doc = Document(node_id="content-nonum", workspace_id="content-ws", name="老文件.docx",
                   extension="docx", file_class="document", storage_dentry_id="")
    text, source = asyncio.run(fetch_document_content(settings, doc))
    assert text == "" and source == "no_numeric_id"


def test_model_score_recomputes_verdict(monkeypatch):
    import app.content as content_module
    from app import service
    from app.db import ModelConfig

    async def fake_fetch(settings, doc):
        return ("规范正文，规则分本会很高。", "storage_download")

    async def fake_model(config, content, filename, file_class="document"):
        return {"score": 35, "findings": [{"rule": "model", "deduction": 0, "message": "内容质量差。"}]}

    monkeypatch.setattr(content_module, "fetch_document_content", fake_fetch)
    monkeypatch.setattr(service, "model_score_content", fake_model)
    init_db()
    with SessionLocal() as db:
        if not db.get(Workspace, "content-ws"):
            db.add(Workspace(workspace_id="content-ws", name="抽取测试库"))
        if not db.scalar(__import__("sqlalchemy").select(ModelConfig).where(ModelConfig.name == "test-model")):
            db.add(ModelConfig(name="test-model", base_url="http://x", model_name="m", api_key="k",
                               enabled=True, version="test-v1"))
        if not db.get(Document, "content-2"):
            db.add(Document(node_id="content-2", workspace_id="content-ws", name="判级测试_V1.0.docx",
                            extension="docx", file_class="document"))
        db.commit()
        settings = get_settings().model_copy(update={"model_allow_content_transfer": True})
        instance = service.run_review(db, settings, "content-2", "test")
        model_block = instance.dimensions["model"]
        assert model_block["model_score"] == 35 and model_block["rule_score"] > 35
        expected = round(0.4 * model_block["rule_score"] + 0.6 * 35)
        assert instance.ai_score == expected  # composite, not a silent override
        assert instance.verdict == ("pass" if expected >= 70 else "manual_review" if expected >= 60 else "return")
        assert instance.model_config_version == "test-v1"
        db.query(ModelConfig).filter(ModelConfig.name == "test-model").delete(synchronize_session=False)
        db.commit()


def test_genre_demotes_document_rules_to_advisory(monkeypatch):
    import app.content as content_module
    from app import service
    from app.db import ModelConfig

    async def fake_fetch(settings, doc):
        return ("步骤一：连接设备。\n步骤二：执行命令。\n步骤三：检查输出。", "storage_download")

    async def fake_model(config, content, filename, file_class="document"):
        return {"score": 90, "genre": "测试用例",
                "dimensions": {"要素完整": 14, "准确清晰": 14, "结构可读": 13, "规范性": 13, "文体专属": 36},
                "findings": []}

    monkeypatch.setattr(content_module, "fetch_document_content", fake_fetch)
    monkeypatch.setattr(service, "model_score_content", fake_model)
    init_db()
    with SessionLocal() as db:
        if not db.get(Workspace, "content-ws"):
            db.add(Workspace(workspace_id="content-ws", name="抽取测试库"))
        db.add(ModelConfig(name="genre-model", base_url="http://x", model_name="m", api_key="k",
                           enabled=True, version="genre-v1"))
        if not db.get(Document, "content-3"):
            db.add(Document(node_id="content-3", workspace_id="content-ws", name="登录流程测试用例.docx",
                            extension="docx", file_class="document"))
        db.commit()
        settings = get_settings().model_copy(update={"model_allow_content_transfer": True})
        instance = service.run_review(db, settings, "content-3", "test")
        dims = instance.dimensions
        assert dims["model"]["genre"] == "测试用例"
        assert any(dims.get(key, {}).get("advisory") for key in ("metadata", "structure", "abstract", "rag"))
        # Advisory deductions do not count: rule score ignores document-shaped dims.
        counted = sum(d["deduction"] for k, d in dims.items() if k != "model" and not d.get("advisory"))
        assert dims["model"]["rule_score"] == 100 - counted
        assert instance.ai_score == round(0.4 * dims["model"]["rule_score"] + 0.6 * 90)
        db.query(ModelConfig).filter(ModelConfig.name == "genre-model").delete(synchronize_session=False)
        db.commit()


def test_run_review_uses_extracted_content(monkeypatch):
    import app.content as content_module

    async def fake_fetch(settings, doc):
        return ("文档信息\n版本号：V1.0\n适用范围：测试\n标签：a, b, c\n\n摘要\n本文验证抽取。\n\n"
                "1. 背景\n抽取测试正文段落。\n2. 结论\n抽取生效。", "storage_download")

    monkeypatch.setattr(content_module, "fetch_document_content", fake_fetch)
    init_db()
    with SessionLocal() as db:
        if not db.get(Workspace, "content-ws"):
            db.add(Workspace(workspace_id="content-ws", name="抽取测试库"))
        if not db.get(Document, "content-1"):
            db.add(Document(node_id="content-1", workspace_id="content-ws", name="抽取测试_V1.0.docx",
                            extension="docx", file_class="document"))
        db.commit()
        instance = run_review(db, get_settings(), "content-1", "test")
        assert instance.review_scope == "full_content"
        assert instance.content_fingerprint  # sha256 of the ephemeral body
