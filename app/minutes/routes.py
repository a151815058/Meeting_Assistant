import io

from flask import abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from marshmallow import ValidationError

from app.extensions import db
from app.knowledge import indexer
from app.minutes import export, generator, minutes_bp, prompting
from app.minutes.schemas import MinutesEditSchema, TemplateSchema, load_form
from app.models.audit import AuditLog
from app.models.meeting import Meeting
from app.models.template import MinutesTemplate
from app.notifications import mailer
from app.notifications.senders import MailError
from app.security.audit import record_audit_event

ERROR_MESSAGES = {
    "no_transcript": "這場會議還沒有逐字稿，請先錄音",
    "recording_in_progress": "錄音進行中，請先停止錄音再產生會議記錄",
    "transcription_in_progress": "錄音檔轉錄中，請等轉錄完成再產生會議記錄",
    "already_generating": "會議記錄產生中，請稍候",
    "template_not_found": "找不到指定的範本",
    "template_render_failed": "範本無法套用，請檢查範本內容",
    "not_configured": "尚未設定 LLM 金鑰（ANTHROPIC_API_KEY），無法產生會議記錄",
    "auth_failed": "LLM 金鑰無效或權限不足",
    "rate_limited": "LLM 服務請求過於頻繁，請稍後再試",
    "provider_unavailable": "LLM 服務暫時無法使用，請稍後再試",
    "connection_failed": "無法連線到 LLM 服務，請檢查網路",
    "bad_request": "LLM 服務拒絕了這個請求，請查看伺服器紀錄",
    "refused": "AI 基於安全政策拒絕處理此逐字稿",
    "truncated": "會議記錄內容過長被截斷，請調整範本或提高 LLM_MAX_OUTPUT_TOKENS",
    "internal_error": "產生會議記錄時發生錯誤，請查看伺服器紀錄",
    "sending": "會議記錄寄送中，請稍候",
}

MAIL_ERROR_MESSAGES = {
    "generating": "會議記錄產生中，完成後才能寄出",
    "already_sending": "會議記錄寄送中，請稍候",
    "no_recipients": "這場會議沒有其他與會者可寄送，請先到會議頁新增或同步與會者",
    "too_many_recipients": "收件人超過上限（MAIL_MAX_RECIPIENTS），請移除部分與會者",
    "cooldown": "剛剛才寄出過，請稍後再重新寄送",
    "not_configured": "您的帳號尚未授權寄信（需以 Google 或 Microsoft 登入並同意寄信權限）",
    "auth_failed": "寄信授權失效或權限不足，請重新登入 Google／Microsoft 帳號後再試",
    "rate_limited": "郵件服務請求過於頻繁，請稍後重試",
    "provider_unavailable": "郵件服務暫時無法使用，請稍後重試",
    "connection_failed": "無法連線到郵件服務；信件可能未寄出，請確認寄件備份後再決定是否重試",
    "bad_request": "郵件服務拒絕了這封信，請查看伺服器紀錄",
    "internal_error": "寄送會議記錄時發生錯誤，請查看伺服器紀錄",
    "attachments_too_large": "附件太大（超過 MAIL_MAX_ATTACHMENT_BYTES），請取消附件或改為下載後另行分享",
    "pdf_font_missing": "伺服器找不到中文字型，無法產生 PDF 附件（請設定 PDF_FONT_PATH）",
    "export_failed": "產生附件時發生錯誤，請查看伺服器紀錄",
}

EXPORT_ERROR_MESSAGES = {
    "pdf_font_missing": "伺服器找不到中文字型，無法產生 PDF（請設定 PDF_FONT_PATH），可先改用 Word 格式",
}

KNOWLEDGE_ERROR_MESSAGES = {
    "empty_minutes": "會議記錄沒有內容，無法建立知識庫索引",
    "model_unavailable": "無法載入向量模型（首次使用需連線 Hugging Face 下載），請查看伺服器紀錄",
    "not_configured": "向量模型設定錯誤（EMBEDDING_PROVIDER），請查看伺服器紀錄",
    "dimension_mismatch": "向量模型維度與資料庫不符，請確認 EMBEDDING_* 設定",
    "internal_error": "建立知識庫索引時發生錯誤，請查看伺服器紀錄",
}

BACKEND_LABELS = {
    "gmail": "您的 Google 帳號（Gmail）",
    "graph": "您的 Microsoft 帳號（Outlook）",
    "outbox": "開發模式本機信箱（不會真的寄出，信件存成 .eml 檔）",
}


def _own_meeting(meeting_id) -> Meeting:
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    return meeting


def _own_template(template_id) -> MinutesTemplate:
    template = db.session.get(MinutesTemplate, template_id)
    if template is None or template.owner_id != current_user.id:
        abort(404)
    return template


def _user_templates():
    return (MinutesTemplate.query.filter_by(owner_id=current_user.id)
            .order_by(MinutesTemplate.is_default.desc(), MinutesTemplate.name).all())


def _flash_errors(err: ValidationError) -> None:
    for field_name, messages in err.messages.items():
        for message in messages if isinstance(messages, list) else [messages]:
            flash(f"{field_name}：{message}", "error")


