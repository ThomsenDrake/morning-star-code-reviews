"""Exceptions raised by the ledger; handlers map them to HTTP-like status codes."""


class LedgerError(Exception):
    """Base class for every error the ledger raises on purpose."""

    status = 500


class InvalidRequest(LedgerError):
    """The caller sent something malformed or out of range."""

    status = 400


class Forbidden(LedgerError):
    """The caller is known but may not perform the action."""

    status = 403


class NotFound(LedgerError):
    """The requested object does not exist."""

    status = 404


class InsufficientFunds(LedgerError):
    """A withdrawal or transfer would take the balance below zero."""

    status = 409
