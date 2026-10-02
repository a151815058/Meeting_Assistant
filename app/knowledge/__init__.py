"""Knowledge base over past meetings. Phase 1 (REQ-58 ~ REQ-60): saved minutes are chunked,
embedded and stored in pgvector with meeting metadata and an AI summary. Phase 2 (REQ-62, REQ-63):
a chat over the meetings a user took part in (``routes``, ``qa``; the chat box is in base.html)."""
import click
from flask import Blueprint
from flask.cli import AppGroup

knowledge_bp = Blueprint("knowledge", __name__, url_prefix="/knowledge")

knowledge_cli = AppGroup("knowledge", help="知識庫（會議記錄向量索引）管理")


@knowledge_cli.command("reindex")
@click.option("--meeting", "meeting_ids", multiple=True, help="只重建指定會議（可重複指定）")
@click.option("--all", "all_meetings", is_flag=True, help="所有已有會議記錄的會議")
@click.option("--failed", "failed_only", is_flag=True, help="只重試上次失敗或尚未建立索引的會議")
@click.option("--force", is_flag=True, help="內容未變也重新建立")
def reindex_command(meeting_ids, all_meetings, failed_only, force):
    """建立或重建會議記錄的知識庫索引（回填既有資料、重試失敗）。"""
    from flask import current_app

    from app.knowledge import indexer
    from app.models.knowledge import MeetingKnowledge
    from app.models.meeting import Meeting
    from app.models.template import Minutes

    if not (meeting_ids or all_meetings or failed_only):
        raise click.UsageError("請指定 --meeting ID、--all 或 --failed")
    if meeting_ids:
        targets = list(meeting_ids)
    else:
        query = Meeting.query.join(Minutes, Minutes.meeting_id == Meeting.id)
        if failed_only:
            query = query.outerjoin(MeetingKnowledge, MeetingKnowledge.meeting_id == Meeting.id).filter(
                (MeetingKnowledge.id.is_(None)) | (MeetingKnowledge.status != "indexed"))
        targets = [m.id for m in query.order_by(Meeting.created_at).all()]

    counts: dict[str, int] = {}
    for meeting_id in targets:
        result = indexer.index_meeting(current_app, meeting_id, None, force=force)
        counts[result] = counts.get(result, 0) + 1
        click.echo(f"{meeting_id}: {result}")
    click.echo("完成：" + ("、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "沒有符合的會議"))


def register(app) -> None:
    # Imported here, not at module level: app.minutes.routes imports this package's indexer.
    from app.knowledge import routes  # noqa: F401

    app.cli.add_command(knowledge_cli)
    app.register_blueprint(knowledge_bp)
    app.jinja_env.globals["knowledge_max_question_chars"] = routes.MAX_QUESTION_CHARS  # chat box in base.html
