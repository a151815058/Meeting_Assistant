from app.meetings.calendar_sync import _extract_google_attendees, _extract_microsoft_attendees


def test_extract_google_attendees_flags_organizer():
    event = {
        "organizer": {"email": "boss@example.com"},
        "attendees": [
            {"email": "boss@example.com", "displayName": "Boss", "responseStatus": "accepted"},
            {"email": "staff@example.com", "responseStatus": "needsAction"},
        ],
    }

    attendees = _extract_google_attendees(event)

    assert attendees[0]["is_organizer"] is True
    assert attendees[0]["response_status"] == "accepted"
    assert attendees[1]["display_name"] == "staff@example.com"
    assert attendees[1]["is_organizer"] is False


def test_extract_microsoft_attendees_flags_organizer_and_skips_missing_email():
    event = {
        "organizer": {"emailAddress": {"address": "boss@example.com"}},
        "attendees": [
            {
                "emailAddress": {"address": "boss@example.com", "name": "Boss"},
                "status": {"response": "accepted"},
            },
            {"emailAddress": {"name": "No Email"}, "status": {"response": "none"}},
        ],
    }

    attendees = _extract_microsoft_attendees(event)

    assert len(attendees) == 1
    assert attendees[0]["is_organizer"] is True
    assert attendees[0]["response_status"] == "accepted"
