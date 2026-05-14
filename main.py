from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_sock import Sock
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator
from werkzeug.utils import secure_filename


load_dotenv()


ALLOWED_ROLES = {
    "patient": "Patient",
    "doctor": "Doctor",
    "ambulance_service": "Ambulance Service",
    "hospital_service": "Hospital Service",
}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_HISTORY_MESSAGES = 24
MAX_SAVED_UPLOADS = 12

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
IMG_DIR = PROJECT_DIR / "img"
IMG_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# ── Persistent sessions: cookies live for 90 days and survive logout ──────────
app.permanent_session_lifetime = timedelta(days=90)

sock = Sock(app)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class DetectedItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "Unknown Item"
    amount: Optional[str] = None
    time: List[str] = Field(default_factory=list)
    duration: Optional[str] = None
    category: Optional[str] = None


class VisionAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    medicine: bool
    extracted_text: Optional[str] = None
    items: List[DetectedItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        payload = dict(value)
        if "items" in payload and payload["items"] is not None:
            return payload

        merged_items: list[dict[str, Any]] = []
        category_keys = ("items", "medicine_items", "food", "fruit", "drink")

        for category_key in category_keys:
            raw_items = payload.get(category_key)
            if not isinstance(raw_items, list):
                continue

            for raw_item in raw_items:
                if isinstance(raw_item, str):
                    item = {
                        "name": raw_item,
                        "category": None if category_key in ("items", "medicine_items") else category_key,
                    }
                elif isinstance(raw_item, dict):
                    item = dict(raw_item)
                    if not item.get("name"):
                        item["name"] = (
                            item.get("label")
                            or item.get("type")
                            or item.get("title")
                            or item.get("product")
                            or "Unknown Item"
                        )
                    if not item.get("category") and category_key not in ("items", "medicine_items"):
                        item["category"] = category_key
                else:
                    continue

                item.setdefault("time", [])
                merged_items.append(item)

        payload["items"] = merged_items
        payload.setdefault("medicine", bool(payload.get("medicine_items")) or False)
        return payload


# ---------------------------------------------------------------------------
# Global locks
# ---------------------------------------------------------------------------

_serial_lock = Lock()
_store_lock = Lock()
_vision_lock = Lock()
_gemini_key_lock = Lock()


# ---------------------------------------------------------------------------
# In-memory stores — ALL keyed by context_id (stable per user name)
# ---------------------------------------------------------------------------

def _initial_next_serial() -> int:
    highest = 0
    for file_path in IMG_DIR.iterdir():
        if not file_path.is_file():
            continue
        try:
            highest = max(highest, int(file_path.stem))
        except ValueError:
            continue
    return highest + 1


_next_serial = _initial_next_serial()

# context_id → list[BaseMessage]   (stable per user name, survives logout)
THREAD_HISTORY: dict[str, list[BaseMessage]] = defaultdict(list)

# context_id → dict of runtime metadata
THREAD_RUNTIME: dict[str, dict[str, Any]] = defaultdict(dict)

# context_id → list of upload dicts
THREAD_UPLOADS: dict[str, list[dict[str, Any]]] = defaultdict(list)

# serial_number → vision payload
VISION_CACHE: dict[int, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# LLM pools
# ---------------------------------------------------------------------------

_gemini_llm_pool: dict[str, Any] = {}
_gemini_key_index = 0
_hf_key_index = 0
_hf_key_lock = Lock()


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class ChatGraphState(BaseModel):
    message: str
    route_mode: str = "chat"
    image_serial: Optional[int] = None
    upload: Optional[dict[str, Any]] = None
    vision_payload: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_label() -> str:
    return datetime.now().strftime("%I:%M %p")


# ---------------------------------------------------------------------------
# Per-user stable context ID  (the core of the persistence feature)
# ---------------------------------------------------------------------------

def get_or_create_context_id(name: str) -> str:
    """
    Returns a stable context ID tied to the user's name.

    context_ids is a dict stored in the cookie:
        { "Alice": "abc123", "Bob": "xyz789", ... }

    Each name gets exactly one ID — minted on first login, reused forever.
    The dict is never wiped by logout, so history survives across sessions.
    """
    # Always mark the session as permanent so the cookie lasts 90 days
    session.permanent = True

    context_ids: dict[str, str] = session.get("context_ids") or {}
    if name in context_ids:
        return context_ids[name]

    # First time we see this name — mint a new permanent ID
    new_id = secrets.token_urlsafe(18)
    context_ids[name] = new_id
    session["context_ids"] = context_ids
    return new_id


def ensure_thread_id() -> str:
    """
    Returns the active thread_id for the current request.
    If a user is logged in, their stable context_id is used.
    Otherwise falls back to a temporary anonymous ID.
    """
    # Prefer the explicitly set thread_id (set on login)
    thread_id = session.get("thread_id")
    if thread_id:
        return thread_id

    # No logged-in user — use a temporary anonymous ID
    anon_id = session.get("anon_thread_id")
    if not anon_id:
        anon_id = secrets.token_urlsafe(18)
        session["anon_thread_id"] = anon_id
    return anon_id


# ---------------------------------------------------------------------------
# Thread-safe store accessors
# ---------------------------------------------------------------------------

def get_thread_history(thread_id: str) -> list[BaseMessage]:
    with _store_lock:
        return THREAD_HISTORY[thread_id]


def get_thread_runtime(thread_id: str) -> dict[str, Any]:
    with _store_lock:
        return THREAD_RUNTIME[thread_id]


def trim_history(history: list[BaseMessage]) -> None:
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[:-MAX_HISTORY_MESSAGES]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt(user: dict[str, str]) -> str:
    return f"""
You are Atlas, a fast, reliable healthcare support assistant for the role "{user['role_label']}".

Behavior rules:
- Keep continuity using prior conversation memory.
- If vision analysis appears in context, treat it as evidence and do not invent unreadable details.
- Explain medicines, schedules, food items, and image findings clearly.
- If the image analysis is uncertain, say exactly what is uncertain.
- Be concise, natural, and practical.
- For urgent symptoms, advise real medical help clearly.
- This assistant is supportive but not a replacement for a licensed clinician.
""".strip()


# ---------------------------------------------------------------------------
# Gemini key management
# ---------------------------------------------------------------------------

def get_gemini_api_keys() -> list[str]:
    multi_value = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GOOGLE_API_KEYS") or ""
    keys = [k.strip() for k in multi_value.split(",") if k.strip()]

    single_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if single_key:
        keys.append(single_key)

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def build_gemini_llm(api_key: str):
    llm = _gemini_llm_pool.get(api_key)
    if llm is None:
        llm = init_chat_model(
            "google_genai:gemini-2.5-flash",
            temperature=0,
            api_key=api_key,
        )
        _gemini_llm_pool[api_key] = llm
    return llm


def ordered_gemini_api_keys() -> list[str]:
    global _gemini_key_index
    keys = get_gemini_api_keys()
    if not keys:
        raise RuntimeError(
            "No Gemini API keys configured. Set GOOGLE_API_KEY or GEMINI_API_KEYS in .env."
        )

    with _gemini_key_lock:
        start_index = _gemini_key_index % len(keys)
        ordered = keys[start_index:] + keys[:start_index]
        _gemini_key_index = (_gemini_key_index + 1) % len(keys)
    return ordered


def is_gemini_api_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "api key", "permission", "quota", "rate limit", "resource exhausted",
        "429", "503", "502", "500", "deadline", "timeout",
        "temporarily unavailable", "service unavailable", "unavailable",
        "internal error", "auth", "authentication",
    )
    return any(marker in text for marker in retry_markers)


# ---------------------------------------------------------------------------
# Qwen / HuggingFace vision client — multi-key round-robin (mirrors Gemini)
# ---------------------------------------------------------------------------

def get_hf_api_keys() -> list[str]:
    """
    Reads HF tokens from the environment.
    Supports:
      HF_TOKENS=tok1,tok2,tok3   (comma-separated, multiple keys)
      HF_TOKEN=tok1              (single key, legacy)
    Both can coexist; duplicates are removed.
    """
    multi_value = os.environ.get("HF_TOKENS", "")
    keys = [k.strip() for k in multi_value.split(",") if k.strip()]

    single_key = os.environ.get("HF_TOKEN", "").strip()
    if single_key:
        keys.append(single_key)

    deduped: list[str] = []
    seen: set[str] = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def ordered_hf_api_keys() -> list[str]:
    """Returns HF keys in round-robin order, advancing the index each call."""
    global _hf_key_index
    keys = get_hf_api_keys()
    if not keys:
        raise RuntimeError(
            "No HuggingFace tokens configured. Set HF_TOKEN or HF_TOKENS in .env."
        )
    with _hf_key_lock:
        start_index = _hf_key_index % len(keys)
        ordered = keys[start_index:] + keys[:start_index]
        _hf_key_index = (_hf_key_index + 1) % len(keys)
    return ordered


def is_hf_api_error(exc: Exception) -> bool:
    text = str(exc).lower()
    retry_markers = (
        "api key", "token", "permission", "quota", "rate limit",
        "429", "503", "502", "500", "deadline", "timeout",
        "temporarily unavailable", "service unavailable", "unavailable",
        "internal error", "auth", "authentication", "unauthorized",
    )
    return any(marker in text for marker in retry_markers)


def build_qwen_client(api_key: str) -> OpenAI:
    """Creates a fresh OpenAI-compatible client for the given HF token."""
    return OpenAI(
        base_url="https://router.huggingface.co/v1",
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def resolve_image_extension(filename: str, mimetype: str | None) -> str | None:
    suffix = Path(secure_filename(filename)).suffix.lower()
    if suffix in ALLOWED_IMAGE_EXTENSIONS:
        return suffix

    mime_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mime_map.get(mimetype or "")


def next_image_serial() -> int:
    global _next_serial
    with _serial_lock:
        serial_number = _next_serial
        _next_serial += 1
    return serial_number


def find_image_by_serial(serial_number: int) -> Path | None:
    matches = sorted(IMG_DIR.glob(f"{serial_number}.*"))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Per-thread upload tracking
# ---------------------------------------------------------------------------

def append_uploaded_image(thread_id: str, serial_number: int, original_name: str) -> None:
    with _store_lock:
        uploads = list(THREAD_UPLOADS[thread_id])
        upload = {
            "serial_number": serial_number,
            "original_name": original_name,
            "image_url": url_for("get_uploaded_image", serial_number=serial_number),
        }
        uploads.append(upload)
        THREAD_UPLOADS[thread_id] = uploads[-MAX_SAVED_UPLOADS:]

    session["uploaded_images"] = THREAD_UPLOADS[thread_id]


def sync_uploaded_images_from_session(thread_id: str) -> None:
    """Restore uploads from session cookie if the in-memory store is empty."""
    with _store_lock:
        if THREAD_UPLOADS[thread_id]:
            return
        session_uploads = list(session.get("uploaded_images", []))
        if session_uploads:
            THREAD_UPLOADS[thread_id] = session_uploads[-MAX_SAVED_UPLOADS:]


def list_uploaded_images(thread_id: str) -> list[dict[str, Any]]:
    sync_uploaded_images_from_session(thread_id)
    with _store_lock:
        return list(THREAD_UPLOADS[thread_id])


def find_uploaded_image(thread_id: str, serial_number: int) -> dict[str, Any] | None:
    for upload in reversed(list_uploaded_images(thread_id)):
        if int(upload["serial_number"]) == serial_number:
            return upload
    return None


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------

def maybe_extract_serial_reference(message: str) -> int | None:
    match = re.search(r"(?:image|img|prescription|serial)\s*#?\s*(\d+)", message, re.IGNORECASE)
    return int(match.group(1)) if match else None


def resolve_target_upload(thread_id: str, message: str) -> dict[str, Any] | None:
    explicit_serial = maybe_extract_serial_reference(message)
    if explicit_serial is not None:
        return find_uploaded_image(thread_id, explicit_serial)
    return None


def route_chat_graph(state: ChatGraphState, thread_id: str) -> ChatGraphState:
    if state.route_mode == "image_analysis" and state.image_serial is not None:
        state.upload = find_uploaded_image(thread_id, state.image_serial)
        return state

    state.upload = resolve_target_upload(thread_id, state.message)
    return state


# ---------------------------------------------------------------------------
# Vision node
# ---------------------------------------------------------------------------

def run_vision_node(ws, thread_id: str, state: ChatGraphState) -> ChatGraphState:
    upload = state.upload
    if upload is None:
        send_status(ws, "router", "No image selected for analysis. Using chat mode only.", done=True)
        return state

    runtime = get_thread_runtime(thread_id)
    runtime["active_image_serial"] = int(upload["serial_number"])
    send_status(ws, "vision", f"Vision agent selected uploaded image #{upload['serial_number']}.")

    vision_payload, cache_hit = get_or_create_vision_result(upload)
    state.vision_payload = vision_payload

    label = "Using cached analysis" if cache_hit else "Vision agent finished analyzing"
    send_status(ws, "vision", f"{label} image #{upload['serial_number']}.", done=True)
    return state


def image_to_data_url(file_path: Path) -> str:
    mime_type = mimetypes.guess_type(file_path.name)[0] or "image/png"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


_VISION_PROMPT = """
Analyze this image carefully.

Rules:
- Detect whether the image contains medicine, prescription, food, fruit, or drink items.
- Set medicine=true only if medicine or a prescription is visible.
- Extract visible OCR text into extracted_text when present.
- Extract only clearly visible items.
- For medicine items:
  - name should be the medicine name (required, always include it).
  - amount should be dosage such as 500 mg, 5 ml, or 1 tablet.
  - time should be an array only if clearly visible.
  - duration should be a string like "5 days" or "2 months" only if clearly visible.
  - category should be "medicine".
- For food, fruit, or drink items:
  - name should be the item name (required, always include it).
  - amount should be quantity such as 2 pieces or 330 ml when visible.
  - time should be [].
  - duration should be null.
  - category should be "food", "fruit", or "drink".
- Every item MUST have a name field. Never omit it.
- If a field is not visible, leave it empty or null instead of guessing.
- Do not hallucinate.
- Skip unreadable items.
""".strip()


def analyze_image_file(file_path: Path) -> VisionAnalysis:
    """
    Runs vision analysis with round-robin HF token rotation and automatic
    fallback to the next key on quota / auth / rate-limit errors —
    identical pattern to Gemini key rotation.
    """
    keys = ordered_hf_api_keys()
    last_error: Exception | None = None

    for attempt_index, api_key in enumerate(keys):
        client = build_qwen_client(api_key)
        try:
            completion = client.beta.chat.completions.parse(
                model="Qwen/Qwen3-VL-8B-Instruct:novita",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VISION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_to_data_url(file_path)},
                            },
                        ],
                    }
                ],
                response_format=VisionAnalysis,
            )
            return completion.choices[0].message.parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt_index == len(keys) - 1 or not is_hf_api_error(exc):
                raise
            # Transient / quota error — try the next key silently
            continue

    raise last_error  # type: ignore[misc]


