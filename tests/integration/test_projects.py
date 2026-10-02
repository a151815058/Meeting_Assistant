"""Project management (REQ-64), meetings filed under a project (REQ-65), attendee names (REQ-66)
and the project / attendee names in the minutes context (REQ-67)."""
from datetime import date, datetime, timezone

import pytest

from app.extensions import db as _db
from app.meetings import calendar_sync
from app.minutes import export, prompting
from app.models.audit import AuditLog
from app.models.meeting import Meeting, Participant
from app.models.project import Project, ProjectStakeholder
from app.models.template import Minutes
from app.models.user import OAuthAccount, User

GOOGLE_SCOPES = "openid https://www.googleapis.com/auth/calendar.readonly"


def _user(email="pm@example.com", name="王經理", *, google=False):
    user = User(email=email, display_name=name)
    _db.session.add(user)
    _db.session.flush()
    if google:
        account = OAuthAccount(user_id=user.id, provider="google", provider_account_id="google-1", scopes=GOOGLE_SCOPES)
        account.refresh_token = "rt"
        _db.session.add(account)
    _db.session.commit()
    return user.id


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True


def _project(owner_id, name="官網改版", *, status="active", stakeholders=()):
    project = Project(owner_id=owner_id, name=name, status=status, description="2026 年官網重新設計",
                      start_date=date(2026, 7, 1), end_date=date(2026, 12, 31))
    project.stakeholders = [ProjectStakeholder(name=n, email=e, role=r, position=i)
                            for i, (n, e, r) in enumerate(stakeholders)]
    _db.session.add(project)
    _db.session.commit()
    return project.id


def _meeting(organizer_id, title="週會", **kwargs):
    meeting = Meeting(organizer_id=organizer_id, title=title, **kwargs)
    _db.session.add(meeting)
    _db.session.commit()
    return meeting.id


def _actions():
    return [a.action for a in AuditLog.query.order_by(AuditLog.created_at).all()]


FORM = {
    "name": "  官網   改版 ", "description": "2026 年官網重新設計\r\n第二行", "start_date": "2026-07-01",
    "end_date": "2026-12-31", "status": "active",
    "stakeholder_name": ["陳總監", "", " 林  設計師 "],
    "stakeholder_email": ["Chen@Example.com", "", ""],
    "stakeholder_role": ["專案發起人", "", "設計組"],
}


# --- TC-64: project management ----------------------------------------------------------------

@pytest.mark.parametrize("method, path", [
    ("get", "/projects/"), ("get", "/projects/new"), ("post", "/projects/new"),
    ("get", "/projects/x/edit"), ("post", "/projects/x/delete"),
])
def test_project_pages_require_login(client, method, path):
    """TC-64：專案管理各頁皆需登入。"""
    resp = getattr(client, method)(path)
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]


def test_create_project_with_period_and_stakeholders(client, app):
    """TC-64：新增專案（名稱、說明、期程、狀態、利害關係人）；空白列略過、Email 轉小寫、多餘空白整理，並寫入稽核紀錄。"""
    user_id = _user()
    _login(client, user_id)
    form = client.get("/projects/new").get_data(as_text=True)
    assert 'name="stakeholder_name"' in form and "project_form.js" in form and "onclick" not in form

    resp = client.post("/projects/new", data=FORM, follow_redirects=True)

    page = resp.get_data(as_text=True)
    assert "專案已建立" in page and "官網 改版" in page and "2026-07-01 ~ 2026-12-31" in page and "進行中" in page
    project = Project.query.one()
    assert (project.owner_id, project.name, project.status) == (user_id, "官網 改版", "active")
    assert project.description == "2026 年官網重新設計\n第二行"
    assert (project.start_date, project.end_date) == (date(2026, 7, 1), date(2026, 12, 31))
    assert [(s.name, s.email, s.role) for s in project.stakeholders] == [
        ("陳總監", "chen@example.com", "專案發起人"), ("林 設計師", None, "設計組")]
    created = AuditLog.query.filter_by(action="project.created").one()
    assert created.target_id == project.id and created.event_metadata == {"name": "官網 改版", "stakeholders": 2}


