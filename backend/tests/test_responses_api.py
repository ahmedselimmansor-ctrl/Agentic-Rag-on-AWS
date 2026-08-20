"""Responses-API path — message/tool translation and hosted web search.

The agent graph builds one Chat-Completions-shaped message list and both APIs
consume it, so these conversions are what keep the tool-call loop working when
native web search switches the request onto the Responses API.
"""

from __future__ import annotations

from app.config import settings
from app.services.llm import (
    Citation,
    _extract_citation,
    _ResponsesToolAccumulator,
    hosted_web_search_tool,
    to_responses_input,
    to_responses_tools,
)
from app.tools import registry


# ------------------------------------------------------ message conversion --
def test_plain_turns_convert():
    items = to_responses_input(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    )

    assert [i["role"] for i in items] == ["system", "user", "assistant"]
    # Inbound text is input_text; the model's own prior text is output_text.
    assert items[1]["content"][0]["type"] == "input_text"
    assert items[2]["content"][0]["type"] == "output_text"


def test_tool_call_round_trip_survives_conversion():
    """The assistant's tool request and the tool's result must both carry the
    same call_id, or the model cannot match them up."""
    items = to_responses_input(
        [
            {"role": "user", "content": "what is in my docs?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "search_documents", "arguments": '{"query":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "[1] a passage"},
        ]
    )

    call = next(i for i in items if i.get("type") == "function_call")
    output = next(i for i in items if i.get("type") == "function_call_output")

    assert call["call_id"] == "call_abc"
    assert call["name"] == "search_documents"
    assert call["arguments"] == '{"query":"x"}'
    assert output["call_id"] == "call_abc"
    assert output["output"] == "[1] a passage"


def test_preamble_before_a_tool_call_is_kept():
    items = to_responses_input(
        [
            {
                "role": "assistant",
                "content": "Let me look that up.",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "web_search", "arguments": "{}"}}
                ],
            }
        ]
    )

    assert items[0]["content"][0]["text"] == "Let me look that up."
    assert items[1]["type"] == "function_call"


def test_image_parts_convert_to_input_image():
    items = to_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
                    {"type": "text", "text": "what is this?"},
                ],
            }
        ]
    )

    assert [p["type"] for p in items[0]["content"]] == ["input_image", "input_text"]
    assert items[0]["content"][0]["image_url"] == "https://x.test/a.png"
    assert items[0]["content"][1]["text"] == "what is this?"


def test_null_content_without_tool_calls_is_dropped():
    assert to_responses_input([{"role": "assistant", "content": None}]) == []


# --------------------------------------------------------- tool conversion --
def test_function_schema_is_flattened():
    """Chat Completions nests under `function`; Responses expects it flat."""
    flat = to_responses_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "search_documents",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )

    assert flat == [
        {
            "type": "function",
            "name": "search_documents",
            "description": "search",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_already_flat_tools_pass_through():
    hosted = [{"type": "web_search"}]
    assert to_responses_tools(hosted) == hosted


def test_hosted_tool_type_is_configurable(monkeypatch):
    """Providers rename this between releases, so it must not be hard-coded."""
    monkeypatch.setattr(settings, "openai_web_search_tool", "web_search_preview")
    monkeypatch.setattr(settings, "openai_web_search_context_size", "")
    assert hosted_web_search_tool() == {"type": "web_search_preview"}

    monkeypatch.setattr(settings, "openai_web_search_context_size", "high")
    assert hosted_web_search_tool() == {
        "type": "web_search_preview",
        "search_context_size": "high",
    }


# ----------------------------------------------------- tool call streaming --
def test_arguments_accumulate_across_deltas():
    acc = _ResponsesToolAccumulator()
    acc.start("item_1", "call_1", "search_documents")
    acc.add_arguments("item_1", '{"query": "pg')
    acc.add_arguments("item_1", 'vector"}')

    calls = acc.finalize()
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].arguments == {"query": "pgvector"}


def test_done_event_overrides_partial_arguments():
    acc = _ResponsesToolAccumulator()
    acc.start("item_1", "call_1", "search_documents")
    acc.add_arguments("item_1", '{"query": "trunc')
    acc.complete("item_1", "call_1", "search_documents", '{"query": "complete"}')

    assert acc.finalize()[0].arguments == {"query": "complete"}


def test_malformed_arguments_degrade_to_empty_dict():
    acc = _ResponsesToolAccumulator()
    acc.start("i", "c", "web_search")
    acc.add_arguments("i", '{"query": unterminated')

    assert acc.finalize()[0].arguments == {}


def test_parallel_calls_stay_separate():
    acc = _ResponsesToolAccumulator()
    acc.start("i1", "c1", "search_documents")
    acc.add_arguments("i1", '{"query":"a"}')
    acc.start("i2", "c2", "web_search")
    acc.add_arguments("i2", '{"query":"b"}')

    names = sorted(c.name for c in acc.finalize())
    assert names == ["search_documents", "web_search"]


# ------------------------------------------------------------- citations ----
def test_url_citation_is_extracted():
    citation = _extract_citation(
        {"type": "url_citation", "url": "https://example.com/a", "title": "A page"}
    )
    assert citation == Citation(url="https://example.com/a", title="A page")


def test_non_url_annotations_are_ignored():
    assert _extract_citation({"type": "file_citation", "file_id": "f1"}) is None
    assert _extract_citation({"type": "url_citation"}) is None  # no url
    assert _extract_citation(None) is None


# ---------------------------------------------------- tool registration ----
def test_native_search_does_not_register_a_duplicate_function(monkeypatch):
    """The model runs its own search server-side. Registering our web_search
    function too would give it two ways to do one thing."""
    monkeypatch.setattr(settings, "web_search_provider", "openai")
    assert registry.uses_native_web_search() is True

    tools = registry.available_tools(web_enabled=True, has_documents=True)
    names = [t["function"]["name"] for t in tools]
    assert names == ["search_documents"]


def test_external_provider_still_registers_our_tools(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "tavily")
    assert registry.uses_native_web_search() is False

    tools = registry.available_tools(web_enabled=True, has_documents=True)
    names = sorted(t["function"]["name"] for t in tools)
    assert names == ["fetch_page", "search_documents", "web_search"]


def test_web_toggle_off_removes_search_entirely(monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "tavily")
    tools = registry.available_tools(web_enabled=False, has_documents=True)
    assert [t["function"]["name"] for t in tools] == ["search_documents"]
