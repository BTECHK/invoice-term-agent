"""Fixed-query offline MCP boundary, without a network, provider, or write fallback."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sys
import time
from collections import deque
from collections.abc import Awaitable, Collection, Mapping
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from httpx2 import TransportError
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from . import REPO_ROOT, miniyaml

READ_TOOLS = frozenset({
    "run_query", "get_data_catalog", "search_records", "get_kpis", "get_event_timeline",
})
DENIED_TOOLS = frozenset({
    "call_assistant_model", "trigger_erp_action", "start_workflow", "resume_workflow",
})

class ErpClientError(RuntimeError):
    """A protocol, transport, or structured-output failure."""

class QueryValidationError(ErpClientError):
    """A fixed query identifier or variable failed local validation."""

class ToolDeniedError(ErpClientError):
    """The caller requested a tool outside the five read tools."""

class ThrottleExceeded(ErpClientError):
    """The bounded wait for a read attempt was exhausted."""

AuditSink = Callable[[str, Mapping[str, Any], dict[str, Any] | None, BaseException | None], None]

class ErpClient:
    """Validated async client for the offline mock ERP data service."""

    def __init__(
        self, transport: Any, *,
        config_path: Path = REPO_ROOT / "integration" / "agent-tools.yaml",
        timeout_seconds: float = 10,
        calls_per_minute: int = 400,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait_seconds: float = 120,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be positive and finite")
        if not isinstance(calls_per_minute, int) or not 0 < calls_per_minute < 1000:
            raise ValueError("calls_per_minute must be an integer from 1 to 999, below the local client ceiling of 1000")
        if not math.isfinite(max_wait_seconds) or max_wait_seconds < 0:
            raise ValueError("max_wait_seconds must be nonnegative and finite")
        self.transport = transport
        self.config_path = Path(config_path)
        self.timeout_seconds = float(timeout_seconds)
        self.calls_per_minute = int(calls_per_minute)
        self.clock, self.sleep = clock, sleep
        self.max_wait_seconds = float(max_wait_seconds)
        self._calls: deque[float] = deque()
        self._client: Client | None = None
        self._ctx: Any = None
        self._audit_sink: AuditSink | None = None
        self._variable_types: dict[str, Any] = {}
        self._queries = self._load_queries()

    @contextmanager
    def audit_to(self, sink: AuditSink) -> Iterator[None]:
        """Route each attempted tool call to a role-specific audit trail."""
        previous = self._audit_sink
        self._audit_sink = sink
        try:
            yield
        finally:
            self._audit_sink = previous

    def _emit_audit(
        self, name: str, arguments: Mapping[str, Any], output: dict[str, Any] | None,
        error: BaseException | None, attempt: int = 1,
    ) -> None:
        if self._audit_sink is not None:
            self._audit_sink(name, {**arguments, "attempt": attempt}, output, error)

    @classmethod
    def stdio(
        cls, *,
        root: Path = REPO_ROOT,
        fixtures_dir: Path | None = None,
        timeout_seconds: float = 10,
        calls_per_minute: int = 400,
        max_wait_seconds: float = 120,
    ) -> "ErpClient":
        """Create a local stdio client with an explicit, credential-free environment."""
        root = Path(root).resolve()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(root / "src"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if fixtures_dir is not None:
            env["MOCK_ERP_FIXTURES"] = str(Path(fixtures_dir).resolve())
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(root / "mock_erp" / "server.py")],
            cwd=str(root),
            env=env,
        )
        return cls(
            params,
            config_path=root / "integration" / "agent-tools.yaml",
            timeout_seconds=timeout_seconds,
            calls_per_minute=calls_per_minute,
            max_wait_seconds=max_wait_seconds,
        )

    def _load_queries(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.is_file():
            raise QueryValidationError(f"tool contract not found: {self.config_path}")
        config = miniyaml.load(self.config_path.read_text(encoding="utf-8"))
        self._variable_types = config.get("variable_types") or {}
        queries: dict[str, dict[str, Any]] = {}
        for item in config.get("queries") or []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise QueryValidationError("tool contract has a malformed query")
            spec = dict(item)
            spec["variable_types"] = self._variable_types
            queries[spec["id"]] = spec
        if not queries:
            raise QueryValidationError("tool contract declares no queries")
        return queries

    async def __aenter__(self) -> "ErpClient":
        self._ctx = Client(self.transport)
        try:
            self._client = await asyncio.wait_for(self._ctx.__aenter__(), self.timeout_seconds)
            listing = await asyncio.wait_for(self._client.list_tools(), self.timeout_seconds)
        except Exception as exc:
            self._client = None
            raise ErpClientError(f"MCP discovery failed: {exc}") from exc
        names = {tool.name for tool in listing.tools}
        missing = (READ_TOOLS | DENIED_TOOLS) - names
        if missing:
            await self.__aexit__(None, None, None)
            raise ErpClientError(f"MCP discovery is missing tools: {sorted(missing)}")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        ctx, self._ctx, self._client = self._ctx, None, None
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.__aexit__(exc_type, exc, tb), self.timeout_seconds)
            except Exception as close_exc:
                if exc is None:
                    raise ErpClientError(f"MCP teardown failed: {close_exc}") from close_exc

    async def _wait(self, seconds: float, deadline: float, reason: str) -> None:
        if seconds <= 0:
            return
        if seconds > deadline - self.clock():
            raise ThrottleExceeded("read attempt exceeded its maximum wait")
        logging.getLogger(__name__).info("ERP read waiting %.3f seconds (%s)", seconds, reason)
        await self.sleep(seconds)
        if self.clock() > deadline:
            raise ThrottleExceeded("read attempt exceeded its maximum wait")

    async def _throttle(self, deadline: float) -> None:
        while True:
            now = self.clock()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) < self.calls_per_minute:
                self._calls.append(now)
                return
            await self._wait(60 - (now - self._calls[0]), deadline, "client window")

    @staticmethod
    def _rate_limit(source: Any) -> float | None:
        """Recognize explicit rate errors, never guess from free-form error text."""
        body = getattr(source, "structured_content", None) or getattr(source, "data", None)
        body = body if isinstance(body, Mapping) else {}
        detail = body.get("error")
        if isinstance(detail, Mapping):
            body = {**body, **detail}
        response = getattr(source, "response", source)
        code = body.get("status_code", body.get("code", getattr(response, "status_code", getattr(source, "code", None))))
        codes = {"429", "rate_limit", "rate_limited", "rate_limit_exceeded", "too_many_requests"}
        if str(code).lower() not in codes and str(detail).lower() not in codes:
            return None
        headers = getattr(response, "headers", None) or body.get("headers", {})
        headers = {str(k).lower(): v for k, v in headers.items()} if isinstance(headers, Mapping) else {}
        raw = headers.get("retry-after", body.get("retry_after", 0))
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(str(raw)).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                return 0
        return max(0, delay) if math.isfinite(delay) else 0

    async def _call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None, *,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        call_arguments = dict(arguments or {})
        deadline, pause = self.clock() + self.max_wait_seconds, 0.0
        reason = "transient backoff"
        for attempt in range(1, 4):
            retry_after = None
            try:
                if name not in READ_TOOLS:
                    raise ToolDeniedError(f"tool {name!r} is not one of the five allowed read tools")
                if self._client is None:
                    raise ErpClientError("client is not open")
                await self._wait(pause, deadline, reason)
                await self._throttle(deadline)
                result = await asyncio.wait_for(
                    self._client.call_tool(name, call_arguments), self.timeout_seconds
                )
                if result.is_error:
                    retry_after = self._rate_limit(result)
                    raise ErpClientError(f"tool {name} returned an error")
                body = result.structured_content
                if not isinstance(body, dict):
                    raise ErpClientError(f"tool {name} returned no structured object")
                if validate is not None:
                    validate(body)
            except Exception as exc:
                if retry_after is None:
                    retry_after = self._rate_limit(exc)
                transient = isinstance(exc, (TimeoutError, ConnectionError, EOFError,
                    TransportError, BrokenResourceError, ClosedResourceError, EndOfStream))
                transient |= isinstance(exc, MCPError) and exc.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT)
                error = exc if isinstance(exc, ErpClientError) else ErpClientError(
                    f"tool {name} failed ({type(exc).__name__})")
                self._emit_audit(name, call_arguments, None, error, attempt)
                if isinstance(exc, (ToolDeniedError, QueryValidationError, ThrottleExceeded)) or (
                    not transient and retry_after is None
                ) or attempt == 3:
                    if error is exc:
                        raise
                    raise error from exc
                backoff = (0.5, 1.0)[attempt - 1]
                pause = max(backoff, retry_after or 0)
                reason = "server Retry-After" if retry_after is not None and retry_after >= backoff else "transient backoff"
                continue
            self._emit_audit(name, call_arguments, body, None, attempt)
            return body
        raise AssertionError("read attempt loop exhausted")

    def _refuse(self, tool: str, arguments: Mapping[str, Any], message: str) -> QueryValidationError:
        error = QueryValidationError(message)
        self._emit_audit(tool, arguments, None, error)
        return error

    def _validated(self, tool: str, arguments: Mapping[str, tuple[str, Any]]) -> dict[str, str]:
        """Check each argument against its declared variable type before anything crosses MCP."""
        checked: dict[str, str] = {}
        for name, (type_name, value) in arguments.items():
            pattern = (self._variable_types.get(type_name) or {}).get("pattern")
            if not isinstance(value, str) or not isinstance(pattern, str) or re.fullmatch(pattern, value) is None:
                raise self._refuse(tool, {name: value}, f"{tool} argument {name} fails its declared pattern")
            checked[name] = value
        return checked

    async def get_data_catalog(self) -> dict[str, Any]:
        return await self._call_tool("get_data_catalog", {})

    async def search_records(self, query: str, limit: int = 3) -> dict[str, Any]:
        checked = self._validated("search_records", {"query": ("search_text", query)})
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
            raise self._refuse("search_records", {"query": query, "limit": limit},
                               "search_records limit must be an integer from 1 to 3")
        return await self._call_tool("search_records", {**checked, "limit": limit})

    async def get_kpis(self, topic: str) -> dict[str, Any]:
        return await self._call_tool("get_kpis", self._validated("get_kpis", {"topic": ("kpi_topic", topic)}))

    async def get_event_timeline(self, po_reference: str) -> dict[str, Any]:
        checked = self._validated("get_event_timeline", {"po_reference": ("po_reference", po_reference)})
        return await self._call_tool("get_event_timeline", checked)

    def validate_query(
        self, query_id: str, variables: Mapping[str, Any], *,
        allowed_queries: Collection[str] | None = None,
    ) -> dict[str, str]:
        """Validate the fixed query and declared variables before crossing MCP."""
        if allowed_queries is not None and query_id not in set(allowed_queries):
            raise QueryValidationError(f"query {query_id!r} is outside the active skill")
        spec = self._queries.get(query_id)
        if spec is None:
            raise QueryValidationError(f"query {query_id!r} is not declared")
        declared = list(spec.get("variables") or [])
        if set(variables) != set(declared):
            raise QueryValidationError(
                f"query {query_id} takes exactly {declared}, got {sorted(variables)}"
            )
        checked: dict[str, str] = {}
        for name in declared:
            value = str(variables[name])
            pattern = ((spec.get("variable_types") or {}).get(name) or {}).get("pattern")
            if not isinstance(pattern, str) or re.fullmatch(pattern, value) is None:
                raise QueryValidationError(f"variable {name} fails its declared pattern")
            checked[name] = value
        return checked

    async def run_query(
        self, query_id: str, variables: Mapping[str, Any], *,
        allowed_queries: Collection[str] | None = None,
    ) -> dict[str, Any]:
        raw_arguments = {"query": query_id, "variables": dict(variables)}
        try:
            checked = self.validate_query(query_id, variables, allowed_queries=allowed_queries)
        except QueryValidationError as exc:
            self._emit_audit("run_query", raw_arguments, None, exc)
            raise
        call_arguments = {"query": query_id, "variables": checked}
        def validate_body(body: dict[str, Any]) -> None:
            spec = self._queries[query_id]
            columns = list(spec.get("returns") or [])
            rows = body.get("rows")
            if body.get("query") != query_id or body.get("columns") != columns or not isinstance(rows, list):
                raise ErpClientError(f"query {query_id} returned a malformed envelope")
            if body.get("row_count") != len(rows):
                raise ErpClientError(f"query {query_id} returned an incorrect row count")
            if len(rows) > int(spec.get("max_rows", 0)):
                raise ErpClientError(f"query {query_id} exceeded its row cap")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(columns):
                    raise ErpClientError(f"query {query_id} returned undeclared columns")
        return await self._call_tool("run_query", call_arguments, validate=validate_body)
