from app.security.crypto import decrypt_token, encrypt_token


def test_encrypt_then_decrypt_round_trips(app):
    with app.app_context():
        ciphertext = encrypt_token("my-refresh-token")
        assert ciphertext != "my-refresh-token"
        assert decrypt_token(ciphertext) == "my-refresh-token"


def test_encrypt_empty_string_is_noop(app):
    with app.app_context():
        assert encrypt_token("") == ""
        assert decrypt_token("") == ""
