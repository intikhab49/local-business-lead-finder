import logging
import sys

from app.core.config import get_settings


def setup_logging(log_level: str | None = None) -> None:
    settings = get_settings()
    level = log_level or settings.log_level

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=settings.log_format,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
