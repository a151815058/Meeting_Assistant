from datetime import timezone

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from marshmallow import ValidationError

from app.extensions import db
from app.knowledge import indexer
from app.meetings import calendar_sync, meetings_bp
from app.meetings.calendar_sync import sync_meeting_participants
from app.meetings.schemas import MeetingSchema, ParticipantSchema
from app.models.meeting import Meeting, Participant
from app.security.audit import record_audit_event
from app.transcription import upload as audio_upload


def _refresh_knowledge(meeting: Meeting) -> None:
    """The attendee list is knowledge-base metadata (REQ-59, REQ-62): re-index after it changes."""
    if meeting.minutes is not None:
        indexer.schedule_index(current_app._get_current_object(), meeting.id, current_user.id)


@meetings_bp.route("/")
@login_required
def dashboard():
    meetings = (
        Meeting.query.filter_by(organizer_id=current_user.id)
        .order_by(Meeting.scheduled_start.desc().nullslast())
        .all()
    )
    return render_template("meetings/dashboard.html", meetings=meetings)


def _render_create(form=None, status=200):
    app = current_app._get_current_object()
    has_calendar = bool(calendar_sync.calendar_accounts(current_user))
    events, errors = calendar_sync.list_upcoming_events(app, current_user) if has_calendar else ([], [])
    created = {m.platform_event_id: m.id for m in
               Meeting.query.filter(Meeting.organizer_id == current_user.id, Meeting.platform_event_id.isnot(None))}
    return render_template("meetings/create.html", events=events, calendar_errors=errors,
                           has_calendar=has_calendar, created=created, form=form or {},
                           provider_labels=calendar_sync.PROVIDER_LABELS), status


@meetings_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "GET":
        return _render_create()

    form = request.form
    event = None
    choice = form.get("calendar_event", "")
    if choice:
        # Picked from the dropdown: "<platform>|<event id>". Title and times are re-read from the
        # calendar rather than trusted from the form.
        platform, _, event_id = choice.partition("|")
        raw = {"title": "x", "platform": platform, "platform_event_id": event_id}
        try:
            MeetingSchema().load(raw)
            if platform == "manual":
                raise ValidationError("manual")
            event = calendar_sync.fetch_event(current_app._get_current_object(), current_user, platform, event_id)
        except ValidationError:
            flash("選擇的行事曆會議無效", "error")
            return _render_create(form, 400)
        except Exception:
            current_app.logger.exception("fetching calendar event %s failed", event_id)
            flash("無法讀取這場行事曆會議，請重新整理後再試", "error")
            return _render_create(form, 400)
        existing = Meeting.query.filter_by(organizer_id=current_user.id, platform=platform,
                                           platform_event_id=event_id).first()
        if existing is not None:
            flash("這場行事曆會議已經建立過，已為您開啟", "success")
            return redirect(url_for("meetings.detail", meeting_id=existing.id))
        raw = {"title": " ".join(form.get("title", "").split()) or event.title,
               "platform": platform, "platform_event_id": event_id}
    else:
        raw = {"title": " ".join(form.get("title", "").split()),
               "platform": form.get("platform", "manual"),
               "platform_event_id": form.get("platform_event_id", "").strip() or None}

    try:
        data = MeetingSchema().load(raw)
    except ValidationError as err:
        labels = {"title": "標題", "platform": "平台", "platform_event_id": "事件 ID"}
        for field_name in err.messages:
            flash(f"{labels.get(field_name, field_name)}格式不正確", "error")
        return _render_create(form, 400)

    meeting = Meeting(organizer_id=current_user.id, **data)
    if event is not None:
        meeting.scheduled_start = event.start.astimezone(timezone.utc)
        meeting.scheduled_end = event.end.astimezone(timezone.utc) if event.end else None
    db.session.add(meeting)
    db.session.commit()

    if event is not None:  # convenience: pull the invitee list right away
        try:
            participants = sync_meeting_participants(meeting)
            flash(f"已建立會議，並從行事曆同步 {len(participants)} 位與會者", "success")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("initial participant sync failed for meeting %s", meeting.id)
            flash("已建立會議，但同步與會者失敗，可稍後在會議頁重新同步", "error")
    return redirect(url_for("meetings.detail", meeting_id=meeting.id))


