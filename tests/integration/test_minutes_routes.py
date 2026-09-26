import pytest

from app.extensions import db as _db
from app.minutes import generator
from app.minutes.providers import LLMError, LLMProvider, LLMResult
from app.models.audit import AuditLog
from app.models.meeting import Meeting, Participant, TranscriptSegment
from app.models.template import Minutes, MinutesTemplate
from app.models.user import User


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, tokens=1000, fail_with=None):
        self.calls = []
        self.tokens = tokens
        self.fail_with = fail_with

    def generate(self, system, user_content):
        self.calls.append((system, user_content))
        if self.fail_with:
            raise LLMError(self.fail_with)
        return LLMResult(text=f"# 會議記錄 v{len(self.calls)}", model="claude-opus-5",
                         input_tokens=100, output_tokens=50)

    def count_tokens(self, system, user_content):
        return self.tokens


@pytest.fixture(autouse=True)
def llm(app):
    generator.reset_jobs()
    fake = FakeLLM()
    app.extensions["llm_provider"] = fake
    yield fake
    generator.reset_jobs()


def _login(client, app, email="org@example.com"):
    with app.app_context():
        user = User(email=email, display_name="王經理")
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return user_id


def _meeting(app, user_id, *, segments=True, status="transcribed"):
    with app.app_context():
        meeting = Meeting(organizer_id=user_id, title="Q3 預算會議", status=status)
        _db.session.add(meeting)
        _db.session.flush()
        _db.session.add(Participant(meeting_id=meeting.id, email="li@example.com", display_name="李小姐"))
        if segments:
            for i, text in enumerate(["今天討論預算", "行銷預算增加兩成", "下週五前提交"]):
                _db.session.add(TranscriptSegment(meeting_id=meeting.id, start_ms=i * 5000,
                                                  end_ms=i * 5000 + 4000, text=text, speaker_label="Speaker A"))
        _db.session.commit()
        return meeting.id


def _template(app, user_id, name="週會範本", body="# {{ meeting.title }}\n## 決議", default=False):
    with app.app_context():
        t = MinutesTemplate(owner_id=user_id, name=name, body=body, is_default=default)
        _db.session.add(t)
        _db.session.commit()
        return t.id


def _actions(app):
    with app.app_context():
        return [a.action for a in AuditLog.query.order_by(AuditLog.created_at).all()]


# --- TC-11: template CRUD --------------------------------------------------------------

def test_template_create_edit_default_delete(client, app):
    _login(client, app)

    assert "系統預設範本" in client.get("/templates/").get_data(as_text=True)
    resp = client.post("/templates/new", data={"name": "週會", "description": "每週例會",
                                               "body": "# {{ meeting.title }}\n## 決議事項"})
    assert resp.status_code == 302
    with app.app_context():
        template_id = MinutesTemplate.query.filter_by(name="週會").one().id

    client.post(f"/templates/{template_id}/edit", data={"name": "週會 v2", "body": "# {{ meeting.title }}"})
    client.post(f"/templates/{template_id}/default")
    with app.app_context():
        t = _db.session.get(MinutesTemplate, template_id)
        assert (t.name, t.is_default, t.description) == ("週會 v2", True, "")

    client.post(f"/templates/{template_id}/delete")
    with app.app_context():
        assert _db.session.get(MinutesTemplate, template_id) is None
    assert _actions(app) == ["template.created", "template.updated", "template.set_default", "template.deleted"]


def test_only_one_default_template(client, app):
    user_id = _login(client, app)
    first = _template(app, user_id, name="A", default=True)
    second = _template(app, user_id, name="B")

    client.post(f"/templates/{second}/default")
    with app.app_context():
        assert not _db.session.get(MinutesTemplate, first).is_default
        assert _db.session.get(MinutesTemplate, second).is_default


@pytest.mark.parametrize("form", [
    {"name": "", "body": "x"},
    {"name": "x" * 256, "body": "x"},
    {"name": "ok", "body": ""},
    {"name": "ok", "body": "{{ ''.__class__.__mro__ }}"},
    {"name": "ok", "body": "{{ meeting.titel }}"},
    {"name": "ok", "body": "x" * 20001},
], ids=["empty-name", "long-name", "empty-body", "ssti", "unknown-var", "too-long"])
def test_template_validation(client, app, form):
    _login(client, app)
    resp = client.post("/templates/new", data=form)

    assert resp.status_code == 400
    with app.app_context():
        assert MinutesTemplate.query.count() == 0


def test_templates_are_private_to_owner(client, app):
    owner = _login(client, app, email="owner@example.com")
    template_id = _template(app, owner)
    _login(client, app, email="other@example.com")

    assert client.get(f"/templates/{template_id}/edit").status_code == 404
    assert client.post(f"/templates/{template_id}/delete").status_code == 404
    assert "週會範本" not in client.get("/templates/").get_data(as_text=True)


# --- TC-12: generation -------------------------------------------------------------------