# --- templates (REQ-11) ---------------------------------------------------------------

@minutes_bp.route("/templates/")
@login_required
def template_list():
    return render_template("minutes/template_list.html", templates=_user_templates(),
                           builtin_name=prompting.BUILTIN_TEMPLATE_NAME)


@minutes_bp.route("/templates/new", methods=["GET", "POST"])
@login_required
def template_new():
    if request.method == "POST":
        try:
            data = load_form(TemplateSchema(), request.form, ("name", "description", "body"))
        except ValidationError as err:
            _flash_errors(err)
            return render_template("minutes/template_form.html", template=None, form=request.form,
                                   variables=prompting.TEMPLATE_VARIABLES), 400
        template = MinutesTemplate(owner_id=current_user.id, **data)
        db.session.add(template)
        db.session.commit()
        record_audit_event(actor_user_id=current_user.id, action="template.created",
                           target_type="minutes_template", target_id=template.id, metadata={"name": template.name})
        flash("範本已建立", "success")
        return redirect(url_for("minutes.template_list"))
    form = {"name": "", "description": "", "body": prompting.BUILTIN_TEMPLATE_BODY}
    return render_template("minutes/template_form.html", template=None, form=form,
                           variables=prompting.TEMPLATE_VARIABLES)


@minutes_bp.route("/templates/<template_id>/edit", methods=["GET", "POST"])
@login_required
def template_edit(template_id):
    template = _own_template(template_id)
    if request.method == "POST":
        try:
            data = load_form(TemplateSchema(), request.form, ("name", "description", "body"))
        except ValidationError as err:
            _flash_errors(err)
            return render_template("minutes/template_form.html", template=template, form=request.form,
                                   variables=prompting.TEMPLATE_VARIABLES), 400
        for key, value in data.items():
            setattr(template, key, value)
        db.session.commit()
        record_audit_event(actor_user_id=current_user.id, action="template.updated",
                           target_type="minutes_template", target_id=template.id, metadata={"name": template.name})
        flash("範本已更新", "success")
        return redirect(url_for("minutes.template_list"))
    form = {"name": template.name, "description": template.description or "", "body": template.body}
    return render_template("minutes/template_form.html", template=template, form=form,
                           variables=prompting.TEMPLATE_VARIABLES)


@minutes_bp.route("/templates/<template_id>/delete", methods=["POST"])
@login_required
def template_delete(template_id):
    template = _own_template(template_id)
    # Minutes keep their text; they just lose the link to the deleted template.
    for minutes in template.minutes:
        minutes.template_id = None
    db.session.delete(template)
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="template.deleted",
                       target_type="minutes_template", target_id=template_id, metadata={"name": template.name})
    flash("範本已刪除", "success")
    return redirect(url_for("minutes.template_list"))


@minutes_bp.route("/templates/<template_id>/default", methods=["POST"])
@login_required
def template_set_default(template_id):
    template = _own_template(template_id)
    MinutesTemplate.query.filter_by(owner_id=current_user.id, is_default=True).update({"is_default": False})
    template.is_default = True
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="template.set_default",
                       target_type="minutes_template", target_id=template.id)
    flash(f"已將「{template.name}」設為預設範本", "success")
    return redirect(url_for("minutes.template_list"))


# --- minutes (REQ-12, REQ-14) -----------------------------------------------------------

@minutes_bp.route("/meetings/<meeting_id>/minutes/generate", methods=["POST"])
@login_required
def minutes_generate(meeting_id):
    meeting = _own_meeting(meeting_id)
    if mailer.is_sending(meeting.id):
        flash(ERROR_MESSAGES["sending"], "error")
        return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
    try:
        template = generator.resolve_template(request.form.get("template_id") or None, current_user.id)
        generator.start_generation(current_app._get_current_object(), meeting, template, current_user.id)
    except generator.GenerationRejected as exc:
        flash(ERROR_MESSAGES.get(exc.code, exc.code), "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="minutes"))
    return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))


