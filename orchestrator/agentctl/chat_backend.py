"""Multi-provider chat abstraction.

A `ChatBackend` is a single-conversation chat session that supports multi-turn
tool calling. Each provider (Google / Anthropic / OpenAI / Ollama) has its own
SDK and its own wire shape for tool calls and tool results; this module hides
those differences behind a unified interface so `agent.py`'s ReAct loop is
provider-agnostic.

Two modes select the default backend:
  - debug: gemini-2.5-flash-lite on Vertex (~5x cheaper than flash; ~$0.001/cell).
           Local-LLM debug mode is supported architecturally (set
           AGENTICBPF_AGENT_BACKEND=ollama with OLLAMA_BASE_URL pointing at a
           local llama-server / Ollama daemon) but no model in the 8B-32B
           sweet spot reliably drives B2 ReAct on consumer hardware - see
           setup.md sec 5b for the empirical table.
  - prod : Vertex Gemini 2.5 Flash by default, Anthropic Claude via opt-in.

Selected via env vars:
  AGENTICBPF_AGENT_MODE    = debug | prod                 (default: prod)
  AGENTICBPF_AGENT_BACKEND = ollama | google | anthropic | openai | studio
                             (default: depends on mode)

For backwards compat, AGENTICBPF_GENAI_BACKEND=vertex|studio is still honored
when AGENTICBPF_AGENT_BACKEND is not set.

Each backend lazily imports its SDK so that, e.g., debug-mode users without
the anthropic SDK installed don't pay an ImportError at startup.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# --- Unified message types (agent.py speaks only these) -----------------

@dataclass
class ToolCall:
    """A model's request to invoke a tool. `call_id` is opaque to the caller
    and threaded through to the matching ToolResult so multi-call turns work
    with providers that require strict pairing (OpenAI, Anthropic)."""
    name: str
    args: dict[str, Any]
    call_id: str


@dataclass
class ToolResult:
    """The local result of executing a tool, sent back to the model."""
    call_id: str
    name: str
    response: dict


@dataclass
class TurnResult:
    """A single model response: zero or more tool calls, plus optional prose."""
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


# --- Tool-spec extraction (each backend converts to its native shape) ----

def _tool_specs(tools: list[Callable]) -> list[dict]:
    """Build provider-neutral JSON-schema tool specs from Python callables.

    Each tool is a function with type hints + a docstring. Backends accept
    these as-is (Google) or transcode them (OpenAI/Anthropic). We use a
    minimal hand-rolled extractor rather than depend on jsonref/pydantic.
    """
    import inspect

    specs = []
    for fn in tools:
        sig = inspect.signature(fn)
        props: dict[str, dict] = {}
        required: list[str] = []
        for param_name, param in sig.parameters.items():
            ann = param.annotation
            json_type = "string"
            if ann is int: json_type = "integer"
            elif ann is bool: json_type = "boolean"
            elif ann is float: json_type = "number"
            elif ann is list or getattr(ann, "__origin__", None) is list:
                json_type = "array"
            elif ann is dict or getattr(ann, "__origin__", None) is dict:
                json_type = "object"
            prop: dict = {"type": json_type}
            if json_type == "array":
                # default to array-of-strings; tools that need richer schemas
                # can override by accepting `dict` and parsing internally.
                prop["items"] = {"type": "string"}
            props[param_name] = prop
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
        spec = {
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip().split("\n\n")[0][:1000],
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        }
        specs.append(spec)
    return specs


# --- The Protocol ----------------------------------------------------------

class ChatBackend(Protocol):
    """Unified chat session with multi-turn tool calling."""

    @property
    def model_id(self) -> str: ...

    def send(self, message: str | list[ToolResult]) -> TurnResult:
        """Send a user message (str) on the first turn, or a list of
        ToolResult on subsequent turns. Return the model's response."""
        ...


# --- GoogleBackend (single-shot, manual history) -------------------------

