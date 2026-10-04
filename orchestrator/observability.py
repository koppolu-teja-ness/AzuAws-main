"""LangSmith tracing/evaluation support for the migration graph.

Strictly opt-in via LANGSMITH_TRACING + LANGSMITH_API_KEY. Every public helper
here is a true no-op (no `langsmith` import, no `Client` construction, no
network call) unless both are set, so --dry-run and ordinary runs behave
identically whether or not this module is used. Every helper also swallows its
own errors (bad endpoint, SDK surprises, etc.) and degrades to a no-op instead
of raising, so tracing problems never break a migration run.

Redaction: redact() is the single place secret-shaped data gets scrubbed
before it can reach LangSmith. It is wired into the configured Client via
hide_inputs/hide_outputs/hide_metadata (see get_client()), so it applies to
every run this module creates or that LangGraph auto-traces while
`tracing_scope()` is active -- callers should not need to call redact()
themselves, but ad hoc span inputs/outputs are still passed through it too.
"""
from __future__ import annotations

import dataclasses
import functools
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SECRET_KEY_RE = re.compile(r"password|secret|token|key|connectionstring", re.IGNORECASE)
_AZURE_GUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_AWS_ACCOUNT_ID_RE = re.compile(r"\b\d{12}\b")
_REDACTED = "**REDACTED**"
_MASKED_ID = "**MASKED-ID**"

_registered_secret_values: set[str] = set()
_client: Any = None


def is_tracing_enabled() -> bool:
    """False unless LANGSMITH_TRACING is truthy AND LANGSMITH_API_KEY is set --
    both are required so a half-configured environment never attempts a call."""
    tracing_flag = os.environ.get("LANGSMITH_TRACING", "false").strip().lower() in ("true", "1", "yes")
    has_api_key = bool((os.environ.get("LANGSMITH_API_KEY") or "").strip())
    return tracing_flag and has_api_key


def register_secret_values(*values: str | None) -> None:
    """Feed exact secret-value strings (real Key Vault values from Agent 0,
    resolved NoEcho CFN parameter values from Agent 6) into the redaction
    registry, so redact() masks them by value even when the containing key
    name doesn't match a known secret-key pattern."""
    for value in values:
        if value:
            _registered_secret_values.add(str(value))


def redact(data: Any) -> Any:
    """Recursively redact a payload before it is traced: secret-shaped keys
    are fully masked, registered exact secret values are substring-replaced,
    and Azure subscription IDs / AWS account IDs are masked wherever found."""
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        data = dataclasses.asdict(data)
    if isinstance(data, dict):
        return {k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else redact(v)) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(v) for v in data]
    if isinstance(data, str):
        return _redact_string(data)
    return data


def _redact_string(text: str) -> str:
    for value in _registered_secret_values:
        if value and value in text:
            text = text.replace(value, _REDACTED)
    text = _AZURE_GUID_RE.sub(_MASKED_ID, text)
    text = _AWS_ACCOUNT_ID_RE.sub(_MASKED_ID, text)
    return text


def _hide(data: dict) -> dict:
    return redact(data)


def get_client():
    """Lazily construct (and cache) the configured LangSmith Client with
    redaction hooks wired in. Only ever called when tracing is enabled."""
    global _client
    if _client is None:
        from langsmith import Client

        _client = Client(
            api_key=os.environ.get("LANGSMITH_API_KEY") or None,
            api_url=os.environ.get("LANGSMITH_ENDPOINT") or None,
            hide_inputs=_hide,
            hide_outputs=_hide,
            hide_metadata=_hide,
        )
    return _client


@contextmanager
def tracing_scope(*, project_name: str, tags: list[str] | None = None) -> Iterator[None]:
    """Force every run created in this block (including LangGraph's automatic
    per-node runs) to use the redacting client from get_client(), regardless
    of ambient LANGSMITH_TRACING env state elsewhere. True no-op when tracing
    is disabled or anything about the LangSmith SDK fails to cooperate."""
    if not is_tracing_enabled():
        yield
        return
    try:
        from langchain_core.tracers.context import tracing_v2_enabled

        with tracing_v2_enabled(project_name=project_name, tags=tags, client=get_client()):
            yield
    except Exception:
        yield


class _NullRun:
    """No-op stand-in for a LangSmith run span when tracing is disabled."""

    def __init__(self) -> None:
        self.metadata: dict = {}
        self.id = None

    def end(self, **_kwargs: Any) -> None:
        return None


