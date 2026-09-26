import os

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.extensions import socketio  # noqa: E402

app = create_app(os.environ.get("FLASK_ENV", "development"))

if __name__ == "__main__":
    from app.transcription.streaming import warm_up

    warm_up(app)  # preload speech models while the server starts, not on a user's first connect
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
