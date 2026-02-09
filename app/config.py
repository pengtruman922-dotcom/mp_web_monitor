import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DOWNLOADS_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "db.sqlite"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Database
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

# Application
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-to-a-random-secret-key")

# Default LLM settings (used to seed the database)
DEFAULT_LLM_API_URL = os.getenv("LLM_API_URL", "")
DEFAULT_LLM_API_KEY = os.getenv("LLM_API_KEY", "")
DEFAULT_LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "")

# Default SMTP settings
DEFAULT_SMTP_HOST = os.getenv("SMTP_HOST", "")
DEFAULT_SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
DEFAULT_SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
DEFAULT_SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
DEFAULT_SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
DEFAULT_SENDER_NAME = os.getenv("SENDER_NAME", "政策情报助手")

# Agent settings
AGENT_MAX_TURNS = 50  # Maximum LLM call rounds per agent
AGENT_PAGE_DELAY = 2.0  # Seconds between page visits (anti-crawl)
AGENT_MAX_FILE_SIZE_MB = 50  # Max downloadable file size

# LLM settings
LLM_MAX_RETRIES = 3  # Max retry attempts for transient LLM errors
LLM_MAX_CONCURRENCY = 3  # Max concurrent LLM API requests

# Source-level concurrency
AGENT_MAX_CONCURRENCY = 5  # Max concurrent source agents running in parallel