@pytest.mark.parametrize("change, message", [
    ({"name": "   "}, "專案名稱"),
    ({"name": "名" * 256}, "專案名稱"),
    ({"start_date": "2026-12-31", "end_date": "2026-07-01"}, "結束日期不能早於開始日期"),
    ({"start_date": "不是日期"}, "開始日期"),
    ({"status": "archived"}, "狀態"),
    ({"stakeholder_email": ["not-an-email", "", ""]}, "利害關係人第 1 列：Email"),
    ({"stakeholder_name": ["", "", ""]}, "利害關係人第 1 列：姓名"),  # e-mail and role without a name
], ids=["blank-name", "long-name", "period", "bad-date", "status", "bad-email", "no-stakeholder-name"])
def test_invalid_project_is_rejected_and_the_form_keeps_the_input(client, app, change, message):
    """TC-64：專案輸入驗證（名稱必填且有上限、期程先後、日期格式、狀態、利害關係人姓名與 Email），不合格不寫入並保留已填內容。"""
    _login(client, _user())
    resp = client.post("/projects/new", data={**FORM, **change})
    page = resp.get_data(as_text=True)
    assert resp.status_code == 400 and message in page
    assert "2026 年官網重新設計" in page and 'value="專案發起人"' in page
    assert Project.query.count() == 0 and "project.created" not in _actions()


def test_edit_project_replaces_fields_and_stakeholders(client, app):
    """TC-64：編輯專案會更新欄位並以表單內容取代利害關係人名單；編輯頁列出專案下的會議。"""
    user_id = _user()
    project_id = _project(user_id, stakeholders=[("陳總監", "chen@example.com", "專案發起人")])
    _meeting(user_id, "官網需求訪談", project_id=project_id)
    _login(client, user_id)

    page = client.get(f"/projects/{project_id}/edit").get_data(as_text=True)
    assert 'value="陳總監"' in page and "官網需求訪談" in page and "此專案的會議（1）" in page

    resp = client.post(f"/projects/{project_id}/edit", data={
        "name": "官網改版二期", "description": "", "start_date": "", "end_date": "", "status": "closed",
        "stakeholder_name": ["林設計師"], "stakeholder_email": [""], "stakeholder_role": [""]}, follow_redirects=True)

    assert "專案已更新" in resp.get_data(as_text=True) and "已結束" in resp.get_data(as_text=True)
    _db.session.expire_all()
    project = _db.session.get(Project, project_id)
    assert (project.name, project.description, project.start_date, project.status) == ("官網改版二期", None, None, "closed")
    assert [(s.name, s.email, s.role) for s in project.stakeholders] == [("林設計師", None, None)]
    assert ProjectStakeholder.query.count() == 1  # the replaced stakeholder row is gone
    assert "project.updated" in _actions()


def test_projects_are_private_to_their_owner(client, app):
    """TC-64、TC-65：專案只有建立者看得到、改得動；別人的專案回 404，也不能把會議歸入別人的專案。"""
    owner, other = _user(), _user("other@example.com", "他人")
    project_id = _project(owner)
    meeting_id = _meeting(other, "別人的會議")
    _login(client, other)

    assert "官網改版" not in client.get("/projects/").get_data(as_text=True)
    assert client.get(f"/projects/{project_id}/edit").status_code == 404
    assert client.post(f"/projects/{project_id}/edit", data=FORM).status_code == 404
    assert client.post(f"/projects/{project_id}/delete").status_code == 404
    assert "官網改版" not in client.get("/meetings/new").get_data(as_text=True)

    resp = client.post("/meetings/new", data={"title": "偷用專案", "project_id": project_id})
    assert resp.status_code == 400 and "選擇的專案無效" in resp.get_data(as_text=True)
    assert Meeting.query.filter_by(title="偷用專案").count() == 0

    resp = client.post(f"/meetings/{meeting_id}/project", data={"project_id": project_id}, follow_redirects=True)
    assert "選擇的專案無效" in resp.get_data(as_text=True)
    _db.session.expire_all()
    assert _db.session.get(Meeting, meeting_id).project_id is None
    assert _db.session.get(Project, project_id).name == "官網改版"


