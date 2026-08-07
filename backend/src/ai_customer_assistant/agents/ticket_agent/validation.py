"""
Email validation for the ticket agent.

Uses the `email-validator` library (MIT-licensed, free, open-source:
https://github.com/JoshData/python-email-validator) rather than a
hand-rolled regex, since regex reliably mishandles edge cases (quoted
local parts, internationalized domains, etc.) that a maintained library
already covers correctly.

Deliberately syntax-only: check_deliverability is left at its default
of False, so this performs no DNS/MX lookups and makes no network calls.
Any valid-looking email format is accepted (not restricted to any
particular provider, e.g. Gmail).

Case sensitivity: the local part of the email (before the @) is
preserved exactly as submitted — this module does not lowercase it.
Per RFC 5321 the local part is technically case-sensitive, and per
project decision here two addresses differing only in local-part case
are treated as distinct. The domain part is lowercased by the
underlying library, since domains are not case-sensitive by definition.
"""

from __future__ import annotations

from email_validator import EmailNotValidError, validate_email as _validate_email_syntax


class InvalidEmailError(Exception):
    """Raised when a submitted email fails syntax validation."""


def validate_email(email: str) -> str:
    """
    Validate ``email`` for correct syntax and return its normalized form.

    Args:
        email: The raw email string submitted by the customer.

    Returns:
        The validated email, normalized by the underlying library
        (whitespace stripped, domain lowercased, local part preserved).

    Raises:
        InvalidEmailError: if ``email`` is not a syntactically valid
            email address. Wraps the library's own exception so callers
            of this module depend on one exception type, not on
            email_validator's internals directly.
    """
    try:
        result = _validate_email_syntax(email, check_deliverability=False)
    except EmailNotValidError as exc:
        raise InvalidEmailError(f"{email!r} is not a valid email address: {exc}") from exc

    return result.normalized
