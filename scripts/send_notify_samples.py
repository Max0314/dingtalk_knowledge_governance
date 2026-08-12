"""Send one example of every review-push message type to a user.

Messages are composed by the SAME builders production uses (app.notify), so
what lands in DingTalk is exactly what uploaders will receive:
  1. 单条 · 评审通过（正反馈）
  2. 单条 · 低分说明（试点期无退回流程）
  3. 汇总 · 短时间大量文件评审完毕后的一条合并消息

Run inside the api container:
    python scripts/send_notify_samples.py                 # 默认发给陈鹏列
    python scripts/send_notify_samples.py --user <userId> # 指定接收人
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.integrations import DingtalkClient
from app.notify import build_message, digest_message

DEFAULT_USER = "01115324500438248944"  # 陈鹏列


def sample(name: str, score: float, verdict: str, findings: list[str] | None = None):
    doc = SimpleNamespace(name=name)
    instance = SimpleNamespace(ai_score=score, verdict=verdict, review_scope="full_content",
                               rule_version="V1.1", findings=[{"message": m} for m in (findings or [])])
    return doc, instance


def main() -> None:
    user = sys.argv[sys.argv.index("--user") + 1] if "--user" in sys.argv else DEFAULT_USER
    messages = [
        build_message(*sample("接口发布规范_V1.0.docx", 88, "pass")),
        build_message(*sample("智能网关IPV6测试步骤.docx", 54, "return", [
            "标题未标注版本号（形如 V1.0）。",
            "存在超过 800 字未分级的大段落。",
            "存在未在首次出现处释义的英文缩写。",
        ])),
        digest_message([
            {"name": "接口发布规范_V1.0.docx", "score": 88, "verdict": "pass"},
            {"name": "数据治理说明_V2.1.docx", "score": 92, "verdict": "pass"},
            {"name": "服务巡检清单_V1.2.md", "score": 76, "verdict": "pass"},
            {"name": "智能网关IPV6测试步骤.docx", "score": 54, "verdict": "return"},
            {"name": "会议纪要20260812.docx", "score": 63, "verdict": "manual_review"},
        ]),
    ]
    client = DingtalkClient(get_settings())
    for title, body in messages:
        asyncio.run(client.send_robot_markdown([user], title, body))
        print("sent:", title)


if __name__ == "__main__":
    main()