def get_or_create_vision_result(upload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    serial_number = int(upload["serial_number"])
    with _vision_lock:
        cached = VISION_CACHE.get(serial_number)
    if cached is not None:
        return cached, True

    file_path = find_image_by_serial(serial_number)
    if file_path is None:
        raise FileNotFoundError(f"Uploaded image #{serial_number} could not be found.")

    analysis = analyze_image_file(file_path)
    payload = {
        "serial_number": serial_number,
        "original_name": upload.get("original_name"),
        "image_url": upload.get("image_url"),
        "analysis": analysis.model_dump(),
    }
    with _vision_lock:
        VISION_CACHE[serial_number] = payload
    return payload, False


def build_vision_context(vision_payload: dict[str, Any]) -> str:
    return (
        "Vision agent result for the currently referenced uploaded image.\n"
        f"Image serial: {vision_payload['serial_number']}\n"
        f"Original name: {vision_payload.get('original_name')}\n"
        "Structured analysis:\n"
        f"{json.dumps(vision_payload['analysis'], indent=2)}"
    )


# ---------------------------------------------------------------------------
# WebSocket event helpers
# ---------------------------------------------------------------------------

def event_payload(event_type: str, **data: Any) -> str:
    return json.dumps({"type": event_type, **data})


def send_status(ws, phase: str, message: str, done: bool = False) -> None:
    ws.send(event_payload("status", phase=phase, message=message, done=done, timestamp=now_label()))


def send_error(ws, message: str) -> None:
    ws.send(event_payload("error", message=message, timestamp=now_label()))


# ---------------------------------------------------------------------------
# LLM streaming
# ---------------------------------------------------------------------------

def chunk_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def stream_gemini_reply(
    ws,
    user: dict[str, str],
    thread_id: str,
    user_message: str,
    vision_payload: dict[str, Any] | None,
) -> str:
    history = list(get_thread_history(thread_id))
    prompt_messages: list[BaseMessage] = [SystemMessage(content=build_system_prompt(user)), *history]

    if vision_payload is not None:
        prompt_messages.append(SystemMessage(content=build_vision_context(vision_payload)))

    prompt_messages.append(HumanMessage(content=user_message))

    ws.send(event_payload("assistant_start", sender="Atlas", timestamp=now_label()))

    parts: list[str] = []
    last_error: Exception | None = None
    keys = ordered_gemini_api_keys()

    for attempt_index, api_key in enumerate(keys):
        llm = build_gemini_llm(api_key)
        emitted_any_chunk = False

        try:
            for chunk in llm.stream(prompt_messages):
                text = chunk_to_text(getattr(chunk, "content", ""))
                if not text:
                    continue
                text = text.replace("*", "")
                emitted_any_chunk = True
                parts.append(text)
                ws.send(event_payload("assistant_delta", delta=text))
            last_error = None
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if emitted_any_chunk or attempt_index == len(keys) - 1 or not is_gemini_api_error(exc):
                raise
            send_status(
                ws, "reasoning",
                f"Atlas is switching to backup model key {attempt_index + 2} after an API issue.",
            )
            continue

    if last_error is not None and not parts:
        raise last_error

    final_text = "".join(parts).strip() or "I couldn't generate a response just now."
    ws.send(event_payload("assistant_end", timestamp=now_label()))

    # Save to this user's persistent history
    history_ref = get_thread_history(thread_id)
    history_ref.extend([
        HumanMessage(content=user_message),
        AIMessage(content=final_text),
    ])
    trim_history(history_ref)
    return final_text


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

def process_chat_message(
    ws,
    user: dict[str, str],
    thread_id: str,
    message: str,
    route_mode: str = "chat",
    image_serial: int | None = None,
) -> None:
    ws.send(event_payload(
        "user_echo",
        sender=user["name"],
        message=message,
        timestamp=now_label(),
    ))

    send_status(ws, "router", "Router agent is checking whether this needs image analysis.")

    state = ChatGraphState(message=message, route_mode=route_mode, image_serial=image_serial)
    state = route_chat_graph(state, thread_id)

    if state.upload is not None:
        send_status(
            ws, "router",
            f"Router selected image analysis for image #{state.upload['serial_number']}.",
            done=True,
        )
        try:
            state = run_vision_node(ws, thread_id, state)
        except FileNotFoundError as exc:
            send_error(ws, str(exc))
            return
        except Exception as exc:
            send_error(ws, f"Vision analysis failed: {exc}")
            return
    else:
        send_status(ws, "router", "Router selected chat without image analysis.", done=True)

    send_status(ws, "reasoning", "Atlas is drafting the response.")
    try:
        stream_gemini_reply(ws, user, thread_id, message, state.vision_payload)
    except Exception as exc:
        send_error(ws, f"Assistant request failed: {exc}")
        return
    send_status(ws, "reasoning", "Response complete.", done=True)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.get("/")
def home() -> str:
    ensure_thread_id()
    user = session.get("user")
    if user:
        return redirect(url_for("chat_page"))
    return render_template("login.html", roles=ALLOWED_ROLES, error=None)


@app.post("/login")
def login():
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "")

    if not name or role not in ALLOWED_ROLES:
        return (
            render_template(
                "login.html",
                roles=ALLOWED_ROLES,
                error="Please enter your name and choose a valid role.",
            ),
            400,
        )

    # ── Preserve the context_ids map before clearing the session ─────────────
    # This is the single most important line: the name→context_id mapping
    # must never be wiped, so we rescue it before session.clear().
    context_ids: dict[str, str] = session.get("context_ids") or {}

    session.clear()

    # Restore the persistent map and mark session as permanent (90-day cookie)
    session["context_ids"] = context_ids
    session.permanent = True

    # Resolve (or mint) the stable context ID for this user name
    thread_id = get_or_create_context_id(name)

    session["thread_id"] = thread_id
    session["uploaded_images"] = THREAD_UPLOADS.get(thread_id, [])
    session["user"] = {
        "name": name,
        "role": role,
        "role_label": ALLOWED_ROLES[role],
    }
    return redirect(url_for("chat_page"))


