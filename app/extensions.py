from celery import Celery
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
# cors_allowed_origins=None = same-origin only. Do NOT use [] here: engineio treats an
# empty list as "skip Origin validation", which allows cross-site WebSocket hijacking.
socketio = SocketIO(cors_allowed_origins=None)
csrf = CSRFProtect()
celery = Celery(__name__)

login_manager.login_view = "auth.login"


def init_celery(app):
    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
    )

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery
