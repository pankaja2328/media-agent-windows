import threading
import time
from collections import deque


_LOCK = threading.Lock()
_EVENTS = deque(maxlen=500)
_SEQUENCE = 0


def reset_progress(prompt: str = "") -> None:
    global _SEQUENCE
    with _LOCK:
        _EVENTS.clear()
        _SEQUENCE = 0


def emit_progress(stage: str, **details) -> None:
    global _SEQUENCE
    with _LOCK:
        _SEQUENCE += 1
        _EVENTS.append(
            {
                "sequence": _SEQUENCE,
                "time": time.time(),
                "stage": stage,
                **details,
            }
        )


def progress_after(sequence: int) -> dict:
    with _LOCK:
        events = [event.copy() for event in _EVENTS if event["sequence"] > sequence]
        latest = _SEQUENCE
    return {"events": events, "latest": latest}
