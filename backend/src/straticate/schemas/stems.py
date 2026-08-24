"""The one definition of what a stem name may be.

A stem name is not free text. It is a **file name** under a job's output
directory (``{data_dir}/jobs/{job_id}/stems/{stem}.wav``) and a **path segment**
in ``GET /jobs/{job_id}/stems/{stem_name}``, so the same string has to be safe
on disk, safe in a URL, and identical on both sides of the API. The pattern is
the one ``models/schemas/model-manifest.schema.json`` already declares for
catalog stem lists.

This module exists because the constraint was previously enforced in exactly
one place — :class:`straticate.inference.SeparatorInfo` — and *not* at the
schema boundary, which produced two separate defects:

- a catalog entry with ``"stems": ["Vocals", "Instrumental"]`` (or a repeated
  name) validated fine, served ``GET /models`` and ``GET /separation-modes``
  fine, and only blew up as an unhandled ``ValueError`` — a ``500`` — on the
  first job created for that mode;
- a :class:`~straticate.schemas.jobs.Stem` could advertise a name that
  ``GET /stems/{name}`` then refused, producing a self-contradictory ``404``
  listing the very stem it said did not exist.

Both are the same missing constraint, so it is stated once, here, and imported
by ``schemas/`` and by :mod:`straticate.inference.base` alike. This module
imports nothing from the application, which is what lets both sides share it
without a cycle.
"""

import re
from typing import Annotated

from pydantic import Field

STEM_NAME_REGEX = r"^[a-z][a-z0-9_]*$"
"""Regular expression source for a valid stem name.

Lowercase ASCII, starting with a letter, then letters, digits and underscores.
It admits no separator, no dot and no space, which makes path traversal through
a stem name impossible by construction rather than by sanitizing.
"""

STEM_NAME_PATTERN = re.compile(STEM_NAME_REGEX)
""":data:`STEM_NAME_REGEX`, compiled — for code that matches rather than validates."""

StemName = Annotated[str, Field(pattern=STEM_NAME_REGEX)]
"""A ``str`` constrained to :data:`STEM_NAME_REGEX`.

Used as a field type so the constraint reaches the OpenAPI document (and from
there the generated frontend types) rather than living only in Python.
"""

__all__ = ["STEM_NAME_PATTERN", "STEM_NAME_REGEX", "StemName"]
