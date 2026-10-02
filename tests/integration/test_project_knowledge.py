"""Project details in the knowledge base (REQ-68): stored as metadata when minutes are indexed,
kept current when the project changes, and usable as a filter in the chat."""
import hashlib
from datetime import date, datetime, timezone

import numpy as np
import pytest

from app.extensions import db as _db
from app.knowledge import indexer
from app.knowledge.embeddings import EmbeddingProvider
from app.minutes import generator
from app.minutes.providers import LLMProvider, LLMResult
from app.models.audit import AuditLog
from app.models.knowledge import MeetingKnowledge, MeetingKnowledgeChunk
from app.models.meeting import Meeting, Participant, TranscriptSegment
from app.models.project import Project, ProjectStakeholder
from app.models.template import Minutes
from app.models.user import User

MINUTES = """# Q3 預算會議 會議記錄

## 會議摘要
本次會議討論第三季行銷預算。

## 決議事項
行銷預算增加兩成，由李小姐負責提案。
"""

LAUNCH = """# 新產品上線會議 會議記錄

## 決議事項
新產品十一月正式上線，由陳工程師負責部署。
"""

SUMMARY_JSON = '{"summary": "討論第三季行銷預算。", "keywords": ["預算", "行銷"]}'


class HashEmbedder(EmbeddingProvider):
    """Deterministic bag-of-characters vectors: texts sharing characters are close."""

    name = "fake-embedder"
    dimensions = 384

    def __init__(self):
        self.calls = []

    def _vec(self, t):
        v = np.zeros(self.dimensions)
        for ch in t:
            v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % self.dimensions] += 1
        return (v / (np.linalg.norm(v) or 1)).tolist()

    def embed_passages(self, texts):
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, t):
        return self._vec(t)


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, text):
        self.calls = []
        self.text = text

    def generate(self, system, user_content):
        self.calls.append((system, user_content))
        return LLMResult(text=self.text, model="claude-opus-5", input_tokens=10, output_tokens=10)

    def count_tokens(self, system, user_content):
        return 100


@pytest.fixture(autouse=True)
def knowledge_app(app):
    app.config["KNOWLEDGE_ENABLED"] = True
    indexer.reset_jobs()
    generator.reset_jobs()
    app.extensions["embedding_provider"] = HashEmbedder()
    app.extensions["knowledge_llm_provider"] = FakeLLM(SUMMARY_JSON)
    app.extensions["knowledge_qa_provider"] = FakeLLM("行銷預算增加兩成[1]。")
    app.extensions["llm_provider"] = FakeLLM(MINUTES)
    yield app
    indexer.reset_jobs()
    generator.reset_jobs()


def _user(email="org@example.com", name="王經理"):
    user = User(email=email, display_name=name)
    _db.session.add(user)
    _db.session.commit()
    return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True


def _project(owner_id, name="官網改版"):
    project = Project(owner_id=owner_id, name=name, description="2026 年官網重新設計",
                      start_date=date(2026, 7, 1), end_date=date(2026, 12, 31))
    project.stakeholders = [ProjectStakeholder(name="陳總監", email="chen@example.com", role="發起人", position=0)]
    _db.session.add(project)
    _db.session.commit()
    return project.id


def _meeting(organizer_id, title="Q3 預算會議", *, minutes=None, project_id=None, attendees=("li@example.com",)):
    meeting = Meeting(organizer_id=organizer_id, title=title, status="transcribed", platform="google_meet",
                      project_id=project_id, scheduled_start=datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc))
    _db.session.add(meeting)
    _db.session.flush()
    for email in attendees:
        _db.session.add(Participant(meeting_id=meeting.id, email=email, display_name="李小姐"))
    _db.session.add(TranscriptSegment(meeting_id=meeting.id, start_ms=0, end_ms=4000, text="行銷預算增加兩成"))
    if minutes:
        _db.session.add(Minutes(meeting_id=meeting.id, content_markdown=minutes, llm_provider="fake",
                                llm_model="claude-opus-5", status="draft"))
    _db.session.commit()
    return meeting.id


def _knowledge(meeting_id):
    _db.session.expire_all()  # jobs write through their own session
    return MeetingKnowledge.query.filter_by(meeting_id=meeting_id).one()


def _chunk(meeting_id, index=0):
    return MeetingKnowledgeChunk.query.filter_by(meeting_id=meeting_id, chunk_index=index).one()


# --- TC-68: metadata written at index time -----------------------------------------------------

