"""Web push is unused in plan B. Telegram is the notifier."""


def notify(*_args, **_kwargs) -> bool:
    return False
