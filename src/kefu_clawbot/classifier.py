from __future__ import annotations

from .models import CategoryResult, IncomingEvent

LOGISTICS_KEYWORDS = (
    "快递",
    "物流",
    "发货",
    "没收到",
    "什么时候到",
    "几天到",
    "揽收",
    "运输",
    "派送",
    "签收",
    "催",
    "在路上",
    "单号",
)

WRONG_ITEM_KEYWORDS = (
    "发错",
    "错发",
    "不是这个",
    "不是我要",
    "发错货",
    "发错了",
    "拿错",
    "寄错",
    "颜色不对",
    "尺码不对",
    "款式不对",
    "少发",
    "漏发",
    "缺件",
)

QUALITY_KEYWORDS = (
    "质量",
    "破",
    "破损",
    "开线",
    "脱线",
    "洞",
    "脏",
    "污",
    "瑕疵",
    "掉色",
    "异味",
    "起球",
    "拉链坏",
    "扣子坏",
)


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    if not text:
        return 0
    return sum(1 for token in keywords if token in text)


def classify_event(event: IncomingEvent) -> CategoryResult:
    text = (event.text or "").strip().lower()
    is_image_msg = str(event.message_type or "").strip().lower() == "image"

    if (not text) and (is_image_msg or bool(str(event.media_id or "").strip())):
        return CategoryResult(
            category="unknown_image",
            confidence=0.30,
            reason="image_without_text",
        )

    if not text:
        return CategoryResult(category="other", confidence=0.20, reason="empty_text")

    scores = {
        "logistics": _keyword_hits(text, LOGISTICS_KEYWORDS),
        "wrong_item": _keyword_hits(text, WRONG_ITEM_KEYWORDS),
        "quality": _keyword_hits(text, QUALITY_KEYWORDS),
    }
    top_category = max(scores, key=scores.get)
    top_score = scores[top_category]

    if top_score == 0:
        return CategoryResult(category="other", confidence=0.35, reason="no_keyword_match")

    confidence = min(0.55 + 0.15 * top_score, 0.95)
    return CategoryResult(
        category=top_category,
        confidence=round(confidence, 2),
        reason=f"keyword_match:{top_category}:{top_score}",
    )
