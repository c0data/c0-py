"""Exceptions raised by the c0 package. Every one derives from ``C0Error``.

The ``_c0`` extension imports this module at load time and raises these
classes directly, so a C-level failure and a Python-level one are caught
the same way.
"""


class C0Error(Exception):
    """Base class of every error the c0 package raises."""


class C0SyntaxError(C0Error):
    """Malformed input. ``position`` is the byte offset the error was found at."""

    def __init__(self, message: str, position: int = 0):
        super().__init__(f"{message} at byte {position}")
        self.message = message
        self.position = position


class UnassignedCodeError(C0SyntaxError):
    """A control code outside text that the format does not assign."""


class UnexpectedEndError(C0SyntaxError):
    """Input ended inside text, inside a level, or after a DLE or BEL."""


class UnitTypeError(C0Error):
    """A unit read as the wrong kind: a level asked for as a value, a value
    as a level, a table as a list, a plain value as a reference."""


class NotFoundError(C0Error, LookupError):
    """No such group, key, field or blob."""


class BuildError(C0Error):
    """A builder call with a bad name (control byte or edge whitespace) or in
    a bad state (a level closed that was never opened, an empty note)."""
