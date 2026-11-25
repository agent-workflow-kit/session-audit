"""The registry of commands.

Every command of the interface is one module here that exports ``COMMANDS``, a tuple of
``Command`` values (see ``session_audit/command.py``). All are registered below, in the order
of the interface (it is the order of ``--help``). An issue that implements a command replaces
that module's ``run`` (and extends ``check``); it does not touch this file or ``cli.py``. A new
command adds one module and one name to ``MODULES``.
"""

from __future__ import annotations

from ..command import Command
from . import (
    cron,
    export,
    ingest,
    prune,
    retro_delete,
    retro_list,
    retro_run,
    retro_show,
    sessions,
    show,
    usage,
    validate,
)

MODULES = (
    ingest,
    sessions,
    usage,
    show,
    retro_run,
    retro_list,
    retro_show,
    retro_delete,
    prune,
    export,
    validate,
    cron,
)

COMMANDS: tuple[Command, ...] = tuple(c for module in MODULES for c in module.COMMANDS)
