import re

from app.extensions import db as _db
from app.models.meeting import Meeting, Participant
from app.models.user import User


def test_login_page_places_visual_left_of_card(client):
    """TC-25: 登入頁主視覺圖與登入卡片為並排的獨立欄位（圖左、卡片右），互不重疊。"""
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    hero = html.index('class="login-hero"')
    visual = html.index('class="login-visual"')
    card = html.index('class="login-card"')
    assert hero < visual < card
    assert 'aria-label="會議小助手機器人介紹圖"' in html


def test_login_background_image_is_served(client):
    """TC-24: 登入頁主視覺圖可正常載入。"""
    resp = client.get("/static/pic/background.png")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_header_has_no_login_link_for_anonymous_users(client):
    """TC-42: 未登入時頁首右上角不顯示「登入」連結（登入頁本身已提供登入按鈕）。"""
    html = client.get("/auth/login").get_data(as_text=True)
    nav = html[html.index("<nav>"):html.index("</nav>")]
    assert "/auth/login" not in nav
    assert "登入" not in nav


def _nav(html: str) -> str:
    return html[html.index("<nav>"):html.index("</nav>")]


def test_site_root_sends_anonymous_users_to_login(client):
    """TC-53: 網站根網址不回 404：未登入經會議列表導向登入頁。"""
    resp = client.get("/")
    assert resp.status_code == 302 and resp.headers["Location"] == "/meetings/"
    assert client.get("/", follow_redirects=True).request.path == "/auth/login"


def test_login_redirect_shows_no_please_log_in_notice(client):
    """TC-54: 未登入被導向登入頁時，不顯示「Please log in to access this page.」提示。"""
    resp = client.get("/meetings/", follow_redirects=True)
    assert resp.request.path == "/auth/login"
    html = resp.get_data(as_text=True)
    assert "Please log in" not in html
    assert 'class="flash' not in html


def test_site_root_shows_meetings_when_signed_in(client, app, db):
    """TC-53: 已登入時網站根網址導向會議列表。"""
    with app.app_context():
        user = User(email="root@example.com", display_name="Root")
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200 and resp.request.path == "/meetings/"


def test_header_links_are_buttons_and_mark_current_page(client, app, db):
    """TC-44: 登入後頁首「會議」「範本」「登出」為導覽按鈕，目前所在頁面以 aria-current 標示。"""
    with app.app_context():
        user = User(email="nav@example.com", display_name="Nav")
        _db.session.add(user)
        _db.session.commit()
        user_id = user.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True

    nav = _nav(client.get("/meetings/").get_data(as_text=True))
    buttons = re.findall(r'<a class="(nav-btn[^"]*)" href="([^"]+)"([^>]*)>([^<]+)</a>', nav)
    assert [(href, text) for _, href, _, text in buttons] == [
        ("/meetings/", "會議"), ("/templates/", "範本"), ("/auth/logout", "登出")]
    assert "nav-btn-logout" in buttons[2][0]
    assert [bool(attrs.strip()) for _, _, attrs, _ in buttons] == [True, False, False]

    nav = _nav(client.get("/templates/").get_data(as_text=True))
    assert re.search(r'href="/templates/" aria-current="page"', nav)
    assert 'href="/meetings/" aria-current' not in nav


def test_header_nav_buttons_have_no_border(client):
    """TC-48: 頁首導覽按鈕（含滑過、目前頁面、登出狀態）皆不設定外框。"""
    html = client.get("/auth/login").get_data(as_text=True)
    rules = re.findall(r"(\.nav-btn[^{]*)\{([^}]*)\}", html)
    assert rules
    for selector, body in rules:
        assert "border:" not in body and "border-color" not in body, selector


def _login_with_meeting(client, app):
    with app.app_context():
        user = User(email="tabs@example.com", display_name="Tabs")
        _db.session.add(user)
        _db.session.flush()
        meeting = Meeting(organizer_id=user.id, title="頁籤測試會議", status="scheduled", platform="google_meet")
        _db.session.add(meeting)
        _db.session.flush()
        _db.session.add(Participant(meeting_id=meeting.id, email="li@example.com", display_name="李小姐"))
        _db.session.commit()
        user_id, meeting_id = user.id, meeting.id
    with client.session_transaction() as sess:
        sess["_user_id"] = user_id
        sess["_fresh"] = True
    return meeting_id