@app.get("/chat")
def chat_page() -> str:
    ensure_thread_id()
    user = session.get("user")
    if not user:
        return redirect(url_for("home"))

    starter_prompt = {
        "patient": "Describe your symptoms, medications, or ask about an uploaded image.",
        "doctor": "Paste notes, triage details, or ask to analyze an uploaded prescription.",
        "ambulance_service": "Share incident details, handoff notes, or uploaded image questions.",
        "hospital_service": "Share admission details, transfer notes, or uploaded image questions.",
    }[user["role"]]

    return render_template(
        "chat.html",
        user=user,
        starter_prompt=starter_prompt,
        uploaded_images=list_uploaded_images(ensure_thread_id()),
    )


@app.post("/logout")
def logout():
    # ── Preserve the context_ids map — this is what keeps history alive ───────
    context_ids: dict[str, str] = session.get("context_ids") or {}

    session.clear()

    # Put the map back; keep the cookie alive for 90 days
    session["context_ids"] = context_ids
    session.permanent = True

    return redirect(url_for("home"))


@app.post("/api/uploads/prescription")
def upload_prescription():
    thread_id = ensure_thread_id()
    user = session.get("user")
    if not user:
        return jsonify({"error": "Your session expired. Please log in again."}), 401

    uploaded_file = request.files.get("file")
    if uploaded_file is None or not uploaded_file.filename:
        return jsonify({"error": "Please choose an image to upload."}), 400

    extension = resolve_image_extension(uploaded_file.filename, uploaded_file.mimetype)
    if extension is None:
        return jsonify({"error": "Only image uploads are supported right now."}), 400

    serial_number = next_image_serial()
    file_path = IMG_DIR / f"{serial_number}{extension}"
    uploaded_file.save(file_path)
    append_uploaded_image(thread_id, serial_number, uploaded_file.filename)

    return jsonify({
        "serial_number": serial_number,
        "image_url": url_for("get_uploaded_image", serial_number=serial_number),
        "original_name": uploaded_file.filename,
        "message": f"Uploaded image #{serial_number}. Ask about it in chat when you want analysis.",
    })


