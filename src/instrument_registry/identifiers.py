"""ISIN and LEI as their own types, with the ISO check-digit validation that
makes them more than strings.

`Isin` and `Lei` are `NewType`s over `str`: a consumer can still use one
anywhere a `str` goes, but this package's own code can't pass a ticker or
an LEI where an ISIN belongs without the type checker noticing. Public
functions still *accept* plain `str` (so consumers' type checks don't
break), and the ones that write a learned row validate it at runtime with
`parse_isin()`/`parse_lei()`. A typo'd or swapped identifier is refused
before it can reach a table that nothing can regenerate.

Every ISIN and LEI in the local cache passed these checks when they were
introduced (2026-11-26: 213 instruments, 153 entities, every learned row).
"""
from __future__ import annotations

import re
from typing import NewType

#: An ISO 6166 International Securities Identification Number.
Isin = NewType("Isin", str)

#: An ISO 17442 Legal Entity Identifier.
Lei = NewType("Lei", str)

_ISIN_SHAPE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_LEI_SHAPE = re.compile(r"[A-Z0-9]{18}[0-9]{2}")


def _digits(text: str) -> str:
    """Letters become their base-36 value (A=10 ... Z=35), as both standards
    specify before computing the check digits."""
    return "".join(str(int(char, 36)) for char in text)


def is_valid_isin(value: str) -> bool:
    """Shape (2-letter country, 9 alphanumerics, 1 digit) and the Luhn check
    digit over the base-36-expanded first 11 characters."""
    if not _ISIN_SHAPE.fullmatch(value):
        return False
    total = 0
    for position, char in enumerate(reversed(_digits(value[:-1]))):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10 == int(value[-1])


def is_valid_lei(value: str) -> bool:
    """Shape (18 alphanumerics, 2 digits) and ISO 7064 MOD 97-10."""
    return bool(_LEI_SHAPE.fullmatch(value)) and int(_digits(value)) % 97 == 1


def parse_isin(value: str) -> Isin:
    """`value` as an `Isin`, or ValueError naming it. No normalisation:
    lower case or surrounding whitespace is refused, not fixed, because a
    caller passing that has a bug worth seeing."""
    if not is_valid_isin(value):
        raise ValueError(f"not a valid ISIN (ISO 6166 shape and check digit): {value!r}")
    return Isin(value)


def parse_lei(value: str) -> Lei:
    """`value` as a `Lei`, or ValueError naming it. No normalisation."""
    if not is_valid_lei(value):
        raise ValueError(f"not a valid LEI (ISO 17442 shape and check digits): {value!r}")
    return Lei(value)