def _panel(html: str, panel_id: str) -> str:
    start = html.index(f'id="{panel_id}"')
    nxt = html.find('role="tabpanel"', start)
    return html[start:nxt if nxt != -1 else html.index("<script", start)]


def test_meeting_page_splits_live_minutes_and_participants_into_tabs(client, app, db):
    """TC-45: 會議頁以頁籤區分：「即時錄音轉文字」含錄音控制與逐字稿，「會議記錄」「與會人員」各自獨立頁籤。"""
    meeting_id = _login_with_meeting(client, app)
    html = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)

    tabs = re.findall(r'role="tab" id="(tab-[a-z]+)" aria-controls="(panel-[a-z]+)"[^>]*>([^<]+)</button>', html)
    assert [(t, p) for t, p, _ in tabs] == [
        ("tab-live", "panel-live"), ("tab-minutes", "panel-minutes"), ("tab-participants", "panel-participants")]
    assert [label for _, _, label in tabs] == ["即時錄音轉文字", "會議記錄", "與會人員（1）"]

    live = _panel(html, "panel-live")
    assert 'id="recorder"' in live and 'id="record-start"' in live and 'id="transcript-body"' in live
    assert "逐字稿" in live and "產生會議記錄" not in live and "li@example.com" not in live

    minutes = _panel(html, "panel-minutes")
    assert "以預設範本產生會議記錄" in minutes and 'id="transcript-body"' not in minutes

    participants = _panel(html, "panel-participants")
    assert "li@example.com" in participants and "新增與會者" in participants and "從行事曆同步與會者名單" in participants
    assert 'src="/static/js/tabs.js"' in html
    assert client.get("/static/js/tabs.js").status_code == 200


def test_meeting_tabs_strip_has_no_scrollbar(client, app, db):
    """TC-49: 會議頁頁籤列不設 overflow 捲動（避免出現上下箭頭），窄螢幕自動換行，頁籤有固定行高。"""
    meeting_id = _login_with_meeting(client, app)
    html = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    tabs_rule = re.search(r"\.tabs \{([^}]*)\}", html).group(1)
    assert "overflow" not in tabs_rule
    assert "flex-wrap: wrap" in tabs_rule
    tab_rule = re.search(r"\.tab \{([^}]*)\}", html).group(1)
    assert "line-height" in tab_rule and "padding" in tab_rule


def test_participant_changes_return_to_participants_tab(client, app, db):
    """TC-45: 新增／移除與會者後導回會議頁的「與會人員」頁籤（#participants）。"""
    meeting_id = _login_with_meeting(client, app)

    resp = client.post(f"/meetings/{meeting_id}/participants", data={"email": "new@example.com"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/meetings/{meeting_id}#participants")
    resp = client.post(f"/meetings/{meeting_id}/participants", data={"email": "not-an-email"})
    assert resp.headers["Location"].endswith("#participants")
    with app.app_context():
        pid = Participant.query.filter_by(meeting_id=meeting_id, email="new@example.com").one().id
    resp = client.post(f"/meetings/{meeting_id}/participants/{pid}/delete")
    assert resp.headers["Location"].endswith(f"/meetings/{meeting_id}#participants")


def test_meeting_status_is_shown_in_chinese(client, app, db):
    """TC-46: 會議狀態以中文顯示（scheduled→已安排、transcribed→轉錄），會議列表與會議頁一致；錄音時即時更新的狀態文字同樣取自中文標籤。"""
    from app.filters import meeting_status

    assert meeting_status("scheduled") == "已安排"
    assert meeting_status("transcribed") == "轉錄"
    assert meeting_status("something_new") == "something_new"

    meeting_id = _login_with_meeting(client, app)
    dashboard = client.get("/meetings/").get_data(as_text=True)
    assert "<td>已安排</td>" in dashboard and ">scheduled<" not in dashboard

    page = client.get(f"/meetings/{meeting_id}").get_data(as_text=True)
    assert re.search(r'id="meeting-status"[^>]*>已安排</span>', page)
    assert 'data-label-transcribed="轉錄"' in page and 'data-label-recording="錄音中"' in page

    with app.app_context():
        _db.session.get(Meeting, meeting_id).status = "transcribed"
        _db.session.commit()
    assert re.search(r'id="meeting-status"[^>]*>轉錄</span>', client.get(f"/meetings/{meeting_id}").get_data(as_text=True))