@meetings_bp.route("/<meeting_id>")
@login_required
def detail(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    return render_template("meetings/detail.html", meeting=meeting)


UPLOAD_ERRORS = {
    "no_file": "請選擇要上傳的錄音檔",
    "unsupported_type": "不支援的檔案格式，請上傳 MP3、WAV、M4A、AAC、FLAC、OGG、OPUS、WEBM、WMA 或 MP4",
    "empty_file": "檔案是空的",
    "file_too_large": "檔案超過大小上限",
    "invalid_audio": "無法讀取音訊，檔案可能已損毀或不是錄音檔",
    "too_long": "錄音長度超過上限",
    "busy": "此會議正在錄音或轉錄其他錄音檔，請稍後再試",
    "minutes_in_progress": "會議記錄產生中，請稍後再上傳",
}


@meetings_bp.route("/<meeting_id>/audio-upload", methods=["POST"])
@login_required
def upload_audio(meeting_id):
    """REQ-47: transcribe an uploaded recording into this meeting's transcript; the file is not kept."""
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    wants_json = request.accept_mimetypes.best == "application/json"
    audio = request.files.get("audio")
    try:
        audio_upload.start_upload(current_app._get_current_object(), meeting, current_user.id, audio)
    except audio_upload.UploadRejected as exc:
        if wants_json:
            return jsonify(ok=False, error=exc.code, message=UPLOAD_ERRORS.get(exc.code, exc.code)), 400
        flash(UPLOAD_ERRORS.get(exc.code, exc.code), "error")
    else:
        if wants_json:
            return jsonify(ok=True), 202
        flash("錄音檔已上傳，正在轉錄", "success")
    finally:
        if audio is not None:
            audio.close()  # drop the server's spooled copy of the upload right away
    return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="live"))


@meetings_bp.route("/<meeting_id>/sync-participants", methods=["POST"])
@login_required
def sync_participants(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    try:
        participants = sync_meeting_participants(meeting)
        flash(f"已同步 {len(participants)} 位與會者", "success")
        _refresh_knowledge(meeting)
    except (ValueError, RuntimeError) as exc:
        flash(f"同步失敗：{exc}", "error")
    return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="participants"))


@meetings_bp.route("/<meeting_id>/participants", methods=["POST"])
@login_required
def add_participant(meeting_id):
    """Organizer adds a participant by hand (meetings not linked to a calendar event).
    These rows are the only source of mail recipients (REQ-16), so every change is audited."""
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    try:
        data = ParticipantSchema().load({
            "email": request.form.get("email", "").strip().lower(),
            "display_name": " ".join(request.form.get("display_name", "").split()),
        })
    except ValidationError:
        flash("請輸入有效的 Email（名稱最多 255 字）", "error")
        return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="participants"))

    if any(p.email.lower() == data["email"] for p in meeting.participants):
        flash(f"{data['email']} 已在與會者名單中", "error")
    elif len(meeting.participants) >= current_app.config["MAIL_MAX_RECIPIENTS"]:
        flash("與會者人數已達上限", "error")
    else:
        db.session.add(Participant(meeting_id=meeting.id, email=data["email"],
                                   display_name=data["display_name"] or data["email"],
                                   response_status="accepted"))
        db.session.commit()
        record_audit_event(actor_user_id=current_user.id, action="participant.added", target_type="meeting",
                           target_id=meeting.id, metadata={"email": data["email"]})
        flash(f"已新增與會者 {data['email']}", "success")
        _refresh_knowledge(meeting)
    return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="participants"))


@meetings_bp.route("/<meeting_id>/participants/<participant_id>/delete", methods=["POST"])
@login_required
def remove_participant(meeting_id, participant_id):
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != current_user.id:
        abort(404)
    participant = db.session.get(Participant, participant_id)
    if participant is None or participant.meeting_id != meeting.id:
        abort(404)
    email = participant.email
    db.session.delete(participant)
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="participant.removed", target_type="meeting",
                       target_id=meeting.id, metadata={"email": email})
    flash(f"已移除與會者 {email}", "success")
    _refresh_knowledge(meeting)
    return redirect(url_for("meetings.detail", meeting_id=meeting.id, _anchor="participants"))
