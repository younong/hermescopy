"""Coordinate foreground turns with single-flight background review."""

from __future__ import annotations

import threading
from typing import Any


class ReviewCoordinator:
    """Give foreground turns priority over one pending or running review."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._foreground_active = False
        self._foreground_waiters = 0
        self._review_pending = False
        self._review_running = False
        self._review_agent: Any = None

    def begin_foreground(self) -> None:
        with self._condition:
            self._foreground_waiters += 1
        try:
            with self._condition:
                while self._review_running and self._review_agent is None:
                    self._condition.wait()
                review_agent = self._review_agent
            if review_agent is not None:
                try:
                    review_agent.interrupt()
                except Exception:
                    pass
            with self._condition:
                while self._review_running or self._foreground_active:
                    self._condition.wait()
                self._foreground_active = True
        finally:
            with self._condition:
                self._foreground_waiters -= 1
                self._condition.notify_all()

    def end_foreground(self) -> None:
        with self._condition:
            self._foreground_active = False
            self._condition.notify_all()

    def reserve_review(self) -> bool:
        with self._condition:
            if self._review_pending or self._review_running:
                return False
            self._review_pending = True
            return True

    def begin_review(self) -> None:
        with self._condition:
            while self._foreground_active or self._foreground_waiters:
                self._condition.wait()
            self._review_pending = False
            self._review_running = True
            self._condition.notify_all()

    def set_review_agent(self, review_agent: Any) -> None:
        with self._condition:
            if self._review_running:
                self._review_agent = review_agent
                self._condition.notify_all()

    def cancel_review(self) -> None:
        with self._condition:
            self._review_pending = False
            self._condition.notify_all()

    def end_review(self) -> None:
        with self._condition:
            self._review_agent = None
            self._review_pending = False
            self._review_running = False
            self._condition.notify_all()