def test_deleting_a_project_keeps_its_meetings(client, app):
    """TC-64：刪除專案只刪專案與利害關係人，會議與會議記錄保留並變成不屬於任何專案。"""
    user_id = _user()
    project_id = _project(user_id, stakeholders=[("陳總監", "chen@example.com", None)])
    meeting_id = _meeting(user_id, "官網需求訪談", project_id=project_id)
    _db.session.add(Minutes(meeting_id=meeting_id, content_markdown="# 記錄", llm_provider="fake", llm_model="m"))
    _db.session.commit()
    _login(client, user_id)

    page = client.get("/projects/").get_data(as_text=True)
    assert "確定要刪除專案「官網改版」嗎？" in page and "1 場" in page and "1 位" in page

    resp = client.post(f"/projects/{project_id}/delete", follow_redirects=True)

    assert "已刪除專案「官網改版」" in resp.get_data(as_text=True)
    _db.session.expire_all()
    assert Project.query.count() == 0 and ProjectStakeholder.query.count() == 0
    meeting = _db.session.get(Meeting, meeting_id)
    assert meeting is not None and meeting.project_id is None and meeting.minutes is not None
    deleted = AuditLog.query.filter_by(action="project.deleted").one()
    assert deleted.event_metadata == {"name": "官網改版", "meetings": 1}


# --- TC-65: meetings are filed under a project --------------------------------------------------

def test_new_meeting_can_be_filed_under_an_active_project(client, app):
    """TC-65：新增會議頁有專案下拉（只列進行中的專案），建立後會議歸入專案，會議列表與會議頁顯示專案。"""
    user_id = _user()
    active = _project(user_id, "官網改版")
    _project(user_id, "舊系統汰換", status="closed")
    _login(client, user_id)

    page = client.get("/meetings/new").get_data(as_text=True)
    assert 'name="project_id"' in page and f'value="{active}"' in page and "2026-07-01 ~ 2026-12-31" in page
    assert "舊系統汰換" not in page

    resp = client.post("/meetings/new", data={"title": "官網需求訪談", "platform": "manual", "project_id": active},
                       follow_redirects=True)

    assert Meeting.query.filter_by(title="官網需求訪談").one().project_id == active
    assert f'<option value="{active}" selected>官網改版</option>' in resp.get_data(as_text=True)
    assert "<td>官網改版</td>" in client.get("/meetings/").get_data(as_text=True)

    client.post("/meetings/new", data={"title": "不屬於專案的會議", "project_id": ""})
    assert Meeting.query.filter_by(title="不屬於專案的會議").one().project_id is None


def test_meeting_project_can_be_changed_and_cleared(client, app):
    """TC-65：會議頁可變更或取消專案並寫入稽核紀錄；會議目前所屬的專案即使已結束仍會出現在下拉中。"""
    user_id = _user()
    first, closed = _project(user_id, "官網改版"), _project(user_id, "舊系統汰換", status="closed")
    meeting_id = _meeting(user_id, "週會", project_id=closed)
    _login(client, user_id)

    page = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    assert f'<option value="{closed}" selected>舊系統汰換</option>' in page and f'value="{first}"' in page

    resp = client.post(f"/meetings/{meeting_id}/project", data={"project_id": first}, follow_redirects=True)
    assert "已將會議歸入專案「官網改版」" in resp.get_data(as_text=True)
    _db.session.expire_all()
    assert _db.session.get(Meeting, meeting_id).project_id == first

    resp = client.post(f"/meetings/{meeting_id}/project", data={"project_id": ""}, follow_redirects=True)
    assert "已取消會議的專案" in resp.get_data(as_text=True)
    _db.session.expire_all()
    assert _db.session.get(Meeting, meeting_id).project_id is None
    changes = AuditLog.query.filter_by(action="meeting.project_changed").order_by(AuditLog.created_at).all()
    assert [c.event_metadata for c in changes] == [{"project_id": first}, {"project_id": None}]

    other_meeting = _meeting(_user("other@example.com", "他人"), "別人的會議")
    assert client.post(f"/meetings/{other_meeting}/project", data={"project_id": first}).status_code == 404


# --- TC-66: attendee names ------------------------------------------------------------------------

def _stub_google(mocker, attendees):
    mocker.patch("app.meetings.calendar_sync.get_valid_access_token", return_value="token")
    return mocker.patch("app.meetings.calendar_sync._fetch_google_event", return_value={
        "organizer": {"email": "pm@example.com"}, "attendees": attendees})