@app.get("/img/<int:serial_number>")
def get_uploaded_image(serial_number: int):
    file_path = find_image_by_serial(serial_number)
    if file_path is None:
        abort(404)
    return send_file(file_path)


@app.get("/api/uploads/<int:serial_number>/analysis")
def get_uploaded_image_analysis(serial_number: int):
    thread_id = ensure_thread_id()
    user = session.get("user")
    if not user:
        return jsonify({"error": "Your session expired. Please log in again."}), 401

    upload = find_uploaded_image(thread_id, serial_number)
    if upload is None:
        return jsonify({"error": f"Image #{serial_number} is not available in this session."}), 404

    try:
        vision_payload, cache_hit = get_or_create_vision_result(upload)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404

    return jsonify({
        "ok": True,
        "cache_hit": cache_hit,
        "serial_number": serial_number,
        "original_name": vision_payload.get("original_name"),
        "image_url": vision_payload.get("image_url"),
        "analysis": vision_payload.get("analysis", {}),
    })


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@sock.route("/ws/chat")
def chat_socket(ws) -> None:
    user = session.get("user")
    if not user:
        send_error(ws, "Your session expired. Please log in again.")
        return

    thread_id = ensure_thread_id()

    ws.send(event_payload(
        "assistant",
        sender="Atlas",
        message=(
            f"Welcome back, {user['name']}. You're in {user['role_label']} mode. "
            "Upload an image anytime, then ask about it when you're ready."
        ),
        timestamp=now_label(),
    ))

    uploads = list_uploaded_images(thread_id)
    if uploads:
        ws.send(event_payload(
            "status",
            phase="uploads",
            message=(
                f"{len(uploads)} uploaded image(s) available. "
                f"Latest is image #{uploads[-1]['serial_number']}."
            ),
            done=True,
            timestamp=now_label(),
        ))

    while True:
        raw_message = ws.receive()
        if raw_message is None:
            break

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            send_error(ws, "Invalid message payload received.")
            continue

        message = str(payload.get("message", "")).strip()
        route_mode = str(payload.get("route_mode", "chat")).strip() or "chat"
        raw_image_serial = payload.get("image_serial")
        image_serial = (
            int(raw_image_serial)
            if isinstance(raw_image_serial, int)
            or (isinstance(raw_image_serial, str) and raw_image_serial.isdigit())
            else None
        )

        if not message:
            send_error(ws, "Please enter a message before sending.")
            continue

        process_chat_message(ws, user, thread_id, message, route_mode=route_mode, image_serial=image_serial)


if __name__ == "__main__":
    app.run(host="0.0.0.0")
