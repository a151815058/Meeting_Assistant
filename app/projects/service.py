"""Project lookups shared with the meeting pages (REQ-65)."""
from app.extensions import db
from app.models.project import Project


def selectable_projects(user_id: str, current_id: str | None = None) -> list[Project]:
    """The user's projects for a dropdown: the active ones, plus the one already selected."""
    projects = Project.query.filter_by(owner_id=user_id).order_by(Project.name).all()
    return [p for p in projects if p.status == "active" or p.id == current_id]


def own_project(user_id: str, project_id: str | None) -> Project | None:
    """The project if it exists and belongs to the user, else None."""
    if not project_id or len(project_id) > 36:
        return None
    project = db.session.get(Project, project_id)
    return project if project is not None and project.owner_id == user_id else None
