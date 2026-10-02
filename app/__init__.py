import os

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import get_config
from app.extensions import csrf, db, init_celery, login_manager, migrate, socketio


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))
    if app.config["DEV_LOGIN_ENABLED"] and not (app.debug or app.testing):
        raise RuntimeError("DEV_LOGIN_ENABLED must never be set outside development/testing")
    if app.config["MAIL_OUTBOX_ENABLED"] and not (app.debug or app.testing):
        raise RuntimeError("MAIL_OUTBOX_ENABLED must never be set outside development/testing")
    # Meeting data travels over the internet to Supabase: production must use an encrypted connection.
    if (app.config["SUPABASE_ENABLED"] and not (app.debug or app.testing)
            and app.config["SUPABASE_DB_SSLMODE"] not in ("require", "verify-ca", "verify-full")):
        raise RuntimeError("SUPABASE_DB_SSLMODE must be require/verify-ca/verify-full outside development")
    # oauthlib only accepts https callback URLs. Local development runs on http://localhost,
    # so allow plain http there; anywhere else the OAuth code must never travel over http.
    if app.debug and not app.testing:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    elif not app.testing and os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"):
        raise RuntimeError("OAUTHLIB_INSECURE_TRANSPORT must never be set outside development")

    # Behind a TLS-terminating proxy (Render) the app sees plain http: take the client's scheme, host
    # and IP from the proxy's headers, or OAuth rejects the callback URL as insecure.
    hops = app.config["TRUSTED_PROXY_HOPS"]
    if hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops)

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
    from app.projects import projects_bp
    from app.public import public_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(minutes_bp)
    app.register_blueprint(public_bp)

    from app import knowledge

    knowledge.register(app)

    from app.transcription import register_socketio_handlers

    register_socketio_handlers(socketio)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
