import hashlib
import json
from datetime import datetime, timezone

import numpy as np
import pytest
from sqlalchemy import text

from app.extensions import db as _db
from app.knowledge import indexer
from app.knowledge.embeddings import EmbeddingError, EmbeddingProvider
from app.minutes import generator
from app.minutes.providers import LLMError, LLMProvider, LLMResult
from app.models.audit import AuditLog
from app.models.knowledge import MeetingKnowledge, MeetingKnowledgeChunk
from app.models.meeting import Meeting, Participant, TranscriptSegment
from app.models.template import Minutes
from app.models.user import User
from app.security.db_hardening import lock_down_public_schema

MINUTES = """# Q3 預算會議 會議記錄

## 會議摘要
本次會議討論第三季行銷預算與新產品上市時程。

## 決議事項
行銷預算增加兩成，由李小姐負責提案。

## 待辦事項
李小姐：下週五前提交行銷提案。
"""

SUMMARY_JSON = json.dumps({
    "summary": "討論第三季行銷預算。",
    "key_points": ["行銷預算", "上市時程"],
    "decisions": ["行銷預算增加兩成"],
    "action_items": [{"task": "提交行銷提案", "owner": "李小姐", "due": "下週五"}],
    "keywords": ["預算", "行銷"],
}, ensure_ascii=False)


class HashEmbedder(EmbeddingProvider):
    """Deterministic bag-of-characters vectors: texts sharing characters are close."""

    name = "fake-embedder"
    dimensions = 384

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def _vec(self, t):
        v = np.zeros(self.dimensions)
        for ch in t:
            v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % self.dimensions] += 1
        return (v / (np.linalg.norm(v) or 1)).tolist()

    def embed_passages(self, texts):
        if self.fail_with:
            raise EmbeddingError(self.fail_with)
        self.calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, t):
        return self._vec(t)


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, text=MINUTES, fail_with=None):
        self.calls = []
        self.text = text
        self.fail_with = fail_with

    def generate(self, system, user_content):
        self.calls.append((system, user_content))
        if self.fail_with:
            raise LLMError(self.fail_with)
        return LLMResult(text=self.text, model="claude-opus-5", input_tokens=10, output_tokens=10)

    def count_tokens(self, system, user_content):
        return 100


@pytest.fixture(autouse=True)
def knowledge_app(app):
    app.config["KNOWLEDGE_ENABLED"] = True
    indexer.reset_jobs()
    generator.reset_jobs()
    app.extensions["embedding_provider"] = HashEmbedder()
    app.extensions["knowledge_llm_provider"] = FakeLLM(text=SUMMARY_JSON)
    app.extensions["llm_provider"] = FakeLLM(text=MINUTES)
    yield app
    indexer.reset_jobs()
    generator.reset_jobs()


def _login(client, app, email="org@example.com", name="王經理"):
    with app.app_context():
        user = User(email=email, display_name=name)
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id


def _meeting(app, user_id, *, minutes=None):
    with app.app_context():
        meeting = Meeting(organizer_id=user_id, title="Q3 預算會議", status="transcribed", platform="google_meet",
                          scheduled_start=datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc))
        _db.session.add(meeting)
        _db.session.flush()
        _db.session.add(Participant(meeting_id=meeting.id, email="Li@Example.com", display_name="李小姐"))
        _db.session.add(Participant(meeting_id=meeting.id, email="org@example.com", display_name="王經理",
                                    is_organizer=True))
        _db.session.add(TranscriptSegment(meeting_id=meeting.id, start_ms=0, end_ms=4000, text="行銷預算增加兩成"))
        if minutes:
            _db.session.add(Minutes(meeting_id=meeting.id, content_markdown=minutes, llm_provider="fake",
                                    llm_model="claude-opus-5", status="draft"))
        _db.session.commit()
        return meeting.id


def _knowledge(meeting_id):
    _db.session.expire_all()  # background jobs write through their own app context / session
    return MeetingKnowledge.query.filter_by(meeting_id=meeting_id).one_or_none()


def _actions():
    return [a.action for a in AuditLog.query.order_by(AuditLog.created_at).all()]


# --- TC-58 / TC-59: written automatically after minutes are generated ---------------------