@minutes_bp.route("/meetings/<meeting_id>/minutes", methods=["GET", "POST"])
@login_required
def minutes_view(meeting_id):
    meeting = _own_meeting(meeting_id)
    if request.method == "POST":
        if meeting.minutes is None:
            abort(404)
        job = generator.get_job(meeting.id)
        if job is not None and job.state == "running":
            flash("會議記錄產生中，暫時無法儲存", "error")
            return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
        if mailer.is_sending(meeting.id):
            flash("會議記錄寄送中，暫時無法儲存", "error")
            return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
        try:
            data = load_form(MinutesEditSchema(), request.form, ("content_markdown",))
        except ValidationError as err:
            _flash_errors(err)
            return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
        meeting.minutes.content_markdown = data["content_markdown"]
        db.session.commit()
        record_audit_event(actor_user_id=current_user.id, action="minutes.edited", target_type="meeting",
                           target_id=meeting.id, metadata={"chars": len(data["content_markdown"])})
        flash("會議記錄已儲存", "success")
        # The saved version is the one sent and searched: refresh the knowledge base (REQ-58).
        indexer.schedule_index(current_app._get_current_object(), meeting.id, current_user.id)
        return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))

    job = generator.get_job(meeting.id)
    recipients, skipped = mailer.recipients_for(meeting)
    last_send = (AuditLog.query.filter_by(action="mail.sent", target_type="meeting", target_id=meeting.id)
                 .order_by(AuditLog.created_at.desc()).first())
    backend = mailer.describe_backend(current_app, meeting)
    return render_template("minutes/minutes.html", meeting=meeting, minutes=meeting.minutes, job=job,
                           templates=_user_templates(), builtin_name=prompting.BUILTIN_TEMPLATE_NAME,
                           error_message=ERROR_MESSAGES.get(job.error, job.error) if job and job.error else None,
                           recipients=recipients, skipped=skipped, last_send=last_send,
                           backend=backend, backend_label=BACKEND_LABELS.get(backend),
                           max_recipients=current_app.config["MAIL_MAX_RECIPIENTS"],
                           knowledge_enabled=current_app.config["KNOWLEDGE_ENABLED"],
                           knowledge=meeting.knowledge, knowledge_running=indexer.is_indexing(meeting.id),
                           knowledge_error=KNOWLEDGE_ERROR_MESSAGES.get(
                               meeting.knowledge.error, meeting.knowledge.error) if meeting.knowledge else None)


@minutes_bp.route("/meetings/<meeting_id>/knowledge/reindex", methods=["POST"])
@login_required
def knowledge_reindex(meeting_id):
    """Rebuild the meeting's knowledge-base entry (REQ-60), e.g. after a failure."""
    meeting = _own_meeting(meeting_id)
    if meeting.minutes is None:
        abort(404)
    if not current_app.config["KNOWLEDGE_ENABLED"]:
        flash("知識庫功能未啟用（KNOWLEDGE_ENABLED）", "error")
    else:
        indexer.schedule_index(current_app._get_current_object(), meeting.id, current_user.id, force=True)
        flash("已開始重建知識庫索引", "success")
    return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))


# --- sending (REQ-15 ~ REQ-17) ------------------------------------------------------------

@minutes_bp.route("/meetings/<meeting_id>/minutes/send", methods=["POST"])
@login_required
def minutes_send(meeting_id):
    """Send the saved minutes to the meeting's participants. Deliberately takes no recipient
    input: the list always comes from the Participant table (REQ-16, threat_model.md)."""
    meeting = _own_meeting(meeting_id)
    if meeting.minutes is None:
        abort(404)
    job = generator.get_job(meeting.id)
    attach = [f for f in export.FORMATS if f in request.form.getlist("attach")]  # whitelist, fixed order
    try:
        result, count = mailer.send_minutes(current_app._get_current_object(), meeting, current_user.id,
                                            generation_running=job is not None and job.state == "running",
                                            attach=attach)
    except (mailer.SendRejected, MailError) as exc:
        flash(MAIL_ERROR_MESSAGES.get(exc.code, exc.code), "error")
        return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
    if result.backend == "outbox":
        flash(f"開發模式：信件未真的寄出，已寫入本機信箱（{count} 位收件人）：{result.outbox_path}", "success")
    else:
        flash(f"會議記錄已寄給 {count} 位與會者", "success")
    return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))


# --- export (REQ-41) ----------------------------------------------------------------------

@minutes_bp.route("/meetings/<meeting_id>/minutes/export/<fmt>")
@login_required
def minutes_export(meeting_id, fmt):
    """Download the saved minutes as Word or PDF."""
    meeting = _own_meeting(meeting_id)
    if meeting.minutes is None or fmt not in export.FORMATS:
        abort(404)
    try:
        data = export.render(meeting, fmt, current_app.config)
    except export.ExportError as exc:
        current_app.logger.error("minutes export failed for meeting %s: %s", meeting.id, exc.code)
        flash(EXPORT_ERROR_MESSAGES.get(exc.code, "匯出失敗，請查看伺服器紀錄"), "error")
        return redirect(url_for("minutes.minutes_view", meeting_id=meeting.id))
    record_audit_event(actor_user_id=current_user.id, action="minutes.exported", target_type="meeting",
                       target_id=meeting.id, metadata={"format": fmt, "bytes": len(data)})
    response = send_file(io.BytesIO(data), mimetype=export.FORMATS[fmt], as_attachment=True,
                         download_name=export.filename(meeting, fmt))
    response.headers["Cache-Control"] = "no-store"  # confidential content: keep it out of shared caches
    return response


@minutes_bp.route("/meetings/<meeting_id>/minutes/status")
@login_required
def minutes_status(meeting_id):
    meeting = _own_meeting(meeting_id)
    job = generator.get_job(meeting.id)
    if job is None:
        return {"state": "idle", "has_minutes": meeting.minutes is not None}
    return {
        "state": job.state,
        "progress": job.progress,
        "error": ERROR_MESSAGES.get(job.error, job.error) if job.error else None,
        "has_minutes": meeting.minutes is not None,
    }