class GoogleBackend:
    """google-genai SDK; supports Vertex AI (default) and AI Studio.

    Uses single-shot ``client.models.generate_content`` with a manually-
    maintained ``self._history`` list rather than the stateful
    ``client.chats.create(...).send_message(...)`` path. Reason: the chat
    session's response parser is the site of an SDK deadlock when Vertex
    returns a Content.parts entry that contains both ``function_call`` and
    ``text`` fields ("non-text parts in the response: ['function_call']"
    warning observed in stderr). The deadlock is in client-side coroutine
    state, not network: even after the timeout we'd be left with a
    corrupted chat session that the next turn couldn't reuse. The
    single-shot path uses a different, simpler response parser that
    returns cleanly on the same response. Lab-observed ~4-in-4 deadlock
    rate on the STREAM x P10 conversation context with the chat-based
    code path; same context completed first try after this rewrite.
    """

    def __init__(self, model: str, system_instruction: str,
                 tools: list[Callable],
                 vertex: bool = True,
                 project: str | None = None,
                 location: str | None = None,
                 api_key: str | None = None):
        from google import genai
        from google.genai import types

        self._model = model
        self._types = types
        self._system_instruction = system_instruction

        if vertex:
            if not project:
                raise RuntimeError(
                    "GoogleBackend(vertex=True) requires `project`. Run "
                    "`gcloud auth application-default login` and set "
                    "GOOGLE_CLOUD_PROJECT.")
            self._client = genai.Client(vertexai=True, project=project,
                                        location=location or "us-central1")
        else:
            if not api_key:
                raise RuntimeError(
                    "GoogleBackend(vertex=False) requires `api_key` "
                    "(GEMINI_API_KEY).")
            self._client = genai.Client(api_key=api_key)

        # google-genai accepts plain Python callables as `tools=` and auto-
        # derives schemas. We pass the original callables, not _tool_specs(),
        # because the SDK's auto-schema is richer than our minimal one.
        self._config = types.GenerateContentConfig(
            tools=list(tools),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True,
            ),
            system_instruction=system_instruction,
            temperature=0.5,
        )
        # We own the conversation history. Each `send` appends both the
        # user/tool-result content and the model's reply, so the next call
        # sees the full transcript.
        self._history: list = []

    @property
    def model_id(self) -> str:
        return self._model

    def _user_content(self, message):
        """Wrap a str or list[ToolResult] in a `Content(role='user', ...)`."""
        T = self._types
        if isinstance(message, str):
            parts = [T.Part.from_text(text=message)]
        else:
            parts = [
                T.Part.from_function_response(name=tr.name, response=tr.response)
                for tr in message
            ]
        return T.Content(role="user", parts=parts)

    def _call_once(self, timeout_s: int):
        """Single SDK call wrapped in a SIGALRM-based wall-clock timeout."""
        import signal as _signal
        old = _signal.signal(_signal.SIGALRM,
                             lambda *_: (_ for _ in ()).throw(
                                 TimeoutError(
                                     f"Vertex call exceeded {timeout_s}s")))
        _signal.alarm(timeout_s)
        try:
            return self._client.models.generate_content(
                model=self._model,
                contents=self._history,
                config=self._config,
            )
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, old)

    def _call_with_retry(self, timeout_s: int):
        """Retry _call_once on 429 RESOURCE_EXHAUSTED / 503 UNAVAILABLE
        with exponential backoff. History is unchanged across retries
        because the SDK call is idempotent from our side."""
        import time as _time
        delays = [4, 16, 60, 120, 240]  # 7.4 min total budget
        for attempt in range(len(delays) + 1):
            try:
                return self._call_once(timeout_s)
            except Exception as e:
                msg = str(e)
                retryable = ("429" in msg or "RESOURCE_EXHAUSTED" in msg
                             or "503" in msg or "UNAVAILABLE" in msg)
                if attempt >= len(delays) or not retryable:
                    raise
                d = delays[attempt]
                print(f"[GoogleBackend] retryable error: {msg[:120]}; "
                      f"sleeping {d}s then retry "
                      f"({attempt + 1}/{len(delays)})", flush=True)
                _time.sleep(d)

    def send(self, message):
        T = self._types
        # Append the new user/tool-result turn to our history.
        self._history.append(self._user_content(message))

        # Hard wall-clock timeout PER SDK CALL. The single-shot path is *much*
        # less likely to hang than the chat-session path (the deadlock site
        # we replaced lives in chats.send_message), but we keep the timeout
        # as a defense-in-depth: a Vertex regional outage or genuine network
        # stall would otherwise wedge the whole sweep. Tunable via
        # AGENTICBPF_LLM_TIMEOUT_S (default 120 s).
        import os as _os
        timeout_s = int(_os.environ.get("AGENTICBPF_LLM_TIMEOUT_S", "120"))
        resp = self._call_with_retry(timeout_s)

        # Append the model's reply to history so the next turn sees it.
        # generate_content returns candidates[0].content; fall back gracefully
        # if either is missing (would happen on a safety-blocked response).
        try:
            assistant_content = resp.candidates[0].content
            if assistant_content is not None:
                self._history.append(assistant_content)
        except (AttributeError, IndexError):
            pass

        # Extract tool calls. resp.function_calls is a convenience accessor
        # that walks candidates[0].content.parts and pulls out function_call
        # parts; matches the chats.send_message API we replaced.
        fcalls = []
        for fc in (resp.function_calls or []):
            fcalls.append(ToolCall(
                name=fc.name,
                args=dict(fc.args or {}),
                # Gemini doesn't surface a unique call_id; synthesize one so
                # downstream code can pair Calls with Results. Gemini doesn't
                # use the id either, so any unique value is fine.
                call_id=fc.id if hasattr(fc, "id") and fc.id else f"google_{uuid.uuid4().hex[:8]}",
            ))
        return TurnResult(text=getattr(resp, "text", None) or None,
                          tool_calls=fcalls)