def test_generated_minutes_carry_project_details_into_the_knowledge_base(client, app):
    """TC-67、TC-68：產生會議記錄時 prompt 帶入與會人員姓名與專案；知識庫 metadata 與每個片段帶入專案
    （ID、名稱、說明、期程、利害關係人），向量化的文字以專案名稱開頭。"""
    user_id = _user()
    project_id = _project(user_id)
    meeting_id = _meeting(user_id, project_id=project_id)
    _login(client, user_id)

    assert client.post(f"/meetings/{meeting_id}/minutes/generate", data={}).status_code == 302

    minutes_prompt = app.extensions["llm_provider"].calls[0][1]
    assert "專案：官網改版" in minutes_prompt and "與會人員：李小姐" in minutes_prompt
    k = _knowledge(meeting_id)
    assert k.status == "indexed" and (k.project_id, k.project_name) == (project_id, "官網改版")
    meta = _chunk(meeting_id, 1).chunk_metadata
    assert {key: value for key, value in meta.items() if key.startswith("project_")} == {
        "project_id": project_id, "project_name": "官網改版", "project_description": "2026 年官網重新設計",
        "project_start": "2026-07-01", "project_end": "2026-12-31", "project_stakeholders": ["陳總監"]}
    embedded = app.extensions["embedding_provider"].calls[0]
    assert embedded[1].startswith("專案：官網改版｜會議：Q3 預算會議｜日期：2026-09-29｜章節：決議事項\n")


def test_meeting_without_a_project_has_empty_project_metadata(app):
    """TC-68：會議沒有專案時 metadata 的專案欄位為空，向量化文字不含專案。"""
    user_id = _user()
    meeting_id = _meeting(user_id, minutes=MINUTES)

    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"

    k = _knowledge(meeting_id)
    assert k.project_id is None and k.project_name is None
    meta = _chunk(meeting_id).chunk_metadata
    assert meta["project_id"] is None and meta["project_name"] is None and meta["project_stakeholders"] == []
    assert app.extensions["embedding_provider"].calls[0][0].startswith("會議：Q3 預算會議")


def test_project_changes_refresh_the_knowledge_without_a_new_summary(client, app):
    """TC-68：會議換專案、專案改名、刪除專案後自動重建該會議的知識庫 metadata；會議記錄內容沒變時沿用原摘要，
    不再把會議記錄送給 LLM（RISK-13）；內容有改或手動重建才重新產生摘要。"""
    user_id = _user()
    project_id = _project(user_id)
    meeting_id = _meeting(user_id, minutes=MINUTES)
    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"
    summary_llm = app.extensions["knowledge_llm_provider"]
    assert len(summary_llm.calls) == 1
    _login(client, user_id)

    client.post(f"/meetings/{meeting_id}/project", data={"project_id": project_id})
    k = _knowledge(meeting_id)
    assert (k.project_id, k.project_name) == (project_id, "官網改版")
    assert k.summary == "討論第三季行銷預算。" and k.keywords == ["預算", "行銷"]

    client.post(f"/projects/{project_id}/edit", data={"name": "官網改版二期", "status": "active"})
    assert _knowledge(meeting_id).project_name == "官網改版二期"
    meta = _chunk(meeting_id).chunk_metadata
    assert meta["project_name"] == "官網改版二期" and meta["project_stakeholders"] == []
    assert meta["keywords"] == ["預算", "行銷"]  # the summary metadata was kept

    client.post(f"/projects/{project_id}/delete")
    k = _knowledge(meeting_id)
    assert k.status == "indexed" and k.project_id is None and k.project_name is None
    assert _chunk(meeting_id).chunk_metadata["project_name"] is None
    assert len(summary_llm.calls) == 1  # metadata-only changes never re-sent the minutes

    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": MINUTES.replace("兩成", "三成")})
    assert len(summary_llm.calls) == 2  # the minutes text changed
    client.post(f"/meetings/{meeting_id}/knowledge/reindex")
    assert len(summary_llm.calls) == 3  # forced rebuild


def test_renaming_a_participant_refreshes_the_knowledge_metadata(client, app):
    """TC-66：修改與會者姓名後，知識庫 metadata 的與會者姓名同步更新。"""
    user_id = _user()
    meeting_id = _meeting(user_id, minutes=MINUTES)
    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"
    participant = Participant.query.filter_by(meeting_id=meeting_id).one()
    _login(client, user_id)

    client.post(f"/meetings/{meeting_id}/participants/{participant.id}/name", data={"display_name": "李曉華"})

    assert _knowledge(meeting_id).participant_names == ["王經理", "李曉華"]


# --- TC-68: asking within a project ------------------------------------------------------------

