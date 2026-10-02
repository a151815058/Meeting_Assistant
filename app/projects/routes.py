from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from marshmallow import ValidationError

from app.extensions import db
from app.knowledge import indexer
from app.models.meeting import Meeting
from app.models.project import Project, ProjectStakeholder
from app.projects import projects_bp
from app.projects.schemas import MAX_STAKEHOLDERS, ProjectSchema, error_messages, form_to_raw
from app.projects.service import own_project
from app.security.audit import record_audit_event

STATUS_LABELS = {"active": "進行中", "closed": "已結束"}


def _own_project(project_id) -> Project:
    project = own_project(current_user.id, project_id)
    if project is None:
        abort(404)
    return project


def _refresh_knowledge(meeting_ids) -> None:
    """Project details are knowledge-base metadata (REQ-68): re-index the meetings filed under it.
    Unchanged meetings are skipped by the indexer, and the AI summary is not regenerated."""
    app = current_app._get_current_object()
    meetings = Meeting.query.filter(Meeting.id.in_(list(meeting_ids))).all() if meeting_ids else []
    for meeting in meetings:
        if meeting.minutes is not None:
            indexer.schedule_index(app, meeting.id, current_user.id)


def _apply(project: Project, data: dict) -> None:
    project.name = data["name"]
    project.description = data["description"] or None
    project.start_date = data["start_date"]
    project.end_date = data["end_date"]
    project.status = data["status"]
    project.stakeholders = [
        ProjectStakeholder(name=s["name"], email=s["email"], role=s["role"] or None, position=position)
        for position, s in enumerate(data["stakeholders"])
    ]


def _form_values(project: Project | None) -> dict:
    if project is None:
        return {"name": "", "description": "", "start_date": "", "end_date": "", "status": "active",
                "stakeholders": []}
    return {
        "name": project.name, "description": project.description or "",
        "start_date": project.start_date.isoformat() if project.start_date else "",
        "end_date": project.end_date.isoformat() if project.end_date else "",
        "status": project.status,
        "stakeholders": [{"name": s.name, "email": s.email or "", "role": s.role or ""}
                         for s in project.stakeholders],
    }


def _render_form(project: Project | None, values: dict, status: int = 200):
    meetings = []
    if project is not None:
        meetings = (Meeting.query.filter_by(project_id=project.id, organizer_id=current_user.id)
                    .order_by(Meeting.scheduled_start.desc().nullslast(), Meeting.created_at.desc()).all())
    return render_template("projects/form.html", project=project, form=values, meetings=meetings,
                           status_labels=STATUS_LABELS, max_stakeholders=MAX_STAKEHOLDERS), status


def _submitted_values(raw: dict) -> dict:
    """What the user typed, for re-displaying the form after a validation error."""
    return {**raw, "start_date": raw["start_date"] or "", "end_date": raw["end_date"] or "",
            "stakeholders": [{**s, "email": s["email"] or ""} for s in raw["stakeholders"]]}


@projects_bp.route("/")
@login_required
def project_list():
    projects = (Project.query.filter_by(owner_id=current_user.id)
                .order_by(Project.status, Project.start_date.desc().nullslast(), Project.name).all())
    return render_template("projects/list.html", projects=projects, status_labels=STATUS_LABELS)


@projects_bp.route("/new", methods=["GET", "POST"])
@login_required
def project_new():
    if request.method == "GET":
        return _render_form(None, _form_values(None))
    raw = form_to_raw(request.form)
    try:
        data = ProjectSchema().load(raw)
    except ValidationError as err:
        for message in error_messages(err):
            flash(message, "error")
        return _render_form(None, _submitted_values(raw), 400)
    project = Project(owner_id=current_user.id)
    _apply(project, data)
    db.session.add(project)
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="project.created", target_type="project",
                       target_id=project.id, metadata={"name": project.name,
                                                       "stakeholders": len(project.stakeholders)})
    flash("專案已建立", "success")
    return redirect(url_for("projects.project_list"))


@projects_bp.route("/<project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    project = _own_project(project_id)
    if request.method == "GET":
        return _render_form(project, _form_values(project))
    raw = form_to_raw(request.form)
    try:
        data = ProjectSchema().load(raw)
    except ValidationError as err:
        for message in error_messages(err):
            flash(message, "error")
        return _render_form(project, _submitted_values(raw), 400)
    _apply(project, data)
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="project.updated", target_type="project",
                       target_id=project.id, metadata={"name": project.name,
                                                       "stakeholders": len(project.stakeholders)})
    flash("專案已更新", "success")
    _refresh_knowledge([m.id for m in project.meetings])
    return redirect(url_for("projects.project_list"))


@projects_bp.route("/<project_id>/delete", methods=["POST"])
@login_required
def project_delete(project_id):
    """Deletes the project only: its meetings, minutes and transcripts stay, without a project."""
    project = _own_project(project_id)
    name = project.name
    meeting_ids = [m.id for m in project.meetings]
    for meeting in project.meetings:
        meeting.project_id = None
    db.session.delete(project)
    db.session.commit()
    record_audit_event(actor_user_id=current_user.id, action="project.deleted", target_type="project",
                       target_id=project_id, metadata={"name": name, "meetings": len(meeting_ids)})
    flash(f"已刪除專案「{name}」", "success")
    _refresh_knowledge(meeting_ids)
    return redirect(url_for("projects.project_list"))
