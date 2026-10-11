"""Customer-side HTTPS requests only. No Reins server code or provider credentials."""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4


class ReinsError(RuntimeError):
    """Base error for a Reins API call."""


class ReinsUnavailable(ReinsError):
    """The service did not acknowledge a request. Do not assume a paid call was free."""


class ReinsRejected(ReinsError):
    """The service rejected the submitted request."""


class ReinsAuthenticationError(ReinsRejected):
    """The worker credential was absent or not accepted."""


class AdmissionDenied(ReinsRejected):
    """The task or paid operation was not admitted."""


class SettlementUnknown(ReinsUnavailable):
    """A paid call ran, but its settlement was not acknowledged."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        super().__init__(
            f"Paid call {request_id} ran, but Reins did not acknowledge settlement. "
            "Reconcile this request before retrying."
        )


@dataclass(frozen=True)
class Charge:
    """Output and actual charge returned by the customer's existing provider call."""

    result: Any
    actual_cost: str | Decimal
    usage: dict[str, int] = field(default_factory=dict)
    success: bool = True
    validated: bool = False
    validation_name: str | None = None
    reused_from: str | None = None


class ReinsClient:
    """Synchronous worker API client; deliberately never retries paid-call requests.

    Use the customer-specific worker token in a backend process. Browser pairing
    tokens and operator tokens are not worker credentials.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        token_file: str | Path | None = None,
        ca_file: str | Path | None = None,
        timeout: float = 20,
    ):
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"))
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") not in ("", "/api", "/v1")
        ):
            raise ValueError("Use an HTTPS origin with /api or /v1, or HTTP loopback for tests")
        if (token is None) == (token_file is None):
            raise ValueError("Supply exactly one of token or token_file")
        secret = token if token is not None else Path(token_file).expanduser().read_text().strip()
        if not secret or len(secret) < 32 or "\n" in secret or "\r" in secret:
            raise ValueError("A valid worker token is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/") + ("/v1" if parsed.path.rstrip("/") == "" else "")
        self._token = secret
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        )

    def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send one request; caller owns retry and reconciliation decisions."""
        if (
            not endpoint
            or endpoint.startswith("/")
            or any(part in ("", ".", "..") for part in endpoint.split("/"))
        ):
            raise ValueError("Invalid API endpoint")
        payload = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.base_url + "/" + endpoint,
            data=payload,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get("error", "Request rejected")
            except (ValueError, AttributeError):
                message = "Request rejected"
            if exc.code in (401, 403):
                raise ReinsAuthenticationError(message) from None
            if 400 <= exc.code < 500:
                raise ReinsRejected(message) from None
            raise ReinsUnavailable("Reins returned a server error") from None
        except (OSError, ValueError) as exc:
            raise ReinsUnavailable("Reins did not acknowledge the request") from exc

    def decision(self, workflow_id: str, *, success_value: str | Decimal | None = None):
        """Get the pinned configuration and continue/escalate/stop decision."""
        body: dict[str, Any] = {"workflow_id": workflow_id}
        if success_value is not None:
            body["success_value"] = str(success_value)
        return self.post("console/runtime/decision", body)

    def reserve(
        self,
        *,
        task_id: str,
        request_id: str,
        model: str,
        max_cost: str | Decimal,
        estimated_cost: str | Decimal | None = None,
        category: str = "model",
        cache_key: str | None = None,
        read_only: bool = False,
        operation_inputs: Any | None = None,
        retry: bool = False,
    ):
        """Reserve an upper bound *before* the customer's paid operation."""
        if cache_key is not None and not read_only:
            raise ValueError("Result reuse requires a read-only operation")
        upper = str(max_cost)
        body = {
            "task_id": task_id,
            "request_id": request_id,
            "model": model,
            "category": category,
            "max_cost": upper,
            "estimated_cost": str(estimated_cost) if estimated_cost is not None else upper,
        }
        if cache_key is not None:
            body.update(cache_key=cache_key, read_only=True)
        if operation_inputs is not None:
            canonical = json.dumps(
                [model, category, operation_inputs], sort_keys=True, allow_nan=False
            )
            body["operation_key"] = hashlib.sha256(canonical.encode()).hexdigest()
        if retry:
            body["retry"] = True
        return self.post("reserve", body)

    def child_task(
        self,
        parent_task_id: str,
        *,
        budget: str | Decimal,
        task_id: str | None = None,
    ) -> str:
        """Create a sub-agent task under its parent's shared budget."""
        child_id = task_id or str(uuid4())
        result = self.post(
            "tasks",
            {"parent_task_id": parent_task_id, "task_id": child_id, "budget": str(budget)},
        )
        if result.get("decision") != "continue":
            raise AdmissionDenied(result.get("reason", "Child task not admitted"))
        return child_id

    def context_sample(
        self,
        task_id: str,
        *,
        revision: str,
        components: dict[str, int],
        provider_input_tokens: int | None = None,
        sample_id: str | None = None,
    ) -> dict[str, Any]:
        """Record estimated input-token components without sending prompt text."""
        allowed = {"system", "history", "retrieval", "tool_schema", "user", "unattributed"}
        if (
            not isinstance(components, dict)
            or set(components) - allowed
            or any(type(value) is not int or value < 0 for value in components.values())
        ):
            raise ValueError(
                "components must contain nonnegative token counts for known categories"
            )
        if provider_input_tokens is not None and (
            type(provider_input_tokens) is not int or provider_input_tokens < 0
        ):
            raise ValueError("provider_input_tokens must be a nonnegative integer")
        return self.post(
            "context/sample",
            {
                "task_id": task_id,
                "sample_id": sample_id or str(uuid4()),
                "revision": revision,
                "components": components,
                "provider_input_tokens": provider_input_tokens,
            },
        )

    def settle(
        self,
        *,
        request_id: str,
        actual_cost: str | Decimal,
        success: bool = True,
        result: Any = None,
        usage: dict[str, int] | None = None,
        validated: bool = False,
        validation_name: str | None = None,
    ):
        """Record actual spend. A failed result can still have a nonzero charge."""
        return self.post(
            "settle",
            {
                "request_id": request_id,
                "actual_cost": str(actual_cost),
                "success": success,
                "result": result,
                "usage": usage or {},
                "validated": validated,
                "validation_name": validation_name,
            },
        )

    def uncertain(self, request_id: str):
        """Mark a dispatched call whose provider charge is not known yet."""
        return self.post("uncertain", {"request_id": request_id})

    def status(self, workflow_id: str):
        return self.post("status", {"workflow_id": workflow_id})

    def pairing(self):
        """Read the customer account connected to this demo pairing code."""
        return self.post("worker-pair/me", {})

    def start_run(self, *, run_key: str | None = None):
        """Create one hosted run for the paired customer; reuse run_key after an uncertain reply."""
        return self.post("worker-pair/start", {"run_key": run_key or uuid4().hex})

    def progress(self, task_id: str, stage: str, completed: int, total: int):
        return self.post(
            "progress",
            {"task_id": task_id, "stage": stage, "completed": completed, "total": total},
        )

    def run_paid_call(
        self,
        workflow_id: str,
        execute: Callable[[dict[str, Any]], Charge],
        *,
        max_cost: str | Decimal | Callable[[dict[str, Any]], str | Decimal],
        task_id: str | None = None,
        request_id: str | None = None,
        reuse_inputs: Any | None = None,
        read_only: bool = False,
        validator: Callable[[Any], bool] | None = None,
        validation_name: str | None = None,
        operation_inputs: Any | None = None,
        retry: bool = False,
    ) -> Charge:
        """Apply the server's plan to one paid model call.

        ``execute(config)`` runs the customer's own provider client and returns
        its output, actual cost and usage. It is never called after a stop/hold.
        A dispatched call is never retried automatically.

        To reuse an exact result within this workflow, provide JSON-serializable
        ``reuse_inputs``, ``read_only=True``, a business ``validator`` and a
        versioned ``validation_name``. A changed input, model or validation
        version gets a different key. The server reuses only settled, validated
        results; this method validates a reused result again before returning it.
        """
        if reuse_inputs is not None:
            if not read_only or validator is None or not validation_name:
                raise ValueError("Reuse requires read_only=True, validator and validation_name")
        elif read_only:
            raise ValueError("Supply reuse_inputs for read-only result reuse")
        plan = self.decision(workflow_id)
        if plan.get("action") not in ("continue", "escalate"):
            raise AdmissionDenied(plan.get("reason", "Task stopped"))
        config = plan["configuration"].get("config", {})
        model = config.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("Pinned plan has no model; use reserve() for this operation")
        cache_key = None
        if reuse_inputs is not None:
            canonical = json.dumps(
                [model, "model", validation_name, reuse_inputs],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            cache_key = hashlib.sha256(canonical.encode()).hexdigest()
        rid = request_id or str(uuid4())
        admission = self.reserve(
            task_id=task_id or workflow_id,
            request_id=rid,
            model=model,
            max_cost=max_cost(config) if callable(max_cost) else max_cost,
            cache_key=cache_key,
            read_only=read_only,
            operation_inputs=operation_inputs,
            retry=retry,
        )
        if admission.get("decision") == "reuse":
            if cache_key is None:
                raise ReinsRejected("Unexpected result reuse without a cache key")
            result = admission.get("result")
            if not validator(result):
                raise ReinsRejected(
                    "Reused result failed validation; change the validation version before retrying"
                )
            return Charge(
                result=result,
                actual_cost="0",
                validated=True,
                validation_name=validation_name,
                reused_from=admission.get("original_request"),
            )
        if admission.get("decision") != "continue":
            raise AdmissionDenied(admission.get("reason", "Call not admitted"))
        try:
            charge = execute(config)
            if not isinstance(charge, Charge):
                raise TypeError("execute must return a Charge")
        except BaseException:
            try:
                self.uncertain(rid)
            except ReinsError:
                pass
            raise
        validation_error = None
        validated = charge.validated
        if validator is not None:
            try:
                validated = bool(validator(charge.result))
            except Exception as exc:
                validated = False
                validation_error = exc
        try:
            self.settle(
                request_id=rid,
                actual_cost=charge.actual_cost,
                success=charge.success,
                result=charge.result,
                usage=charge.usage,
                validated=validated,
                validation_name=validation_name or charge.validation_name,
            )
        except ReinsError as exc:
            raise SettlementUnknown(rid) from exc
        if validation_error is not None:
            raise validation_error
        return replace(
            charge,
            validated=validated,
            validation_name=validation_name or charge.validation_name,
        )

    def run_with_fallback(
        self,
        workflow_id: str,
        execute: Callable[[dict[str, Any]], Charge],
        *,
        max_cost_for_model: Callable[[str], str | Decimal],
        task_id: str | None = None,
        validation_name: str,
        validator: Callable[[Any], bool],
        operation_inputs: Any | None = None,
    ) -> Charge:
        """Try once, then follow one server-approved escalation after failed validation.

        Each attempt is charged separately. A denied, uncertain or failed
        settlement never triggers another provider call.
        """
        if not validation_name or not callable(validator):
            raise ValueError("A named business validator is required for fallback")
        first_model = None
        for attempt in range(2):
            if attempt:
                plan = self.decision(workflow_id)
                if plan.get("action") not in ("continue", "escalate"):
                    raise AdmissionDenied(plan.get("reason", "Task stopped"))
                model = plan["configuration"].get("config", {}).get("model")
                if not isinstance(model, str) or not model:
                    raise ValueError("Pinned plan has no model")
                if model == first_model:
                    raise AdmissionDenied("No different model was approved after failed validation")
            used_model = None

            def dispatch(current_config):
                nonlocal used_model
                used_model = current_config["model"]
                return execute(current_config)

            charge = self.run_paid_call(
                workflow_id,
                dispatch,
                max_cost=lambda current_config: max_cost_for_model(current_config["model"]),
                task_id=task_id,
                validator=validator,
                validation_name=validation_name,
                operation_inputs=operation_inputs,
                retry=bool(attempt),
            )
            if not attempt:
                first_model = used_model
            if charge.validated:
                return charge
        return charge
