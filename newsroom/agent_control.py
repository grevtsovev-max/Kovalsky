"""Persistent owner stop switch, stored beside operational data, never in a release."""
from pathlib import Path


class AgentDisabled(RuntimeError):
    """The owner disabled all agent work."""


def stop_file(config):
    settings = config.get("newsroom", {})
    explicit = settings.get("agent_stop_file")
    if explicit:
        return Path(explicit).expanduser().resolve()
    database = settings.get("database")
    return Path(database).expanduser().resolve().parent / "agent.disabled" if database else None


def enabled(config):
    if config.get("newsroom", {}).get("agent_enabled", True) is not True:
        return False
    path = stop_file(config)
    try:
        return path is None or not path.exists()
    except OSError:
        return False


def require_enabled(config):
    if not enabled(config):
        raise AgentDisabled("Агент отключён владельцем. Включение требует отдельной команды.")


def set_enabled(config, value):
    path = stop_file(config)
    if path is None:
        raise ValueError("Не задано постоянное хранилище состояния агента")
    if value:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, durable creation; a deployment never writes or removes this file.
        import os
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    import os
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
