import os
import re

from sqlalchemy.engine import URL


def supabase_database_url(env=os.environ) -> str | None:
    """Connection string for a Supabase Postgres database (REQ-50), or None when SUPABASE_DB_HOST is unset.

    Built from separate fields so passwords with special characters need no manual URL-escaping.
    """
    host = env.get("SUPABASE_DB_HOST", "").strip()
    if not host:
        return None
    url = URL.create(
        "postgresql+psycopg2",
        username=env.get("SUPABASE_DB_USER", "postgres").strip(),
        password=env.get("SUPABASE_DB_PASSWORD", ""),
        host=host,
        port=int(env.get("SUPABASE_DB_PORT", 5432)),
        database=env.get("SUPABASE_DB_NAME", "postgres").strip(),
        query={"sslmode": env.get("SUPABASE_DB_SSLMODE", "require").strip()},
    )
    return url.render_as_string(hide_password=False)


def site_verification_token(raw: str) -> str:
    """The token from Search Console's HTML tag (REQ-56), whether pasted bare, as content="...",
    or as the whole <meta> tag."""
    match = re.search(r"""content\s*=\s*["']([^"']*)["']""", raw)
    return (match.group(1) if match else raw).strip().strip("\"'")


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    # Supabase settings take precedence over DATABASE_URL when SUPABASE_DB_HOST is set.
    SUPABASE_ENABLED = bool(os.environ.get("SUPABASE_DB_HOST", "").strip())
    SUPABASE_DB_SSLMODE = os.environ.get("SUPABASE_DB_SSLMODE", "require").strip()
    SQLALCHEMY_DATABASE_URI = supabase_database_url() or os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://meeting_assistant:meeting_assistant@localhost:5432/meeting_assistant",
    )
    # A remote database / pooler closes idle connections; check each pooled connection before use.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True} if SUPABASE_ENABLED else {}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL)
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
    # Only needed when several processes (multiple web workers / Celery) emit SocketIO events.
    # Leave unset for a single web process, e.g. local development without Redis.
    SOCKETIO_MESSAGE_QUEUE = os.environ.get("SOCKETIO_MESSAGE_QUEUE") or None

    TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")

    MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
    MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
    MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "common")
    MS_REDIRECT_URI = os.environ.get("MS_REDIRECT_URI", "")

    LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    LLM_MODEL = os.environ.get("LLM_MODEL", "claude-opus-5")
    LLM_EFFORT = os.environ.get("LLM_EFFORT", "high")  # low | medium | high | xhigh | max
    LLM_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", 32000))
    # Re-run a safety-declined request on Anthropic's recommended fallback model (server-side)
    LLM_FALLBACKS_ENABLED = os.environ.get("LLM_FALLBACKS_ENABLED", "true").lower() == "true"
    # Above this prompt size the transcript is summarised chunk by chunk first (map-reduce)
    MINUTES_MAX_INPUT_TOKENS = int(os.environ.get("MINUTES_MAX_INPUT_TOKENS", 600_000))
    MINUTES_CHUNK_TOKENS = int(os.environ.get("MINUTES_CHUNK_TOKENS", 150_000))
    MINUTES_INLINE = False  # True = generate synchronously inside the request (tests only)

    # Knowledge base (REQ-58 ~ REQ-60): saved minutes are chunked, embedded and stored in pgvector
    # together with meeting metadata and an AI summary, for later Q&A over past meetings.
    KNOWLEDGE_ENABLED = os.environ.get("KNOWLEDGE_ENABLED", "true").lower() == "true"
    KNOWLEDGE_INLINE = False  # True = index synchronously inside the request (tests only)
    KNOWLEDGE_CHUNK_CHARS = int(os.environ.get("KNOWLEDGE_CHUNK_CHARS", 400))
    KNOWLEDGE_CHUNK_OVERLAP = int(os.environ.get("KNOWLEDGE_CHUNK_OVERLAP", 60))
    # The summary metadata is one extra LLM call per indexed version; empty model = LLM_MODEL.
    KNOWLEDGE_SUMMARY_MODEL = os.environ.get("KNOWLEDGE_SUMMARY_MODEL", "")
    KNOWLEDGE_SUMMARY_EFFORT = os.environ.get("KNOWLEDGE_SUMMARY_EFFORT", "medium")
    # Q&A (REQ-62): one LLM call per question over the KNOWLEDGE_QA_TOP_K closest passages.
    KNOWLEDGE_QA_MODEL = os.environ.get("KNOWLEDGE_QA_MODEL", "")  # empty = LLM_MODEL
    KNOWLEDGE_QA_EFFORT = os.environ.get("KNOWLEDGE_QA_EFFORT", "medium")
    KNOWLEDGE_QA_MAX_OUTPUT_TOKENS = int(os.environ.get("KNOWLEDGE_QA_MAX_OUTPUT_TOKENS", 8000))
    KNOWLEDGE_QA_TOP_K = int(os.environ.get("KNOWLEDGE_QA_TOP_K", 8))
    # Local ONNX embedding model (no torch, no third-party API). Changing it requires a migration
    # when the dimension differs (the pgvector column is vector(384)) and a full reindex.
    EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "e5_onnx")
    EMBEDDING_MODEL_REPO = os.environ.get("EMBEDDING_MODEL_REPO", "intfloat/multilingual-e5-small")
    EMBEDDING_MODEL_REVISION = os.environ.get("EMBEDDING_MODEL_REVISION", "614241f622f53c4eeff9890bdc4f31cfecc418b3")
    EMBEDDING_ONNX_FILE = os.environ.get("EMBEDDING_ONNX_FILE", "onnx/model_qint8_avx512_vnni.onnx")
    EMBEDDING_TOKENIZER_FILE = os.environ.get("EMBEDDING_TOKENIZER_FILE", "onnx/tokenizer.json")
    EMBEDDING_BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", 16))
    EMBEDDING_CPU_THREADS = int(os.environ.get("EMBEDDING_CPU_THREADS", 2))

    # "small" keeps up with real time on a typical CPU; "medium" is more accurate but ~3x slower
    # than real time on CPU (see docs/architecture/architecture.md benchmark) — use it only with a GPU.
    WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")
    WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
    WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
    WHISPER_CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", min(8, os.cpu_count() or 4)))
    WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "zh") or None
    WHISPER_INITIAL_PROMPT = os.environ.get("WHISPER_INITIAL_PROMPT", "以下是繁體中文的會議逐字稿。")

    # Real-time transcription limits (threat_model.md DoS control, REQ-20)
    TRANSCRIPTION_INLINE = False  # True = run VAD/ASR in the SocketIO handler (tests only)
    TRANSCRIPTION_MAX_CHUNK_BYTES = int(os.environ.get("TRANSCRIPTION_MAX_CHUNK_BYTES", 32000))  # 1s of 16kHz PCM16
    TRANSCRIPTION_MAX_BYTES_PER_SECOND = int(os.environ.get("TRANSCRIPTION_MAX_BYTES_PER_SECOND", 64000))  # 2x realtime
    TRANSCRIPTION_MAX_RECORDING_SECONDS = int(os.environ.get("TRANSCRIPTION_MAX_RECORDING_SECONDS", 4 * 3600))
    # Warn the client when this much audio is queued but not yet transcribed (ASR slower than real time)
    TRANSCRIPTION_LAG_WARNING_SECONDS = int(os.environ.get("TRANSCRIPTION_LAG_WARNING_SECONDS", 30))
    # Uploaded recordings (REQ-47): the file is deleted once transcribed; length is capped by
    # TRANSCRIPTION_MAX_RECORDING_SECONDS like a live recording.
    TRANSCRIPTION_UPLOAD_MAX_BYTES = int(os.environ.get("TRANSCRIPTION_UPLOAD_MAX_BYTES", 500 * 1024 * 1024))
    # Reject oversized request bodies before they are parsed (the CSRF check reads the form first).
    MAX_CONTENT_LENGTH = TRANSCRIPTION_UPLOAD_MAX_BYTES + 1024 * 1024

    # Speaker diarization (REQ-09) — optional, needs requirements-diarization.txt + a Hugging Face token
    DIARIZATION_ENABLED = os.environ.get("DIARIZATION_ENABLED", "false").lower() == "true"
    DIARIZATION_MODEL = os.environ.get("DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1")
    HF_TOKEN = os.environ.get("HF_TOKEN", "")

    # Times shown in the UI (DB stores UTC). Calendar events offered when creating a meeting:
    # from CALENDAR_LOOKBACK_HOURS ago (meetings already under way) to CALENDAR_LOOKAHEAD_DAYS ahead.
    DISPLAY_TIMEZONE = os.environ.get("DISPLAY_TIMEZONE", "Asia/Taipei")
    CALENDAR_LOOKBACK_HOURS = int(os.environ.get("CALENDAR_LOOKBACK_HOURS", 24))
    CALENDAR_LOOKAHEAD_DAYS = int(os.environ.get("CALENDAR_LOOKAHEAD_DAYS", 14))
    CALENDAR_MAX_EVENTS = int(os.environ.get("CALENDAR_MAX_EVENTS", 50))

    # Sending minutes to participants (REQ-15 ~ REQ-17): as the organizer via Gmail / Graph
    MAIL_MAX_RECIPIENTS = int(os.environ.get("MAIL_MAX_RECIPIENTS", 100))
    MAIL_SEND_ATTEMPTS = int(os.environ.get("MAIL_SEND_ATTEMPTS", 3))  # only 429/503 are retried
    MAIL_RETRY_BACKOFF_SECONDS = float(os.environ.get("MAIL_RETRY_BACKOFF_SECONDS", 2))
    MAIL_RESEND_COOLDOWN_SECONDS = int(os.environ.get("MAIL_RESEND_COOLDOWN_SECONDS", 60))
    # Development only: organizers without a mail-capable OAuth account get their mail written
    # as .eml files here instead of sent. create_app refuses to start with it in production.
    MAIL_OUTBOX_ENABLED = False
    MAIL_OUTBOX_DIR = os.environ.get("MAIL_OUTBOX_DIR", "")  # default: <instance>/outbox
    MAIL_INLINE = False  # True = call the mail API in the request thread (tests only)
    # Word/PDF attachments together; Graph sendMail rejects requests over ~4 MB
    MAIL_MAX_ATTACHMENT_BYTES = int(os.environ.get("MAIL_MAX_ATTACHMENT_BYTES", 3_000_000))

    # PDF export needs a font with Traditional Chinese glyphs. Empty = auto-detect
    # (Windows Microsoft JhengHei, or fonts-noto-cjk on Debian/Ubuntu — see Dockerfile).
    PDF_FONT_PATH = os.environ.get("PDF_FONT_PATH", "")
    PDF_BOLD_FONT_PATH = os.environ.get("PDF_BOLD_FONT_PATH", "")

    # Reverse proxies in front of the app that add X-Forwarded-For/-Proto/-Host (REQ-52), e.g. 1 on
    # Render. 0 = no proxy: the headers are ignored, since a client could otherwise forge its IP / scheme.
    TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", 0))

    # Public pages (REQ-55 / REQ-56): contact shown on the privacy policy, and the token from Google
    # Search Console's "HTML tag" method that proves ownership of the site for OAuth brand verification.
    PRIVACY_CONTACT_EMAIL = os.environ.get("PRIVACY_CONTACT_EMAIL", "")
    GOOGLE_SITE_VERIFICATION = site_verification_token(os.environ.get("GOOGLE_SITE_VERIFICATION", ""))

    WTF_CSRF_ENABLED = True
    # Passwordless login for local testing without OAuth credentials (REQ-29).
    # Only DevelopmentConfig turns it on; create_app refuses to start with it in production.
    DEV_LOGIN_ENABLED = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    DEV_LOGIN_ENABLED = os.environ.get("DEV_LOGIN_ENABLED", "true").lower() == "true"
    MAIL_OUTBOX_ENABLED = os.environ.get("MAIL_OUTBOX_ENABLED", "true").lower() == "true"


class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    TRANSCRIPTION_INLINE = True
    MINUTES_INLINE = True
    MAIL_INLINE = True
    MAIL_RETRY_BACKOFF_SECONDS = 0
    # Off by default so other suites never load the embedding model; knowledge tests switch it on.
    KNOWLEDGE_ENABLED = False
    KNOWLEDGE_INLINE = True
    SOCKETIO_MESSAGE_QUEUE = None
    DIARIZATION_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://meeting_assistant:meeting_assistant@localhost:5432/meeting_assistant_test",
    )
    TOKEN_ENCRYPTION_KEY = os.environ.get(
        "TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None):
    name = name or os.environ.get("FLASK_ENV", "development")
    return config_by_name.get(name, DevelopmentConfig)