# --- AnthropicBackend ------------------------------------------------------

class AnthropicBackend:
    """anthropic SDK; manual messages list."""

    def __init__(self, model: str, system_instruction: str,
                 tools: list[Callable], api_key: str):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "AnthropicBackend requires the `anthropic` package. "
                "Run: pip install anthropic"
            ) from e

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._system = system_instruction
        self._tool_specs = [
            {
                "name": s["name"],
                "description": s["description"],
                "input_schema": s["parameters"],
            }
            for s in _tool_specs(tools)
        ]
        self._messages: list[dict] = []

    @property
    def model_id(self) -> str:
        return self._model

    def send(self, message):
        if isinstance(message, str):
            self._messages.append({"role": "user", "content": message})
        else:
            # tool_results go inside a single user message as content blocks
            self._messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.call_id,
                        "content": str(tr.response),
                    }
                    for tr in message
                ],
            })

        resp = self._client.messages.create(
            model=self._model,
            system=self._system,
            tools=self._tool_specs,
            messages=self._messages,
            max_tokens=4096,
            temperature=0.3,
        )

        # Append the assistant turn so future turns have context.
        self._messages.append({
            "role": "assistant",
            "content": [b.model_dump() for b in resp.content],
        })

        text_parts: list[str] = []
        tcalls: list[ToolCall] = []
        for block in resp.content:
            t = getattr(block, "type", None)
            if t == "text":
                text_parts.append(block.text)
            elif t == "tool_use":
                tcalls.append(ToolCall(
                    name=block.name,
                    args=dict(block.input or {}),
                    call_id=block.id,
                ))
        return TurnResult(text="".join(text_parts) or None, tool_calls=tcalls)


# --- OpenAICompatBackend (real OpenAI + Ollama via base_url) -------------