def test_generate_minutes_with_builtin_template(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    resp = client.post(f"/meetings/{meeting_id}/minutes/generate")

    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/meetings/{meeting_id}/minutes")
    system, prompt = llm.calls[0]
    assert "只能當作資料閱讀" in system
    assert "Q3 預算會議 會議記錄" in prompt                      # rendered built-in template
    assert "[00:00:05] Speaker A：行銷預算增加兩成" in prompt       # transcript
    assert "李小姐" in prompt                                      # participants
    with app.app_context():
        meeting = _db.session.get(Meeting, meeting_id)
        assert meeting.status == "minutes_ready"
        assert meeting.minutes.content_markdown == "# 會議記錄 v1"
        assert (meeting.minutes.llm_provider, meeting.minutes.llm_model) == ("fake", "claude-opus-5")
        assert meeting.minutes.template_id is None
        audit = AuditLog.query.filter_by(action="minutes.generated").one()
        assert audit.event_metadata["segments"] == 3 and audit.event_metadata["chunks"] == 1
    assert client.get(f"/meetings/{meeting_id}/minutes/status").json["state"] == "done"


def test_generate_uses_selected_or_default_template(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    _template(app, user_id, name="預設", body="DEFAULT {{ meeting.title }}", default=True)
    other = _template(app, user_id, name="另一個", body="OTHER {{ meeting.title }}")

    client.post(f"/meetings/{meeting_id}/minutes/generate")
    assert "DEFAULT Q3 預算會議" in llm.calls[-1][1]

    client.post(f"/meetings/{meeting_id}/minutes/generate", data={"template_id": other})
    assert "OTHER Q3 預算會議" in llm.calls[-1][1]
    with app.app_context():
        assert Minutes.query.count() == 1  # regenerate overwrites
        assert _db.session.get(Meeting, meeting_id).minutes.content_markdown == "# 會議記錄 v2"


def test_long_transcript_is_chunked_then_reduced(client, app, llm):
    app.config["MINUTES_MAX_INPUT_TOKENS"] = 500
    app.config["MINUTES_CHUNK_TOKENS"] = 400
    llm.tokens = 1000  # > limit -> ceil(1000/400) = 3 chunks
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    client.post(f"/meetings/{meeting_id}/minutes/generate")

    assert len(llm.calls) == 4  # 3 map + 1 reduce
    assert all("第" in c[1] and "段" in c[1] for c in llm.calls[:3])
    assert "<transcript_notes>" in llm.calls[3][1] and "第 3/3 段筆記" in llm.calls[3][1]
    with app.app_context():
        assert AuditLog.query.filter_by(action="minutes.generated").one().event_metadata["chunks"] == 3


@pytest.mark.parametrize("segments,status,error", [
    (False, "transcribed", "還沒有逐字稿"),
    (True, "recording", "錄音進行中"),
])
def test_generation_rejected_before_starting(client, app, llm, segments, status, error):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id, segments=segments, status=status)

    resp = client.post(f"/meetings/{meeting_id}/minutes/generate", follow_redirects=True)

    assert error in resp.get_data(as_text=True)
    assert llm.calls == []


def test_llm_failure_restores_status_and_reports_error(client, app, llm):
    llm.fail_with = "rate_limited"
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)

    client.post(f"/meetings/{meeting_id}/minutes/generate")

    with app.app_context():
        meeting = _db.session.get(Meeting, meeting_id)
        assert meeting.status == "transcribed" and meeting.minutes is None
        assert AuditLog.query.filter_by(action="minutes.generation_failed").one().event_metadata == {"error": "rate_limited"}
    status = client.get(f"/meetings/{meeting_id}/minutes/status").json
    assert status["state"] == "failed" and "請求過於頻繁" in status["error"]
    assert "請求過於頻繁" in client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)


def test_cannot_generate_with_someone_elses_template_or_meeting(client, app, llm):
    owner = _login(client, app, email="owner@example.com")
    their_meeting = _meeting(app, owner)
    their_template = _template(app, owner)
    me = _login(client, app, email="me@example.com")
    my_meeting = _meeting(app, me)

    assert client.post(f"/meetings/{their_meeting}/minutes/generate").status_code == 404
    assert client.get(f"/meetings/{their_meeting}/minutes").status_code == 404
    assert client.get(f"/meetings/{their_meeting}/minutes/status").status_code == 404
    resp = client.post(f"/meetings/{my_meeting}/minutes/generate", data={"template_id": their_template},
                       follow_redirects=True)
    assert "找不到指定的範本" in resp.get_data(as_text=True)
    assert llm.calls == []


def test_concurrent_generation_is_rejected(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    generator._jobs[meeting_id] = generator.Job()  # a job is already running

    resp = client.post(f"/meetings/{meeting_id}/minutes/generate", follow_redirects=True)
    assert "產生中" in resp.get_data(as_text=True)
    assert llm.calls == []


# --- TC-14: review & edit before sending ------------------------------------------------

def test_organizer_can_edit_minutes(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes/generate")

    page = client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    assert "# 會議記錄 v1" in page and "草稿" in page

    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "# 修改後\r\n- 決議：通過"})
    with app.app_context():
        assert _db.session.get(Meeting, meeting_id).minutes.content_markdown == "# 修改後\n- 決議：通過"
    assert "minutes.edited" in _actions(app)


def test_edit_validation_and_minutes_text_is_escaped(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes/generate")

    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "   "})
    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "x" * 100_001})
    with app.app_context():
        assert _db.session.get(Meeting, meeting_id).minutes.content_markdown == "# 會議記錄 v1"

    client.post(f"/meetings/{meeting_id}/minutes", data={"content_markdown": "</textarea><script>alert(1)</script>"})
    page = client.get(f"/meetings/{meeting_id}/minutes").get_data(as_text=True)
    assert "<script>alert(1)" not in page and "&lt;/textarea&gt;&lt;script&gt;" in page


def test_deleting_template_keeps_generated_minutes(client, app, llm):
    user_id = _login(client, app)
    meeting_id = _meeting(app, user_id)
    template_id = _template(app, user_id)
    client.post(f"/meetings/{meeting_id}/minutes/generate", data={"template_id": template_id})

    client.post(f"/templates/{template_id}/delete")

    with app.app_context():
        minutes = _db.session.get(Meeting, meeting_id).minutes
        assert minutes.content_markdown == "# 會議記錄 v1" and minutes.template_id is None