@pytest.fixture()
def chat(app):
    """me organises the budget meeting (project 官網改版) and attends boss's launch meeting
    (project 新產品上市); boss's HR meeting (project 組織調整) is not mine."""
    me, boss = _user("me@example.com", "我"), _user("boss@example.com", "老闆")
    ids = {"me": me, "boss": boss, "web": _project(me), "launch_project": _project(boss, "新產品上市"),
           "secret_project": _project(boss, "組織調整")}
    ids["budget"] = _meeting(me, minutes=MINUTES, project_id=ids["web"])
    ids["launch"] = _meeting(boss, "新產品上線會議", minutes=LAUNCH, project_id=ids["launch_project"],
                             attendees=("me@example.com",))
    ids["secret"] = _meeting(boss, "人事會議", minutes="# 人事\n\n機密名單。", project_id=ids["secret_project"],
                             attendees=("hr@example.com",))
    ids["plain"] = _meeting(me, "沒有專案的會議", minutes="# 雜項\n\n決議：訂便當。")
    for key in ("budget", "launch", "secret", "plain"):
        assert indexer.index_meeting(app, ids[key]) == "indexed"
    return ids


def _prompt(app) -> str:
    return app.extensions["knowledge_qa_provider"].calls[-1][1]


def test_project_filter_limits_passages_and_sources_name_the_project(client, app, chat):
    """TC-68：可只查某個專案的會議；片段與引用來源標示專案；篩選依會議目前所屬的專案，移出專案立即生效。"""
    _login(client, chat["me"])

    resp = client.post("/knowledge/ask", json={"question": "決議", "project_id": chat["web"]})

    assert resp.status_code == 200
    prompt = _prompt(app)
    assert "專案：官網改版｜會議：Q3 預算會議" in prompt
    assert "新產品上線會議" not in prompt and "沒有專案的會議" not in prompt
    assert resp.get_json()["sources"][0]["project"] == "官網改版"
    asked = AuditLog.query.filter_by(action="knowledge.asked").one()
    assert asked.event_metadata["filters"]["project_id"] == chat["web"]

    client.post("/knowledge/ask", json={"question": "決議"})  # no filter: every meeting I took part in
    prompt = _prompt(app)
    assert "專案：新產品上市｜會議：新產品上線會議" in prompt and "\n會議：沒有專案的會議｜" in prompt
    assert "人事會議" not in prompt

    _db.session.get(Meeting, chat["budget"]).project_id = None  # not re-indexed yet
    _db.session.commit()
    calls = len(app.extensions["knowledge_qa_provider"].calls)
    resp = client.post("/knowledge/ask", json={"question": "決議", "project_id": chat["web"]})
    assert resp.status_code == 200 and resp.get_json()["passage_count"] == 0  # the project is empty now
    assert len(app.extensions["knowledge_qa_provider"].calls) == calls


def test_chat_lists_own_projects_and_projects_of_attended_meetings(client, app, chat):
    """TC-68：聊天面板「查詢範圍」固定顯示專案下拉；清單含本人的專案（包含還沒有會議記錄的）與以與會者身分參與的會議
    所屬的專案；選到還沒有會議記錄的專案時回答找不到且不呼叫 LLM；指定其他專案會被拒絕。"""
    empty = _project(chat["me"], "尚無會議的專案")
    _login(client, chat["me"])

    data = client.get("/knowledge/meetings").get_json()

    assert data["projects"] == [{"id": chat["web"], "name": "官網改版"}, {"id": empty, "name": "尚無會議的專案"},
                                {"id": chat["launch_project"], "name": "新產品上市"}]

    resp = client.post("/knowledge/ask", json={"question": "決議", "project_id": empty})
    assert resp.status_code == 200 and "找不到與問題相關的內容" in resp.get_json()["text"]
    assert app.extensions["knowledge_qa_provider"].calls == []
    assert {m["id"]: m["project_id"] for m in data["meetings"]} == {
        chat["budget"]: chat["web"], chat["launch"]: chat["launch_project"], chat["plain"]: None}

    resp = client.post("/knowledge/ask", json={"question": "決議", "project_id": chat["secret_project"]})
    assert resp.status_code == 400 and "找不到指定的專案" in resp.get_json()["errors"][0]
    assert app.extensions["knowledge_qa_provider"].calls == []
    assert AuditLog.query.filter_by(action="knowledge.ask_failed").one().event_metadata == {"error": "project_not_found"}

    page = client.get("/meetings/").get_data(as_text=True)
    filters = page.split('<details class="kb-filters">')[1].split("</details>")[0]
    assert 'id="kb-project"' in filters and "全部專案" in filters and "hidden" not in filters
    assert filters.index('id="kb-project"') < filters.index('id="kb-meeting"')
