"""One place for user-facing failures.

Anything a person can cause by filling a form in raises ``FormError``. The
router catches it and re-renders the same screen with the message on top and
everything they typed still in the boxes — a person should never be dropped
onto a bare error page for a typo.
"""
from __future__ import annotations


class FormError(ValueError):
    """A problem with what was submitted, written for the person who submitted it.

    ``field`` optionally names the input to highlight and focus.
    """

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field


class ActionError(ValueError):
    """A button was pressed that no longer makes sense (already awarded, and so on).

    Shown as a red banner on the screen the person came from.
    """
