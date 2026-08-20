"""Bind-parameter substitution in raw SQL.

These exist because of a real bug: `:qvec::vector` was sent to Postgres
*literally*, unsubstituted, and the resulting syntax error was swallowed by the
caller's `except Exception` — so dense retrieval silently degraded to
sparse-only and long-term memory recall silently returned nothing.

SQLAlchemy's `text()` bind-param regex ends with a `(?!:)` lookahead, so a
parameter immediately followed by a colon is not a parameter at all. Postgres's
`::` cast syntax collides with that exactly.

The tests are string-level on purpose: they need no database, so they run in CI
on every commit rather than only when someone remembers to run an integration
suite.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.services import memory, retrieval

# The construct that silently breaks: a bind param followed by '::'.
BROKEN = re.compile(r":\w+::")


def bind_names(sql: str) -> set[str]:
    """What SQLAlchemy will actually treat as a bind parameter."""
    return set(text(sql)._bindparams.keys())


# ------------------------------------------------- the underlying trap -----
def test_sqlalchemy_does_not_bind_a_param_followed_by_a_cast():
    """Pin the behaviour that caused the bug, so the reason these tests exist
    is documented in executable form."""
    assert bind_names("SELECT :v::vector") == set()
    assert bind_names("SELECT CAST(:v AS vector)") == {"v"}


def test_cast_form_is_what_reaches_postgres():
    compiled = str(text("SELECT CAST(:v AS vector) AS x"))
    assert ":v" in compiled  # still a parameter, not inlined


# --------------------------------------------------- the real queries ------
@pytest.mark.parametrize(
    ("name", "sql", "expected"),
    [
        ("DENSE_SQL", retrieval.DENSE_SQL, {"qvec", "k", "user_id", "conversation_id"}),
        ("SPARSE_SQL", retrieval.SPARSE_SQL, {"query", "k", "user_id", "conversation_id"}),
    ],
)
def test_retrieval_queries_bind_every_parameter(name, sql, expected):
    assert bind_names(sql) == expected, f"{name} lost a bind parameter"


def test_no_query_uses_the_broken_cast_form():
    """A single `:param::type` anywhere reintroduces the silent failure."""
    for name, sql in [
        ("DENSE_SQL", retrieval.DENSE_SQL),
        ("SPARSE_SQL", retrieval.SPARSE_SQL),
        ("_SCOPE_SQL", retrieval._SCOPE_SQL),
    ]:
        assert not BROKEN.search(sql), f"{name} uses ':param::type'; use CAST(:param AS type)"


def test_scope_clause_binds_the_conversation_filter():
    """This one is quiet when broken: an unsubstituted scope clause is a syntax
    error, but a *wrongly* scoped one would leak another user's chunks."""
    names = bind_names(retrieval._SCOPE_SQL)
    assert "user_id" in names
    assert "conversation_id" in names


def test_memory_module_has_no_broken_casts():
    """memory.py builds its vector SQL inline, so it needs the same check."""
    import inspect

    source = inspect.getsource(memory)
    offenders = BROKEN.findall(source)
    assert not offenders, f"memory.py uses {offenders}; use CAST(:param AS type)"


def test_vector_literal_is_well_formed():
    literal = retrieval._vector_literal([1.0, -0.5, 0.25])
    assert literal.startswith("[") and literal.endswith("]")
    assert literal == "[1,-0.5,0.25]"


def test_vector_literal_survives_scientific_notation():
    """%g can emit '1e-08'; pgvector accepts it, but a comma or space would
    corrupt the literal."""
    literal = retrieval._vector_literal([1e-8, 1.23456789])
    assert " " not in literal
    assert literal.count(",") == 1
