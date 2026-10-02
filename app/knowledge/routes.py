"""Knowledge-base chat (REQ-62, REQ-63): JSON endpoints behind the chat icon on every page."""
from flask import current_app, jsonify, request, url_for
from flask_login import current_user, login_required
from marshmallow import Schema, ValidationError, fields, validate, validates_schema

from app.filters import localtime
from app.knowledge import knowledge_bp, qa
from app.knowledge.embeddings import EmbeddingError
from app.minutes.providers import LLMError
from app.security.audit import record_audit_event

MAX_QUESTION_CHARS = 500
# Earlier turns sent along with a question: enough to follow a conversation, bounded so a long
# chat does not grow the prompt (and its cost) without limit.
MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 2000

DISABLED_MESSAGE = "知識庫功能未啟用（KNOWLEDGE_ENABLED）"

ERROR_MESSAGES = {
    "meeting_not_found": "找不到指定的會議，或您不是這場會議的與會者",
    "project_not_found": "找不到指定的專案，或您沒有參與過這個專案的會議",
    # embedding
    "model_unavailable": "無法載入向量模型（首次使用需連線 Hugging Face 下載），請稍後再試",
    "dimension_mismatch": "向量模型設定錯誤，請聯絡管理者",
    # LLM
    "not_configured": "尚未設定 LLM 金鑰（ANTHROPIC_API_KEY），無法回答問題",
    "auth_failed": "LLM 金鑰無效或權限不足",
    "rate_limited": "LLM 服務請求過於頻繁，請稍後再試",
    "provider_unavailable": "LLM 服務暫時無法使用，請稍後再試",
    "connection_failed": "無法連線到 LLM 服務，請檢查網路",
    "bad_request": "LLM 服務拒絕了這個請求，請查看伺服器紀錄",
    "refused": "AI 基於安全政策拒絕回答這個問題",
    "truncated": "回答過長被截斷，請把問題問得更具體",
}


class AskSchema(Schema):
    question = fields.String(required=True, validate=validate.Length(min=1, max=MAX_QUESTION_CHARS),
                             error_messages={"required": "請輸入問題"})
    date_from = fields.Date(load_default=None)
    date_to = fields.Date(load_default=None)
    meeting_id = fields.String(load_default=None, validate=validate.Length(max=36))
    project_id = fields.String(load_default=None, validate=validate.Length(max=36))

    @validates_schema
    def _range(self, data, **_kwargs):
        if data.get("date_from") and data.get("date_to") and data["date_from"] > data["date_to"]:
            raise ValidationError("開始日期不能晚於結束日期", "date_to")


FIELD_LABELS = {"question": "問題", "date_from": "開始日期", "date_to": "結束日期", "meeting_id": "會議",
                "project_id": "專案", "history": "對話紀錄"}


def _clean_history(raw) -> list[dict]:
    """The last few turns as {"role", "text"}. The browser keeps the chat, so this is user input:
    anything that is not a list of user / assistant texts is rejected, long turns are cut."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError({"history": ["格式不正確"]})
    turns = []
    for item in raw[-MAX_HISTORY_MESSAGES:]:
        if (not isinstance(item, dict) or item.get("role") not in qa.HISTORY_ROLES
                or not isinstance(item.get("text"), str)):
            raise ValidationError({"history": ["格式不正確"]})
        text = item["text"].replace("\r\n", "\n").strip()[:MAX_HISTORY_CHARS]
        if text:
            turns.append({"role": item["role"], "text": text})
    return turns


def _errors(messages, status):
    return jsonify({"errors": messages}), status


def _source(passage: qa.Passage) -> dict:
    """A cited passage for the chat. Only the organiser can open the minutes page, so only they
    get a link to it."""
    own = passage.organizer_id == current_user.id
    return {"number": passage.number, "title": passage.title, "date": passage.date, "project": passage.project,
            "section": passage.section, "content": qa.plain_text(passage.content),  # shown as plain text
            "url": url_for("minutes.minutes_view", meeting_id=passage.meeting_id) if own else None}


@knowledge_bp.route("/meetings")
@login_required
def meetings():
    """What the chat panel needs when it first opens: the meetings the user may ask about."""
    if not current_app.config["KNOWLEDGE_ENABLED"]:
        return jsonify({"enabled": False, "meetings": [], "projects": []})
    # project_id is the meeting's current project, the same value the project filter matches on.
    return jsonify({"enabled": True, "meetings": [
        {"id": k.meeting_id, "title": k.title, "project_id": k.meeting.project_id,
         "date": localtime(k.meeting_start, "%Y-%m-%d") if k.meeting_start else None}
        for k in qa.searchable_meetings(current_user)],
        "projects": [{"id": pid, "name": name} for pid, name in qa.searchable_projects(current_user)]})


@knowledge_bp.route("/ask", methods=["POST"])
@login_required
def ask():
    if not current_app.config["KNOWLEDGE_ENABLED"]:
        return _errors([DISABLED_MESSAGE], 503)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _errors(["請求格式不正確"], 400)
    # Only the expected keys; blank optional fields mean "no filter".
    raw = {k: (v.replace("\r\n", "\n").strip() if isinstance(v, str) else v)
           for k in ("question", "date_from", "date_to", "meeting_id", "project_id") if (v := body.get(k)) is not None}
    try:
        data = AskSchema().load({k: v for k, v in raw.items() if v != ""})
        history = _clean_history(body.get("history"))
    except ValidationError as err:
        return _errors([f"{FIELD_LABELS.get(name, name)}：{msg}"
                        for name, messages in err.messages.items() for msg in messages], 400)

    try:
        answer = qa.answer_question(current_app._get_current_object(), current_user, data["question"],
                                    history=history, date_from=data["date_from"], date_to=data["date_to"],
                                    meeting_id=data["meeting_id"], project_id=data["project_id"])
    except (qa.QAError, EmbeddingError, LLMError) as exc:
        current_app.logger.warning("knowledge Q&A failed for user %s: %s", current_user.id, exc)
        record_audit_event(actor_user_id=current_user.id, action="knowledge.ask_failed", target_type="knowledge",
                           metadata={"error": exc.code})
        return _errors([ERROR_MESSAGES.get(exc.code, "查詢時發生錯誤，請查看伺服器紀錄")],
                       400 if isinstance(exc, qa.QAError) else 503)

    # Neither the question nor the conversation is stored (they may quote confidential content);
    # who asked, the filters and which meetings the answer drew on are.
    record_audit_event(
        actor_user_id=current_user.id, action="knowledge.asked", target_type="knowledge",
        metadata={"question_chars": len(data["question"]), "history_messages": len(history),
                  "passages": len(answer.passages),
                  "cited_meetings": sorted({p.meeting_id for p in answer.cited_passages}),
                  "filters": {"date_from": raw.get("date_from") or None, "date_to": raw.get("date_to") or None,
                              "meeting_id": data["meeting_id"], "project_id": data["project_id"]},
                  "model": answer.model},
    )
    # The page builds the answer from these parts with textContent, never as HTML.
    return jsonify({
        "text": answer.text,
        "segments": [{"type": kind, "value": value} for kind, value in answer.segments()],
        "sources": [_source(p) for p in answer.cited_passages],
        "passage_count": len(answer.passages),
        "model": answer.model,
    })
