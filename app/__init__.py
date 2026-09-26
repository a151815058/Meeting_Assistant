import os

from flask import Flask

from app.config import get_config
from app.extensions import csrf, db, init_celery, login_manager, migrate, socketio


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))
    if app.config["DEV_LOGIN_ENABLED"] and not (app.debug or app.testing):
        raise RuntimeError("DEV_LOGIN_ENABLED must never be set outside development/testing")
    if app.config["MAIL_OUTBOX_ENABLED"] and not (app.debug or app.testing):
        raise RuntimeError("MAIL_OUTBOX_ENABLED must never be set outside development/testing")
    # oauthlib only accepts https callback URLs. Local development runs on http://localhost,
    # so allow plain http there; anywhere else the OAuth code must never travel over http.
    if app.debug and not app.testing:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    elif not app.testing and os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"):
        raise RuntimeError("OAUTHLIB_INSECURE_TRANSPORT must never be set outside development")

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    socketio.init_app(app, message_queue=app.config.get("SOCKETIO_MESSAGE_QUEUE"))
    init_celery(app)

    from app import filters

    filters.register(app)

    from app.auth import auth_bp
    from app.meetings import meetings_bp
    from app.minutes import minutes_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(minutes_bp)

    from app.transcription import register_socketio_handlers

    register_socketio_handlers(socketio)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
