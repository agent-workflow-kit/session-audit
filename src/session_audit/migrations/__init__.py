"""The migration registry: forward-only, additive, numbered.

How a later issue adds its tables (the convention is also in ``docs/design.md``, Store):

1. Number: the issue number times 100, plus 0 to 99 for a second or third migration of the same
   issue (issue #9 owns 900 to 999). Two issues never share a number, so there is nothing to
   negotiate. A migration may use only objects of migrations with a lower number.
2. One new module ``m<number>_<slug>.py`` here, exporting ``MIGRATION = Migration(...)``
   (see ``base.py`` for what a statement may be).
3. One import line and one tuple line below. The tuple has a slot per known issue, set apart by
   blank lines, so two issues editing their own slots do not conflict; two import lines added
   side by side may, and the resolution is to keep both. Add a slot for an issue that is not
   listed, in numeric order, with blank lines around it.
4. A test of the new tables beside the issue's own tests. ``tests/test_store_schema.py`` lists
   every column of the store and must be extended in the same change, so a column that could
   hold conversation text is reviewed.

A database is "newer" when its ledger holds a number this code does not know. Applying is by the
ledger (every known migration not yet in it, in number order), never by a single version number,
so a branch that lands late still applies on stores that already took a higher number.
"""

from __future__ import annotations

from .base import Migration
from .m800_initial_schema import MIGRATION as M800

MIGRATIONS: tuple[Migration, ...] = (
    M800,
    # #9 leases: 900
    # #13: 1300
    # #15: 1500
    # #18: 1800
    # #19: 1900
    # #23: 2300
    # #27: 2700
    # #29: 2900
    # #31: 3100
)

__all__ = ["MIGRATIONS", "Migration"]
