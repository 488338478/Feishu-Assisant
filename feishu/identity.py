"""Deterministic identity selection for Feishu requests."""

import re


_PERSONAL_MARKERS = ("我的", "本人", "我")
_DOCUMENT_DISCOVERY_ACTIONS = ("搜索", "搜一下", "查找", "找一下", "寻找", "定位")
_DOCUMENT_RESOURCE_TERMS = (
    "文档",
    "知识库",
    "wiki",
    "gdd",
    "周报",
    "策划",
    "手记",
    "报告",
    "方案",
    "表格",
    "报表",
)
_DOMAIN_TERMS = (
    ("minutes", ("会议纪要", "妙记", "录音纪要")),
    ("calendar", ("日程", "日历", "行程")),
    ("task", ("任务", "待办")),
    ("vc", ("会议", "参会", "视频会议")),
    ("mail", ("邮件", "邮箱")),
    ("attendance", ("考勤", "打卡")),
    ("contact", ("个人信息", "通讯录信息", "联系方式")),
)


def required_user_domain(text: str) -> str | None:
    """Return the user-auth domain required before running the agent."""
    normalized = re.sub(r"\s+", "", text or "").lower()
    if (
        any(action in normalized for action in _DOCUMENT_DISCOVERY_ACTIONS)
        and any(term in normalized for term in _DOCUMENT_RESOURCE_TERMS)
    ):
        return "docs"
    if not any(marker in normalized for marker in _PERSONAL_MARKERS):
        return None
    for domain, terms in _DOMAIN_TERMS:
        if any(term in normalized for term in terms):
            return domain
    return None


def auth_required_domain(text: str) -> str | None:
    """Extract the private runtime marker emitted after a bot auth failure."""
    match = re.search(
        r"\[LARK_USER_AUTH_REQUIRED:(docs|drive|wiki|calendar|task|vc|minutes|mail|attendance|contact|im)\]",
        text or "",
    )
    return match.group(1) if match else None
