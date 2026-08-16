"""Tool-call accumulation across streamed chunks."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.llm import _ToolCallAccumulator


def delta(index: int, *, id_=None, name=None, args=None):
    return SimpleNamespace(
        index=index,
        id=id_,
        function=SimpleNamespace(name=name, arguments=args),
    )


def test_arguments_split_across_chunks_are_reassembled():
    acc = _ToolCallAccumulator()
    acc.add([delta(0, id_="call_1", name="web_search")])
    acc.add([delta(0, args='{"query": "pg')])
    acc.add([delta(0, args='vector HNSW"}')])

    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "web_search"
    assert calls[0].arguments == {"query": "pgvector HNSW"}


def test_parallel_tool_calls_stay_separate():
    acc = _ToolCallAccumulator()
    acc.add([delta(0, id_="a", name="search_documents", args='{"query":"x"}')])
    acc.add([delta(1, id_="b", name="web_search", args='{"query":"y"}')])

    calls = acc.finalize()
    assert [c.name for c in calls] == ["search_documents", "web_search"]
    assert calls[0].arguments == {"query": "x"}
    assert calls[1].arguments == {"query": "y"}


def test_name_fragments_concatenate():
    acc = _ToolCallAccumulator()
    acc.add([delta(0, id_="a", name="web_")])
    acc.add([delta(0, name="search")])
    acc.add([delta(0, args="{}")])

    assert acc.finalize()[0].name == "web_search"


def test_malformed_arguments_degrade_to_empty_dict():
    acc = _ToolCallAccumulator()
    acc.add([delta(0, id_="a", name="web_search", args='{"query": "unterminated')])

    calls = acc.finalize()
    assert calls[0].arguments == {}, "a bad payload must not crash the turn"


def test_empty_accumulator_is_falsy():
    assert not _ToolCallAccumulator()
