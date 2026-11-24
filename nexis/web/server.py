from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


def create_app(service: Any) -> Starlette:
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        service.attach_loop(asyncio.get_running_loop())
        yield

    async def index(_: Request):
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    async def favicon(_: Request):
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    async def events(request: Request):
        queue = service.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            finally:
                service.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def get_state(_: Request):
        return JSONResponse(await run_in_threadpool(service.state))

    async def get_history(_: Request):
        return JSONResponse(
            {"session": service.session_id, "messages": await run_in_threadpool(service.history)}
        )

    async def get_models(_: Request):
        state = await run_in_threadpool(service.state)
        return JSONResponse(
            {"model": state["model"], "models": state["models"], "ollama": state["ollama"]}
        )

    async def post_model(request: Request):
        payload = await _json(request)
        name = str(payload.get("model") or "").strip()
        if not name:
            return JSONResponse({"error": "model is required"}, status_code=400)
        service.set_model(name)
        return JSONResponse({"model": name})

    async def post_chat(request: Request):
        payload = await _json(request)
        text = str(payload.get("text") or "")
        if not text.strip():
            return JSONResponse({"error": "text is required"}, status_code=400)
        if not service.start_turn(text):
            return JSONResponse({"error": "Nexis is still working on the previous turn."}, status_code=409)
        return JSONResponse({"started": True})

    async def post_stop(_: Request):
        service.stop_turn()
        return JSONResponse({"stopping": True})

    async def post_confirm(request: Request):
        payload = await _json(request)
        token = str(payload.get("id") or "")
        approved = bool(payload.get("approved"))
        found = service.resolve_confirm(token, approved)
        return JSONResponse({"ok": found}, status_code=200 if found else 404)

    async def post_voice(request: Request):
        payload = await _json(request)
        action = str(payload.get("action") or "").strip().lower()
        if action == "start":
            try:
                await run_in_threadpool(service.voice_start)
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            return JSONResponse({"state": "recording"})
        if action == "stop":
            text = await run_in_threadpool(service.voice_stop)
            return JSONResponse({"text": text})
        return JSONResponse({"error": "action must be start or stop"}, status_code=400)

    async def post_handsfree(request: Request):
        payload = await _json(request)
        on = bool(payload.get("on"))
        active = await run_in_threadpool(service.set_handsfree, on)
        return JSONResponse({"on": active})

    async def post_listen(request: Request):
        payload = await _json(request)
        paused = bool(payload.get("paused"))
        service.set_listening_paused(paused)
        return JSONResponse({"paused": paused})

    async def get_tts(request: Request):
        text = (request.query_params.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)
        try:
            path, mime = await run_in_threadpool(service.synthesize, text[:2000])
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return FileResponse(path, media_type=mime)

    async def post_new_session(_: Request):
        session = await run_in_threadpool(service.new_session)
        return JSONResponse({"session": session})

    async def post_open_workspace(_: Request):
        await run_in_threadpool(service.open_workspace)
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index),
        Route("/favicon.svg", favicon),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
        Route("/api/events", events),
        Route("/api/state", get_state),
        Route("/api/history", get_history),
        Route("/api/models", get_models),
        Route("/api/model", post_model, methods=["POST"]),
        Route("/api/chat", post_chat, methods=["POST"]),
        Route("/api/stop", post_stop, methods=["POST"]),
        Route("/api/confirm", post_confirm, methods=["POST"]),
        Route("/api/voice", post_voice, methods=["POST"]),
        Route("/api/handsfree", post_handsfree, methods=["POST"]),
        Route("/api/listen", post_listen, methods=["POST"]),
        Route("/api/tts", get_tts),
        Route("/api/session/new", post_new_session, methods=["POST"]),
        Route("/api/workspace/open", post_open_workspace, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
