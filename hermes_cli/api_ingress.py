"""Owner-bound OpenAI-compatible ingress for the authenticated Control Plane."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import hashlib
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.redact import redact_sensitive_text
from hermes_cli.dashboard_auth.authority import AuthorityStore, MachineCredential
from hermes_cli.dashboard_auth.owner_context import owner_context_from_registry
from hermes_cli.dashboard_auth.token_auth import register_token_route
from hermes_cli.owner_worker.gateway_client import OwnerWorkerGatewayClient
from hermes_cli.owner_worker.tokens import CONNECTION_PURPOSE_API_INGRESS

router = APIRouter()

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
API_INGRESS_SCOPE = "openai.chat.completions"
MAX_REQUEST_BYTES = 10_000_000
MAX_NORMALIZED_TEXT_LENGTH = 65_536
MAX_CONTENT_LIST_SIZE = 1_000
TURN_TIMEOUT_SECONDS = 900.0
SSE_KEEPALIVE_SECONDS = 30.0
MAX_IDEMPOTENCY_KEY_LENGTH = 255
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~:+/-]+$")

_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})

register_token_route(CHAT_COMPLETIONS_PATH)


@dataclass(frozen=True)
class ImageAttachment:
    content_base64: str
    filename: str


@dataclass(frozen=True)
class ChatRequest:
    history: tuple[dict[str, str], ...]
    prompt: str
    attachments: tuple[ImageAttachment, ...]
    model: str | None
    stream: bool


def _openai_error(
    message: str,
    *,
    err_type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": err_type}
    if code is not None:
        error["code"] = code
    if param is not None:
        error["param"] = param
    return {"error": error}


def _error_response(
    message: str,
    *,
    status_code: int,
    err_type: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        _openai_error(message, err_type=err_type, code=code, param=param),
        status_code=status_code,
        headers=headers,
    )


def _coerce_request_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_text(content: Any, *, depth: int = 0) -> str:
    if depth > 10 or content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH]
    if isinstance(content, list):
        parts: list[str] = []
        total = 0
        for item in content[:MAX_CONTENT_LIST_SIZE]:
            text = ""
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict) and str(item.get("type") or "").lower() in _TEXT_PART_TYPES:
                text = str(item.get("text") or "")
            elif isinstance(item, list):
                text = _normalize_text(item, depth=depth + 1)
            if text:
                remaining = MAX_NORMALIZED_TEXT_LENGTH - total
                if remaining <= 0:
                    break
                text = text[:remaining]
                parts.append(text)
                total += len(text)
        return "\n".join(parts)[:MAX_NORMALIZED_TEXT_LENGTH]
    return str(content)[:MAX_NORMALIZED_TEXT_LENGTH]


def _decode_image_data_url(value: str) -> ImageAttachment:
    header, separator, payload = value.partition(",")
    if not separator or not header.lower().startswith("data:image/") or ";base64" not in header.lower():
        raise ValueError("unsupported_content_type:Only base64 image data URLs are supported.")
    mime = header[5:].split(";", 1)[0].strip().lower()
    try:
        decoded = base64.b64decode("".join(payload.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_image_url:Image data URL is not valid base64.") from exc
    if not decoded:
        raise ValueError("invalid_image_url:Image data URL is empty.")
    if len(decoded) > 25 * 1024 * 1024:
        raise ValueError("invalid_image_url:Image exceeds the 25 MB upload limit.")
    extension = mimetypes.guess_extension(mime) or ".png"
    if extension == ".jpe":
        extension = ".jpg"
    return ImageAttachment(content_base64=base64.b64encode(decoded).decode("ascii"), filename=f"upload{extension}")


def _normalize_user_content(content: Any) -> tuple[str, tuple[ImageAttachment, ...]]:
    if not isinstance(content, list):
        return _normalize_text(content), ()

    text_parts: list[str] = []
    attachments: list[ImageAttachment] = []
    total = 0
    for part in content[:MAX_CONTENT_LIST_SIZE]:
        if isinstance(part, str):
            text = part
        elif not isinstance(part, dict):
            continue
        else:
            part_type = str(part.get("type") or "").strip().lower()
            if part_type in _TEXT_PART_TYPES:
                text = str(part.get("text") or "")
            elif part_type in _IMAGE_PART_TYPES:
                image_ref = part.get("image_url")
                if isinstance(image_ref, dict):
                    image_ref = image_ref.get("url")
                if not isinstance(image_ref, str) or not image_ref.strip():
                    raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
                image_ref = image_ref.strip()
                if not image_ref.lower().startswith("data:image/"):
                    raise ValueError(
                        "invalid_image_url:Owner-bound API ingress accepts inline data:image URLs only."
                    )
                attachments.append(_decode_image_data_url(image_ref))
                continue
            elif part_type in _FILE_PART_TYPES:
                raise ValueError(
                    "unsupported_content_type:Uploaded files and document inputs are not supported."
                )
            else:
                raise ValueError(
                    f"unsupported_content_type:Unsupported content part type {part.get('type')!r}."
                )
        if text:
            remaining = MAX_NORMALIZED_TEXT_LENGTH - total
            if remaining <= 0:
                continue
            text = text[:remaining]
            text_parts.append(text)
            total += len(text)
    return "\n".join(text_parts), tuple(attachments)


def _validation_error(exc: ValueError, *, param: str) -> JSONResponse:
    code, separator, message = str(exc).partition(":")
    if not separator:
        code, message = "invalid_content_part", str(exc)
    return _error_response(message, status_code=400, code=code, param=param)


def _parse_chat_request(body: Any) -> ChatRequest | JSONResponse:
    if not isinstance(body, dict):
        return _error_response("JSON body must be an object", status_code=400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error_response("Missing or invalid 'messages' field", status_code=400)

    seed: list[dict[str, str]] = []
    current_prompt = ""
    current_attachments: tuple[ImageAttachment, ...] = ()
    last_user_index = -1
    normalized: list[tuple[str, str, tuple[ImageAttachment, ...]]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role == "system":
            content = _normalize_text(message.get("content"))
            if content.strip():
                normalized.append((role, content, ()))
            continue
        if role not in {"user", "assistant"}:
            continue
        try:
            if role == "user":
                content, attachments = _normalize_user_content(message.get("content"))
                if content.strip() or attachments:
                    last_user_index = len(normalized)
                    normalized.append((role, content, attachments))
            else:
                content = _normalize_text(message.get("content"))
                if content.strip():
                    normalized.append((role, content, ()))
        except ValueError as exc:
            return _validation_error(exc, param=f"messages[{index}].content")

    if last_user_index < 0:
        return _error_response("No user message found in messages", status_code=400)
    if any(role == "assistant" for role, _content, _attachments in normalized[last_user_index + 1 :]):
        return _error_response("The final conversational message must be from the user", status_code=400)

    for index, (role, content, attachments) in enumerate(normalized):
        if index == last_user_index:
            current_prompt = content
            current_attachments = attachments
            continue
        if attachments:
            return _error_response(
                "Images are supported only on the current user message",
                status_code=400,
                code="unsupported_content_type",
                param="messages",
            )
        if content.strip():
            seed.append({"role": role, "content": content})

    if not current_prompt.strip() and current_attachments:
        current_prompt = "Please analyze the attached image."

    raw_model = body.get("model")
    model = str(raw_model).strip() if isinstance(raw_model, str) and raw_model.strip() else None
    return ChatRequest(
        history=tuple(seed),
        prompt=current_prompt,
        attachments=current_attachments,
        model=model,
        stream=_coerce_request_bool(body.get("stream")),
    )


def _machine_credential(request: Request) -> MachineCredential | JSONResponse:
    principal = getattr(request.state, "token_principal", None)
    if principal is None:
        return _error_response("Unauthorized", status_code=401, err_type="authentication_error")
    if API_INGRESS_SCOPE not in tuple(principal.scopes or ()):
        return _error_response("Credential lacks the required scope", status_code=403, err_type="permission_error")
    store = getattr(request.app.state, "authority_store", None)
    if not isinstance(store, AuthorityStore):
        return _error_response("API ingress authority is unavailable", status_code=503, err_type="server_error")
    try:
        binding = store.resolve_machine_credential(
            provider=str(principal.provider or ""),
            principal=str(principal.principal or ""),
            required_scope=API_INGRESS_SCOPE,
        )
    except Exception:
        return _error_response("API ingress authority is unavailable", status_code=503, err_type="server_error")
    if binding is None:
        return _error_response("Credential binding is unavailable", status_code=403, err_type="permission_error")
    return binding


def _turn_idempotency_key(request: Request, credential: MachineCredential) -> str | JSONResponse:
    supplied = request.headers.get("idempotency-key", "").strip()
    if supplied:
        if len(supplied) > MAX_IDEMPOTENCY_KEY_LENGTH or not _IDEMPOTENCY_KEY_RE.fullmatch(supplied):
            return _error_response(
                "Idempotency-Key must contain 1-255 visible URL-safe characters",
                status_code=400,
                code="invalid_idempotency_key",
                param="Idempotency-Key",
            )
        material = supplied
    else:
        material = uuid.uuid4().hex
    digest = hashlib.sha256(
        f"{credential.credential_id}\x1f{material}".encode("utf-8")
    ).hexdigest()
    return f"api:{credential.credential_id}:{digest}"


def _completion_chunk(
    *, completion_id: str, created: int, model: str, delta: dict[str, Any], finish_reason: str | None = None
) -> bytes:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _run_turn(
    request: Request,
    chat: ChatRequest,
    credential: MachineCredential,
    *,
    idempotency_key: str,
):
    supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
    if supervisor is None or not getattr(request.app.state, "auth_required", False):
        raise RuntimeError("authenticated Owner Worker runtime is unavailable")
    owner = owner_context_from_registry(
        auth_provider=credential.auth_provider,
        tenant_id=credential.tenant_id,
        canonical_user_id=credential.canonical_user_id,
        expected_owner_key=credential.owner_key,
        global_home=getattr(supervisor, "global_home", None),
    )
    create_params: dict[str, Any] = {
        "source": "openai-api",
        "title": "OpenAI API conversation",
        "close_on_disconnect": False,
        "messages": list(chat.history),
        "stored_session_id": f"api_{idempotency_key.rsplit(':', 1)[-1]}",
    }
    if chat.model:
        create_params["model"] = chat.model

    client = OwnerWorkerGatewayClient(
        supervisor,
        owner,
        connection_purpose=CONNECTION_PURPOSE_API_INGRESS,
    )
    await client.connect()
    try:
        created = await client.call("session.create", create_params)
        session_id = str((created or {}).get("session_id") or "")
        if not session_id:
            raise RuntimeError("Owner Worker did not create a session")
        for attachment in chat.attachments:
            await client.call(
                "image.attach_bytes",
                {
                    "session_id": session_id,
                    "content_base64": attachment.content_base64,
                    "filename": attachment.filename,
                },
            )
        await client.call(
            "prompt.submit",
            {
                "session_id": session_id,
                "text": chat.prompt,
                "idempotency_key": idempotency_key,
            },
        )
        return client, session_id
    except BaseException:
        await client.close()
        raise


@router.post(CHAT_COMPLETIONS_PATH)
async def chat_completions(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return _error_response("Request body too large", status_code=413)
        except ValueError:
            pass
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        return _error_response("Request body too large", status_code=413)
    try:
        body = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response("Invalid JSON in request body", status_code=400)

    parsed = _parse_chat_request(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    credential = _machine_credential(request)
    if isinstance(credential, JSONResponse):
        return credential

    idempotency_key = _turn_idempotency_key(request, credential)
    if isinstance(idempotency_key, JSONResponse):
        return idempotency_key
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
    created = int(time.time())
    model = parsed.model or "hermes-agent"
    try:
        client, session_id = await _run_turn(
            request,
            parsed,
            credential,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        return _error_response(
            redact_sensitive_text(str(exc)) or "Owner Worker unavailable",
            status_code=503,
            err_type="server_error",
        )

    if not parsed.stream:
        try:
            event = await client.wait_for_event(
                "message.complete", session_id=session_id, timeout=TURN_TIMEOUT_SECONDS
            )
            payload = event.get("params") or {}
            status = str(payload.get("status") or "")
            text = str(payload.get("text") or "")
            if status != "complete":
                return _error_response(
                    redact_sensitive_text(text) or "Agent turn did not complete",
                    status_code=502,
                    err_type="server_error",
                )
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": int(usage.get("input_tokens") or 0),
                        "completion_tokens": int(usage.get("output_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                    },
                },
                headers={"X-Hermes-Session-Id": session_id},
            )
        except asyncio.TimeoutError:
            return _error_response("Agent turn timed out", status_code=504, err_type="server_error")
        except Exception as exc:
            return _error_response(
                redact_sensitive_text(str(exc)) or "Agent turn failed",
                status_code=502,
                err_type="server_error",
            )
        finally:
            await client.close()

    async def stream():
        try:
            yield _completion_chunk(
                completion_id=completion_id,
                created=created,
                model=model,
                delta={"role": "assistant", "content": ""},
            )
            while True:
                try:
                    message = await client.next_event(timeout=SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                method = message.get("method")
                params = message.get("params") or {}
                if str(params.get("session_id") or "") != session_id:
                    continue
                if method == "message.delta":
                    text = str(params.get("text") or "")
                    if text:
                        yield _completion_chunk(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta={"content": text},
                        )
                elif method == "message.complete":
                    status = str(params.get("status") or "")
                    if status != "complete":
                        error = _openai_error(
                            redact_sensitive_text(str(params.get("text") or ""))
                            or "Agent turn did not complete",
                            err_type="server_error",
                        )
                        yield f"data: {json.dumps(error)}\n\n".encode("utf-8")
                    else:
                        yield _completion_chunk(
                            completion_id=completion_id,
                            created=created,
                            model=model,
                            delta={},
                            finish_reason="stop",
                        )
                    yield b"data: [DONE]\n\n"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _openai_error(
                redact_sensitive_text(str(exc)) or "Agent stream failed",
                err_type="server_error",
            )
            yield f"data: {json.dumps(error)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        finally:
            await client.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
        },
    )
