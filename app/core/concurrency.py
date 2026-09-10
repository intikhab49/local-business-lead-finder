from app.core.config import settings

DEFAULT_MAX_CONCURRENT_TASKS = 5


def get_max_concurrent_tasks() -> int:
    value = settings.max_concurrent_tasks
    if value and value > 0:
        return value
    return DEFAULT_MAX_CONCURRENT_TASKS