def test_generated_minutes_are_chunked_embedded_and_stored_with_metadata(client, app):
    """TC-58、TC-59：產生會議記錄後自動切片、向量化寫入 pgvector，並建立會議名稱、日期、與會者、AI 摘要等 metadata。"""
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    assert client.post(f"/meetings/{meeting_id}/minutes/generate", data={}).status_code == 302

    k = _knowledge(meeting_id)
    assert k.status == "indexed" and k.error is None and k.summary_error is None
    assert k.title == "Q3 預算會議" and k.platform == "google_meet"
    assert k.organizer_id == user_id and k.organizer_name == "王經理"
    assert k.participant_emails == ["org@example.com", "li@example.com"]  # organizer first, lower-case, no duplicates
    assert k.participant_names == ["王經理", "李小姐"]
    assert k.meeting_start == datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc)
    assert k.summary == "討論第三季行銷預算。"
    assert k.decisions == ["行銷預算增加兩成"]
    assert k.action_items == [{"task": "提交行銷提案", "owner": "李小姐", "due": "下週五"}]
    assert k.keywords == ["預算", "行銷"]
    assert k.minutes_id == Minutes.query.filter_by(meeting_id=meeting_id).one().id
    assert k.embedding_model == "fake-embedder" and k.indexed_at is not None

    chunks = MeetingKnowledgeChunk.query.filter_by(meeting_id=meeting_id).order_by(MeetingKnowledgeChunk.chunk_index).all()
    assert k.chunk_count == len(chunks) == 3
    assert [c.section for c in chunks] == ["會議摘要", "決議事項", "待辦事項"]
    assert chunks[1].content == "行銷預算增加兩成，由李小姐負責提案。"
    assert len(chunks[1].embedding) == 384
    assert chunks[1].chunk_metadata["title"] == "Q3 預算會議"
    assert chunks[1].chunk_metadata["date"] == "2026-09-29"  # DISPLAY_TIMEZONE (Asia/Taipei)
    assert chunks[1].chunk_metadata["section"] == "決議事項"
    assert chunks[1].chunk_metadata["participant_emails"] == ["org@example.com", "li@example.com"]
    embedded = app.extensions["embedding_provider"].calls[0]
    assert embedded[1] == "會議：Q3 預算會議｜日期：2026-09-29｜章節：決議事項\n行銷預算增加兩成，由李小姐負責提案。"

    assert "knowledge.indexed" in _actions()
    page = client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    assert "已寫入知識庫（3 個段落" in page


def test_similarity_search_finds_the_right_passage_with_participant_filter(client, app):
    """TC-58：寫入的向量可用 cosine 距離查詢，並可依與會者 Email 過濾（供之後的問答使用）。"""
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, minutes=MINUTES)
    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"

    query_vec = app.extensions["embedding_provider"].embed_query("李小姐 下週五 提交 提案")
    base = (MeetingKnowledgeChunk.query.join(MeetingKnowledge)
            .order_by(MeetingKnowledgeChunk.embedding.cosine_distance(query_vec)))
    assert base.filter(MeetingKnowledge.participant_emails.any("li@example.com")).first().section == "待辦事項"
    assert base.filter(MeetingKnowledge.participant_emails.any("stranger@example.com")).first() is None
    plan = _db.session.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = "
                                    "'ix_meeting_knowledge_chunks_embedding'")).scalar()
    assert "hnsw" in plan and "vector_cosine_ops" in plan


# --- TC-58: re-indexed when the saved minutes change ---------------------------------------

def test_saving_edited_minutes_reindexes_and_unchanged_content_is_skipped(client, app):
    """TC-58：儲存修改後以已儲存版本重建（舊片段移除）；內容與 metadata 未變則跳過。"""
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, minutes=MINUTES)
    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"
    first_hash = _knowledge(meeting_id).content_hash

    edited = MINUTES.replace("增加兩成", "增加三成")
    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": edited})
    k = _knowledge(meeting_id)
    assert k.status == "indexed" and k.content_hash != first_hash
    contents = [c.content for c in MeetingKnowledgeChunk.query.filter_by(meeting_id=meeting_id)]
    assert any("增加三成" in c for c in contents) and not any("增加兩成" in c for c in contents)
    assert MeetingKnowledgeChunk.query.filter_by(meeting_id=meeting_id).count() == k.chunk_count

    llm_calls = len(app.extensions["knowledge_llm_provider"].calls)
    assert indexer.index_meeting(app, meeting_id, user_id) == "skipped"
    assert len(app.extensions["knowledge_llm_provider"].calls) == llm_calls

    # A metadata change (new attendee) alone also refreshes the entry
    _db.session.add(Participant(meeting_id=meeting_id, email="new@example.com", display_name="新同事"))
    _db.session.commit()
    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"
    assert "new@example.com" in _knowledge(meeting_id).participant_emails


def test_request_during_indexing_runs_one_more_pass(client, app):
    """TC-58：索引進行中又有更新時，完成後以最新內容再跑一次，不同時重複執行。"""
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, minutes=MINUTES)
    embedder = app.extensions["embedding_provider"]
    original = embedder.embed_passages

    def edit_while_indexing(texts):
        if len(embedder.calls) == 0:
            assert indexer.schedule_index(app, meeting_id, user_id) is True  # queued, not run concurrently
            m = Minutes.query.filter_by(meeting_id=meeting_id).one()
            m.content_markdown = MINUTES + "\n## 補充\n追加內容。\n"
        return original(texts)

    embedder.embed_passages = edit_while_indexing
    indexer.schedule_index(app, meeting_id, user_id)
    assert len(embedder.calls) == 2
    assert not indexer.is_indexing(meeting_id)
    assert "補充" in [c.section for c in MeetingKnowledgeChunk.query.filter_by(meeting_id=meeting_id)]


# --- TC-59 / TC-60: failures never affect the minutes; retry --------------------------------

