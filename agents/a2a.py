"""The A2A v0.3 layer shared by both agents: JSON-RPC 2.0 over HTTP, `message/send` only.

Server side: `create_a2a_app` serves the agent card and turns each JSON-RPC request into a
call to the agent's `on_message(message) -> task`.
Client side: `A2AClient` discovers a peer from its card, sends messages, and writes one
timestamped log line per request and per response.
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "0.3.0"
CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/a2a"

# Standard JSON-RPC error codes, plus A2A's TaskNotFoundError.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001


class A2AError(Exception):
    """A failure to report as a JSON-RPC error, or one received from a peer."""

    def __init__(self, code: int, message: str):
        super().__init__(f"A2A error {code}: {message}")
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def text_part(text: str) -> dict:
    return {"kind": "text", "text": text}


def data_part(data: dict) -> dict:
    return {"kind": "data", "data": data}


def new_message(parts: list[dict], role: str = "user", task_id: str | None = None, context_id: str | None = None) -> dict:
    message = {"kind": "message", "role": role, "messageId": str(uuid.uuid4()), "parts": parts}
    if task_id:
        message["taskId"] = task_id
    if context_id:
        message["contextId"] = context_id
    return message


def new_task(state: str, text: str, task_id: str | None = None, context_id: str | None = None,
             artifacts: list[dict] | None = None, metadata: dict | None = None) -> dict:
    """Build a Task. `text` becomes the agent's message in the task status."""
    task_id = task_id or f"task-{uuid.uuid4()}"
    context_id = context_id or f"ctx-{uuid.uuid4()}"
    task = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": state,
            "message": new_message([text_part(text)], role="agent", task_id=task_id, context_id=context_id),
            "timestamp": now_iso(),
        },
        "artifacts": artifacts or [],
    }
    if metadata:
        task["metadata"] = metadata
    return task


def find_data(message_or_artifact: dict, key: str) -> dict | None:
    """Return the first data part containing `key`."""
    for part in message_or_artifact.get("parts", []):
        if part.get("kind") == "data" and key in part.get("data", {}):
            return part["data"]
    return None


def task_text(task: dict) -> str:
    parts = task.get("status", {}).get("message", {}).get("parts", [])
    return " ".join(p["text"] for p in parts if p.get("kind") == "text")


def build_agent_card(name: str, description: str, url: str, skills: list[dict]) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
    }


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def create_a2a_app(card: dict, on_message: Callable[[dict], dict]) -> FastAPI:
    """Wrap an agent's message handler in an HTTP server. The agent itself knows nothing of HTTP."""
    app = FastAPI(title=card["name"])

    @app.get(CARD_PATH)
    def get_card() -> dict:
        return card

    @app.post(RPC_PATH)
    async def rpc(request: Request) -> dict:
        try:
            body = json.loads(await request.body())
        except ValueError:
            return _error(None, PARSE_ERROR, "Request body is not valid JSON")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "method" not in body:
            return _error(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 request")

        request_id = body.get("id")
        if body["method"] != "message/send":
            return _error(request_id, METHOD_NOT_FOUND, f"Unsupported method: {body['method']}")
        message = (body.get("params") or {}).get("message")
        if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
            return _error(request_id, INVALID_PARAMS, "params.message with a parts list is required")

        try:
            # The handler can block on file work or an LLM, so keep it off the event loop.
            task = await run_in_threadpool(on_message, message)
        except A2AError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception:
            logger.exception("Unhandled error in message handler")
            return _error(request_id, INTERNAL_ERROR, "Internal error; see the agent log")
        return {"jsonrpc": "2.0", "id": request_id, "result": task}

    return app


class A2AClient:
    """Send `message/send` requests to a peer and log the conversation."""

    def __init__(self, sender: str, receiver: str, base_url: str, log_path: Path | None = None,
                 client: httpx.Client | None = None, timeout: float = 120.0):
        self._sender = sender
        self._receiver = receiver
        self._base_url = base_url.rstrip("/")
        self._log_path = log_path
        self._http = client or httpx.Client(timeout=timeout)

    def discover(self) -> dict:
        """Fetch the peer's card. Its `url` field is the endpoint to post to."""
        response = self._http.get(f"{self._base_url}{CARD_PATH}")
        response.raise_for_status()
        return response.json()

    def send(self, endpoint: str, message: dict) -> dict:
        """Post one message and return the resulting Task. Raises A2AError on any failure."""
        request = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send", "params": {"message": message}}
        self._log("request", self._sender, self._receiver, endpoint, request)
        try:
            response = self._http.post(endpoint, json=request)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._log("response", self._receiver, self._sender, endpoint, {"transport_error": str(exc)})
            raise A2AError(INTERNAL_ERROR, f"Could not reach {self._receiver}: {exc}") from exc
        self._log("response", self._receiver, self._sender, endpoint, body)

        if "error" in body:
            raise A2AError(body["error"]["code"], body["error"]["message"])
        return body["result"]

    def _log(self, direction: str, sender: str, receiver: str, url: str, payload: dict) -> None:
        if self._log_path is None:
            return
        entry = {"timestamp": now_iso(), "direction": direction, "from": sender, "to": receiver,
                 "url": url, "payload": payload}
        with self._log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