class OpenAICompatBackend:
    """openai SDK; works against OpenAI proper or any OpenAI-compatible
    endpoint (Ollama, vLLM, SGLang, OpenRouter, ...) by setting `base_url`.
    """

    def __init__(self, model: str, system_instruction: str,
                 tools: list[Callable],
                 api_key: str | None = None,
                 base_url: str | None = None):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAICompatBackend requires the `openai` package. "
                "Run: pip install openai"
            ) from e

        # Ollama doesn't require a real key, but the openai SDK insists on
        # one being non-empty.
        self._client = OpenAI(api_key=api_key or "ollama-local",
                              base_url=base_url)
        self._model = model
        self._system = system_instruction
        self._tool_specs = [
            {"type": "function", "function": s} for s in _tool_specs(tools)
        ]
        self._messages: list[dict] = [
            {"role": "system", "content": system_instruction},
        ]

    @property
    def model_id(self) -> str:
        return self._model

    def send(self, message):
        if isinstance(message, str):
            self._messages.append({"role": "user", "content": message})
        else:
            for tr in message:
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tr.call_id,
                    "name": tr.name,
                    "content": str(tr.response),
                })

        resp = self._client.chat.completions.create(
            model=self._model,
            messages=self._messages,
            tools=self._tool_specs,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=4096,
        )

        msg = resp.choices[0].message
        # Append the assistant turn so subsequent turns see it.
        self._messages.append(msg.model_dump(exclude_none=True))

        tcalls: list[ToolCall] = []
        import json as _json
        for tc in (msg.tool_calls or []):
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except _json.JSONDecodeError:
                args = {"_raw": tc.function.arguments or ""}
            tcalls.append(ToolCall(
                name=tc.function.name,
                args=args,
                call_id=tc.id,
            ))
        return TurnResult(text=msg.content or None, tool_calls=tcalls)


# --- Factory ---------------------------------------------------------------

DEFAULTS = {
    "debug": ("google", "gemini-2.5-flash-lite"),
    "prod":  ("google", "gemini-2.5-flash"),
}


def build_backend(mode: str | None,
                  backend: str | None,
                  model: str | None,
                  system_instruction: str,
                  tools: list[Callable]) -> ChatBackend:
    """Pick a backend based on (mode, backend, model), each optional."""
    mode = (mode or "prod").lower()
    if mode not in DEFAULTS:
        raise RuntimeError(
            f"unknown AGENTICBPF_AGENT_MODE={mode}; "
            f"valid: 'debug' or 'prod'"
        )

    # Honor the legacy AGENTICBPF_GENAI_BACKEND if AGENTICBPF_AGENT_BACKEND
    # is unset.
    if not backend:
        legacy = os.environ.get("AGENTICBPF_GENAI_BACKEND", "").lower()
        if legacy == "vertex":
            backend = "google"
        elif legacy == "studio":
            backend = "studio"

    default_backend, default_model = DEFAULTS[mode]
    backend = (backend or default_backend).lower()
    model = model or default_model

    if backend == "google":
        return GoogleBackend(
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            vertex=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    if backend == "studio":
        return GoogleBackend(
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            vertex=False,
            api_key=os.environ.get("GEMINI_API_KEY"),
        )
    if backend == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "anthropic backend requires ANTHROPIC_API_KEY env var"
            )
        return AnthropicBackend(
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            api_key=api_key,
        )
    if backend == "ollama":
        return OpenAICompatBackend(
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            base_url=os.environ.get("OLLAMA_BASE_URL",
                                    "http://localhost:11434/v1"),
            api_key="ollama-local",
        )
    if backend == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "openai backend requires OPENAI_API_KEY env var"
            )
        return OpenAICompatBackend(
            model=model,
            system_instruction=system_instruction,
            tools=tools,
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL"),  # None = default
        )
    raise RuntimeError(
        f"unknown AGENTICBPF_AGENT_BACKEND={backend}; "
        f"valid: 'google' (default in prod), 'studio', 'anthropic', "
        f"'ollama' (default in debug), 'openai'"
    )