def test_summary_failure_still_indexes_the_minutes(client, app):
    """TC-59：AI 摘要失敗仍以全文建立索引，記錄 summary_error 並於頁面提示。"""
    app.extensions["knowledge_llm_provider"] = FakeLLM(fail_with="rate_limited")
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, minutes=MINUTES)

    assert indexer.index_meeting(app, meeting_id, user_id) == "indexed"
    k = _knowledge(meeting_id)
    assert k.summary_error == "rate_limited" and k.summary is None and k.key_points == []
    assert k.chunk_count == 3
    assert "AI 重點摘要產生失敗" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)


def test_embedding_failure_marks_failed_without_touching_minutes_and_can_be_retried(client, app):
    """TC-60：向量化失敗不影響會議記錄產生（狀態仍為 minutes_ready），知識庫標示失敗並寫稽核；按「重建」後成功。"""
    app.extensions["embedding_provider"] = HashEmbedder(fail_with="model_unavailable")
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    client.post(f"/meetings/{meeting_id}/minutes/generate", data={})
    _db.session.expire_all()  # the job wrote through its own app context / session
    assert _db.session.get(Meeting, meeting_id).status == "minutes_ready"
    assert generator.get_job(meeting_id).state == "done"
    k = _knowledge(meeting_id)
    assert k.status == "failed" and k.error == "model_unavailable"
    assert "knowledge.index_failed" in _actions()
    assert "無法載入向量模型" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)

    app.extensions["embedding_provider"] = HashEmbedder()
    resp = client.post(f"/meetings/{meeting_id}/knowledge/reindex")
    assert resp.status_code == 302
    _db.session.expire_all()
    assert _knowledge(meeting_id).status == "indexed"


def test_reindex_is_owner_only(client, app):
    """TC-60：只有主辦人能重建自己會議的索引，他人回 404。"""
    # Log in as the other user before any request: Flask-Login caches the user for the app context.
    owner_id = _login(client, app)
    with_minutes = _meeting(app, owner_id, minutes=MINUTES)
    _login(client, app, email="other@example.com", name="別人")
    assert client.post(f"/meetings/{with_minutes}/knowledge/reindex").status_code == 404
    assert _knowledge(with_minutes) is None


def test_reindex_needs_minutes(client, app):
    """TC-60：沒有會議記錄的會議不能建立索引（404）。"""
    owner_id = _login(client, app)
    without_minutes = _meeting(app, owner_id)
    assert client.post(f"/meetings/{without_minutes}/knowledge/reindex").status_code == 404


def test_disabled_knowledge_base_does_nothing(client, app):
    """TC-58：KNOWLEDGE_ENABLED=false 時不建立索引、頁面不顯示知識庫區塊。"""
    app.config["KNOWLEDGE_ENABLED"] = False
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes/generate", data={})
    assert _knowledge(meeting_id) is None
    assert 'id="knowledge"' not in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)


# --- TC-60: backfill command; data lifecycle and lockdown ----------------------------------

def test_cli_reindex_backfills_existing_minutes(client, app):
    """TC-60：`flask knowledge reindex --all/--failed` 為既有會議記錄回填索引。"""
    user_id = _login(client, app)
    first = _meeting(app, user_id, minutes=MINUTES)
    second = _meeting(app, user_id, minutes=MINUTES.replace("Q3", "Q4"))
    _meeting(app, user_id)  # no minutes: not a target

    runner = app.test_cli_runner()
    assert runner.invoke(args=["knowledge", "reindex"]).exit_code != 0
    result = runner.invoke(args=["knowledge", "reindex", "--failed"])
    assert result.exit_code == 0, result.output
    assert "indexed 2" in result.output
    assert {_knowledge(first).status, _knowledge(second).status} == {"indexed"}
    result = runner.invoke(args=["knowledge", "reindex", "--all"])
    assert "skipped 2" in result.output
    result = runner.invoke(args=["knowledge", "reindex", "--meeting", first, "--force"])
    assert "indexed 1" in result.output


def test_deleting_a_meeting_removes_its_knowledge(client, app):
    """TC-58：會議刪除時一併刪除其知識庫資料與向量（資料保存與刪除政策）。"""
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, minutes=MINUTES)
    indexer.index_meeting(app, meeting_id, user_id)
    _db.session.delete(_db.session.get(Meeting, meeting_id))
    _db.session.commit()
    assert MeetingKnowledge.query.count() == 0 and MeetingKnowledgeChunk.query.count() == 0


def test_knowledge_tables_are_locked_down_like_the_rest(app, db):
    """TC-58：新資料表同樣啟用 RLS（REQ-51，避免經 Supabase REST API 讀取會議內容）。"""
    with _db.engine.begin() as conn:
        lock_down_public_schema(conn)
        rows = conn.execute(text("SELECT tablename, rowsecurity FROM pg_tables WHERE tablename IN "
                                 "('meeting_knowledge', 'meeting_knowledge_chunks')")).fetchall()
    assert sorted(rows) == [("meeting_knowledge", True), ("meeting_knowledge_chunks", True)]
