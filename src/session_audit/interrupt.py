"""SIGHUP, SIGINT and SIGTERM: one controller per command run (issue #28).

``cli.main`` installs a ``Signals`` for the whole run (``install``) and hands it to the command as
``Context.interrupts``. It has two modes:

- **raise** (the default, every command): the first signal raises ``Interrupted`` from the handler
  in the main thread, so the command unwinds through its ``finally`` blocks and ``main`` returns
  128 + the signal. Every later signal is ignored (the handler stays installed and does nothing),
  so a second signal cannot raise out of a command's cleanup. Nothing is ever set to ``SIG_IGN``
  after the first signal: an ignored disposition would be inherited by every child the cleanup
  starts, while a handler is reset to the default by ``exec``.
- **deferred** (``retro run`` from step 13 on, ``with signals.defer():``): the first signal only
  records itself, sets the events handed out by ``watch`` (the hook runner, the cleanup bound) and
  returns. The code polls ``pending`` at its safe points and ends the retro in an orderly way: a
  signal never interrupts a SQLite transaction, a compare-and-set or a ``finally`` block, and the
  handler does no database work, starts no process and takes no lock the main thread could hold. A
  **second** signal in this mode raises ``Interrupted`` at once (with the code of the FIRST
  signal): the stack unwinds, the lease is released by compare-and-set when that is still possible,
  and whatever is left is the next housekeeping's (recovery by the recorded id).

``Interrupted`` is a ``BaseException`` so no ``except Exception`` swallows it.
"""

from __future__ import annotations

import contextlib
import signal
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

__all__ = ["Bound", "Interrupted", "Signals"]

TERMINAL_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)


class Interrupted(BaseException):
    """Raised by the signal handlers; a ``BaseException`` so no ``except Exception`` swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class Bound(threading.Event):
    """An event that reads as set once ``seconds`` have passed since the first signal or since this
    object was made, whichever is later (a signal that was handled earlier does not shorten the
    work that starts now, and does not leave it unbounded either). It never blocks: ``is_set`` is
    all the callers use."""

    def __init__(self, signals: Signals, seconds: float) -> None:
        super().__init__()
        self._signals = signals
        self._seconds = seconds
        self._born = signals.clock()

    def is_set(self) -> bool:
        first = self._signals.first_at
        if first is None:
            return super().is_set()
        return self._signals.clock() - max(first, self._born) >= self._seconds or super().is_set()


class Signals:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._first: int | None = None
        self.first_at: float | None = None
        self._count = 0
        self._deferred = False
        self._forced = False
        self._events: list[threading.Event] = []

    # -- what the code polls -------------------------------------------------------------------

    @property
    def first(self) -> int | None:
        """The number of the first signal received, or None."""
        return self._first

    @property
    def pending(self) -> bool:
        return self._first is not None

    def exit_code(self) -> int:
        """128 + the first signal (only meaningful when ``pending``)."""
        assert self._first is not None
        return 128 + int(self._first)

    def raise_if_pending(self) -> None:
        """Raise ``Interrupted`` for the first signal: the exit by signal of a command that has
        nothing left to undo."""
        if self._first is not None:
            raise Interrupted(self._first)

    # -- modes ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def defer(self) -> Iterator[None]:
        """The deferred mode (see the module text) for the duration of the block."""
        previous = self._deferred
        self._deferred = True
        try:
            yield
        finally:
            self._deferred = previous

    @contextlib.contextmanager
    def watch(self, *, preset: bool = False) -> Iterator[threading.Event]:
        """An event set by the first signal while the block runs. Not set when that signal has
        already happened (whoever runs afterwards, such as a post hook, is not cut short by it, only
        by a second signal, which raises), unless ``preset``: then a signal that is already
        pending, or that arrives while this is entered, hands out a set event, so work that must
        not start after a signal (the ``pre-retro`` hook) never does. Used to end a hook's process
        group promptly."""
        event = threading.Event()
        self._events.append(event)
        if preset and self._first is not None:
            event.set()
        try:
            yield event
        finally:
            with contextlib.suppress(ValueError):
                self._events.remove(event)

    def bound(self, seconds: float) -> Bound:
        """``Bound`` for work that starts now: set ``seconds`` after the first signal or after now,
        whichever is later."""
        return Bound(self, seconds)

    # -- the handler ---------------------------------------------------------------------------

    def handle(self, signum: int, _frame: object = None) -> None:
        self._count += 1
        if self._first is None:
            self._first = signum
            self.first_at = self.clock()
            if not self._deferred:
                raise Interrupted(signum)
            for event in list(self._events):
                event.set()
            return
        if self._deferred and not self._forced:
            self._forced = True
            raise Interrupted(self._first)
        # a later signal while the first one unwinds, or after a forced exit: nothing more to do

    @contextlib.contextmanager
    def install(self) -> Iterator[Signals]:
        """Install the handler for SIGHUP, SIGINT and SIGTERM and ignore SIGPIPE; a signal that
        was ignored on entry (``nohup``, a background job) stays ignored. SIGPIPE stays ignored so
        a closed pipe is an ``EPIPE`` error on the write, which ends the run with exit 1.
        Everything is put back on exit. Outside the main thread the host owns the signals and
        nothing is installed."""
        previous: dict[int, Any] = {}
        try:
            try:
                for sig in TERMINAL_SIGNALS:
                    current = signal.getsignal(sig)
                    if current is signal.SIG_IGN:
                        continue
                    previous[sig] = current
                    signal.signal(sig, self.handle)
                previous[signal.SIGPIPE] = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
            except ValueError:  # not the main thread: the host owns the signals
                pass
            yield self
        finally:
            for sig, old in previous.items():
                with contextlib.suppress(ValueError, TypeError, OSError):
                    signal.signal(sig, old)