@contextmanager
def trace_span(
    name: str,
    run_type: str = "tool",
    *,
    inputs: dict | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> Iterator[Any]:
    """Ad hoc child span for a deterministic step (cfn-lint, parameter
    resolution, ...). Yields a `_NullRun` (safe `.end()`/`.metadata` no-ops)
    when tracing is off or anything about the SDK fails."""
    if not is_tracing_enabled():
        yield _NullRun()
        return
    try:
        from langsmith.run_helpers import trace as ls_trace

        with ls_trace(name, run_type=run_type, inputs=inputs or {}, tags=tags, metadata=metadata, client=get_client()) as run:
            yield run
    except Exception:
        yield _NullRun()


def traceable(*decorator_args: Any, **decorator_kwargs: Any) -> Callable[[Callable], Callable]:
    """Drop-in replacement for `langsmith.traceable(...)` that is a true
    no-op (no import, no client, no network call) unless tracing is enabled.
    Must be called with parens, e.g. `@traceable(run_type="llm")`."""

    def decorator(func: Callable) -> Callable:
        if not is_tracing_enabled():
            return _strip_langsmith_extra(func)
        try:
            from langsmith import traceable as ls_traceable

            return ls_traceable(*decorator_args, client=get_client(), **decorator_kwargs)(func)
        except Exception:
            return _strip_langsmith_extra(func)

    return decorator


def _strip_langsmith_extra(func: Callable) -> Callable:
    """Wrap `func` so callers can always pass `langsmith_extra=...` (as the
    real `@traceable` allows) even when this is a plain passthrough."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        kwargs.pop("langsmith_extra", None)
        return func(*args, **kwargs)

    return wrapper


def annotate_current_run(*, tags: list[str] | None = None, metadata: dict | None = None) -> None:
    """Add tags/metadata to the currently-executing traced run. No-op if
    tracing is off, there is no active run, or anything goes wrong."""
    if not is_tracing_enabled():
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        if run_tree is None:
            return
        if tags:
            run_tree.tags = list(dict.fromkeys((run_tree.tags or []) + tags))
        if metadata:
            run_tree.metadata.update(redact(metadata))
    except Exception:
        pass


def attach_feedback(
    key: str,
    *,
    score: float | int | bool | None = None,
    value: float | int | bool | str | None = None,
    comment: str | None = None,
    on_trace_root: bool = False,
) -> None:
    """Attach feedback to the current run (or the whole trace root when
    `on_trace_root=True`). Never raises -- a feedback-submission problem must
    never break the migration pipeline."""
    if not is_tracing_enabled():
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        if run_tree is None:
            return
        target_id = run_tree.trace_id if on_trace_root else run_tree.id
        get_client().create_feedback(target_id, key, score=score, value=value, comment=comment)
    except Exception:
        pass


def get_trace_url() -> str | None:
    """Best-effort shareable URL for the current run's trace. Never raises;
    returns None when tracing is off or the URL can't be resolved."""
    if not is_tracing_enabled():
        return None
    try:
        import warnings

        from langsmith.run_helpers import get_current_run_tree

        run_tree = get_current_run_tree()
        if run_tree is None:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return get_client().get_run_url(run=run_tree)
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=str(_REPO_ROOT),
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_run_config(
    *,
    run_id: str,
    source_file: str | None,
    input_mode: str,
    config: Any,
    prompt_version: str = "v1",
) -> dict:
    """LangGraph run config (run_name/tags/metadata) to merge into
    graph.invoke/stream's `config=`. Returns `{}` when tracing is disabled --
    LangGraph treats that as "nothing extra configured"."""
    if not is_tracing_enabled():
        return {}
    metadata = {
        "migration_run_id": run_id,
        "source_file": source_file,
        "input_mode": input_mode,
        "aws_region": config.aws_region,
        "bedrock_model_id": config.bedrock_model_id,
        "rag_enabled": config.rag_enabled,
        "rag_top_k": config.rag_top_k,
        "max_fix_attempts": config.max_fix_attempts,
        "git_sha": _git_sha(),
        "prompt_version": prompt_version,
    }
    return {
        "run_name": f"migration-{run_id}",
        "tags": ["bicep-to-cfn-migration", f"run:{run_id}"],
        "metadata": metadata,
    }