def test_sync_fills_missing_names_from_what_the_app_already_knows(client, app, mocker):
    """TC-66：行事曆有姓名就用行事曆的；沒有時依序用已註冊使用者、專案利害關係人、主辦人過去會議的與會者姓名補上，都沒有才顯示 Email。"""
    user_id = _user(google=True)
    _user("registered@example.com", "已註冊的小張")
    project_id = _project(user_id, stakeholders=[("陳總監", "Chen@Example.com", "發起人"),
                                                 ("利害關係人版本", "registered@example.com", None)])
    earlier = _meeting(user_id, "上次會議")
    _db.session.add(Participant(meeting_id=earlier, email="old@example.com", display_name="老同事"))
    _db.session.add(Participant(meeting_id=earlier, email="unknown@example.com", display_name="unknown@example.com"))
    _db.session.commit()
    meeting_id = _meeting(user_id, "官網需求訪談", platform="google_meet", platform_event_id="evt-1",
                          project_id=project_id)
    _stub_google(mocker, [
        {"email": "pm@example.com", "displayName": "王經理", "responseStatus": "accepted"},
        {"email": "registered@example.com"}, {"email": "chen@example.com"}, {"email": "old@example.com"},
        {"email": "unknown@example.com"},
    ])
    _login(client, user_id)

    resp = client.post(f"/meetings/{meeting_id}/sync-participants", follow_redirects=True)

    assert "已同步 5 位與會者" in resp.get_data(as_text=True)
    names = {p.email: p.display_name for p in Participant.query.filter_by(meeting_id=meeting_id)}
    assert names == {
        "pm@example.com": "王經理",                  # from the calendar
        "registered@example.com": "已註冊的小張",     # registered user wins over the stakeholder entry
        "chen@example.com": "陳總監",                # stakeholder, matched case-insensitively
        "old@example.com": "老同事",                  # the organizer's earlier meeting
        "unknown@example.com": "unknown@example.com",  # an e-mail standing in for a name is not a name
    }


def test_known_names_only_come_from_the_organizers_own_data(app):
    """TC-66：補姓名只參考主辦人自己的專案與會議，不會帶出其他使用者會議或專案中的姓名。"""
    owner, other = _user(), _user("other@example.com", "他人")
    _project(other, "別人的專案", stakeholders=[("別人填的名字", "guest@example.com", None)])
    theirs = _meeting(other, "別人的會議")
    _db.session.add(Participant(meeting_id=theirs, email="guest@example.com", display_name="別人會議中的名字"))
    _db.session.commit()
    meeting = _db.session.get(Meeting, _meeting(owner, "我的會議"))

    assert calendar_sync.known_names(meeting, ["guest@example.com", "OTHER@example.com"]) == {
        "other@example.com": "他人"}  # only the registered user's own display name
    assert calendar_sync.known_names(meeting, []) == {}


def test_edited_names_survive_a_resync(client, app, mocker):
    """TC-66：主辦人可在與會人員頁籤修改姓名（寫入稽核紀錄）；重新同步不會覆蓋手動修改的姓名，行事曆沒給姓名時也不會把已有的姓名改回 Email。"""
    user_id = _user(google=True)
    meeting_id = _meeting(user_id, "官網需求訪談", platform="google_meet", platform_event_id="evt-1")
    fetch = _stub_google(mocker, [{"email": "li@example.com", "displayName": "Li"},
                                  {"email": "guo@example.com", "displayName": "郭顧問"}])
    _login(client, user_id)
    client.post(f"/meetings/{meeting_id}/sync-participants")
    li = Participant.query.filter_by(meeting_id=meeting_id, email="li@example.com").one()

    page = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    assert f'action="/meetings/{meeting_id}/participants/{li.id}/name"' in page and 'value="Li"' in page

    resp = client.post(f"/meetings/{meeting_id}/participants/{li.id}/name", data={"display_name": "  李  小姐 "},
                       follow_redirects=True)
    assert "已更新 li@example.com 的姓名" in resp.get_data(as_text=True)
    assert AuditLog.query.filter_by(action="participant.renamed").one().event_metadata == {"email": "li@example.com"}

    fetch.return_value = {"attendees": [{"email": "li@example.com", "displayName": "Li"}, {"email": "guo@example.com"}]}
    client.post(f"/meetings/{meeting_id}/sync-participants")

    _db.session.expire_all()
    names = {p.email: (p.display_name, p.display_name_edited) for p in Participant.query.filter_by(meeting_id=meeting_id)}
    assert names == {"li@example.com": ("李 小姐", True), "guo@example.com": ("郭顧問", False)}


