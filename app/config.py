"""Environment-based configuration (no secrets committed)."""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./recruit.db")
USER_AGENT = os.getenv("CRAWLER_USER_AGENT", "RecruitRadarBot/0.4")
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "2.0"))