def test_rename_participant_is_validated_and_owner_only(client, app):
    """TC-66：姓名必填、最多 255 字；只能改自己會議的與會者。"""
    user_id, other = _user(), _user("other@example.com", "他人")
    meeting_id, other_meeting = _meeting(user_id), _meeting(other, "別人的會議")
    mine = Participant(meeting_id=meeting_id, email="li@example.com", display_name="李小姐")
    theirs = Participant(meeting_id=other_meeting, email="x@example.com", display_name="X")
    _db.session.add_all([mine, theirs])
    _db.session.commit()
    mine_id, theirs_id = mine.id, theirs.id
    _login(client, user_id)

    for bad in ("   ", "名" * 256):
        resp = client.post(f"/meetings/{meeting_id}/participants/{mine_id}/name", data={"display_name": bad},
                           follow_redirects=True)
        assert "請輸入姓名（最多 255 字）" in resp.get_data(as_text=True)
    assert client.post(f"/meetings/{other_meeting}/participants/{theirs_id}/name",
                       data={"display_name": "駭"}).status_code == 404
    assert client.post(f"/meetings/{meeting_id}/participants/{theirs_id}/name",
                       data={"display_name": "駭"}).status_code == 404
    _db.session.expire_all()
    assert _db.session.get(Participant, mine_id).display_name == "李小姐"
    assert _db.session.get(Participant, theirs_id).display_name == "X"
    assert "participant.renamed" not in _actions()


def test_manually_added_participant_without_a_name_gets_a_known_name(client, app):
    """TC-66：手動新增與會者未填姓名時，同樣以已知姓名補上；有填姓名則以填寫的為準並視為手動修改。"""
    user_id = _user()
    project_id = _project(user_id, stakeholders=[("陳總監", "chen@example.com", None)])
    meeting_id = _meeting(user_id, project_id=project_id)
    _login(client, user_id)

    client.post(f"/meetings/{meeting_id}/participants", data={"email": "Chen@Example.com"})
    client.post(f"/meetings/{meeting_id}/participants", data={"email": "new@example.com"})
    client.post(f"/meetings/{meeting_id}/participants", data={"email": "typed@example.com", "display_name": "手動輸入"})

    names = {p.email: (p.display_name, p.display_name_edited) for p in Participant.query.filter_by(meeting_id=meeting_id)}
    assert names == {"chen@example.com": ("陳總監", False), "new@example.com": ("new@example.com", False),
                     "typed@example.com": ("手動輸入", True)}


# --- TC-67 / TC-68: what the minutes are generated from ------------------------------------------

def test_minutes_context_and_export_carry_attendee_names_and_the_project(app):
    """TC-67、TC-68：產生會議記錄的資料含與會人員姓名與專案（名稱、說明、期程、利害關係人）；匯出文件的抬頭列出專案。"""
    user_id = _user()
    project_id = _project(user_id, stakeholders=[("陳總監", "chen@example.com", "專案發起人"), ("林設計師", None, None)])
    meeting_id = _meeting(user_id, "官網需求訪談", project_id=project_id,
                          scheduled_start=datetime(2026, 9, 29, 2, 0, tzinfo=timezone.utc))
    _db.session.add(Participant(meeting_id=meeting_id, email="li@example.com", display_name="李小姐"))
    _db.session.add(Participant(meeting_id=meeting_id, email="pm@example.com", display_name="王經理", is_organizer=True))
    _db.session.commit()
    meeting = _db.session.get(Meeting, meeting_id)

    context = prompting.build_context(meeting)

    assert context["participant_names"] == "王經理、李小姐"
    assert context["project"] == {
        "name": "官網改版", "description": "2026 年官網重新設計", "period": "2026-07-01 ~ 2026-12-31",
        "start_date": "2026-07-01", "end_date": "2026-12-31",
        "stakeholders": [{"name": "陳總監", "email": "chen@example.com", "role": "專案發起人"},
                         {"name": "林設計師", "email": "", "role": ""}],
        "stakeholder_names": "陳總監、林設計師",
    }
    rendered = prompting.render_template_body(prompting.BUILTIN_TEMPLATE_BODY, context)
    assert "- 專案：官網改版\n" in rendered and "- 與會人員：王經理、李小姐\n" in rendered
    assert ("專案", "官網改版") in export._meeting_info(meeting)

    meeting.project_id = None
    _db.session.commit()
    context = prompting.build_context(meeting)
    assert context["project"]["name"] == "" and context["project"]["stakeholders"] == []
    assert "專案" not in prompting.render_template_body(prompting.BUILTIN_TEMPLATE_BODY, context)
    assert "專案" not in dict(export._meeting_info(meeting))
