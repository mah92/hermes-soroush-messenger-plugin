"""Regression coverage for the Soroush poll busy-loop (wrong token / user number).

Symptom (before the fix): with a bad token, `getUpdates` answered instantly
with HTTP 401 + `{"ok": false}`. The adapter returned that body as an EMPTY
result, so `_poll_loop` re-polled with no delay -> ~5,300 req/s and a pinned
CPU core. This test spins the REAL `_poll_loop` against a local mock that
answers exactly like Soroush (instant 401 JSON) and asserts the loop stays
bounded, and that a healthy long poll still works.

Run:  python tests/test_busy_loop.py          (needs aiohttp + hermes gateway)
      PYTHONPATH=~/.hermes/hermes-agent python tests/test_busy_loop.py
"""
import asyncio
import importlib.util
import os
import sys
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(os.path.dirname(HERE), "adapter.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")


def load_adapter():
    spec = importlib.util.spec_from_file_location("soroush_regression", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["soroush_regression"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_adapter(mod, token):
    a = object.__new__(mod.SoroushAdapter)
    a._token = token
    a._running = False
    a._last_update_id = 0
    a._bot_id = ""
    a._http = None
    a._poll_task = None
    a._allowed_chats = set()
    a._allowed_users = set()
    a._require_mention = False
    a._pending_context = {}
    a._max_voice_duration = 0
    return a


async def start(routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _noop():
    return None


async def test_busy_loop(mod):
    print("\n1) wrong token: getUpdates answered with instant 401 must NOT spin")
    hits = {"n": 0}

    async def h401(request):
        hits["n"] += 1
        return web.json_response(
            {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401
        )

    runner, port = await start([("*", "/{t:.*}", h401)])
    mod.API_BASE = f"http://127.0.0.1:{port}"
    import aiohttp

    a = make_adapter(mod, "12345:BADBADBAD")
    a._running = True
    a._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30.0))
    a._handle_update = lambda u: _noop()

    hits["n"] = 0
    c0 = time.process_time()
    task = asyncio.create_task(a._poll_loop())
    await asyncio.sleep(3.0)
    a._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    cpu = time.process_time() - c0
    check("request rate bounded (<= 4 in 3s)", hits["n"] <= 4, f"(got {hits['n']})")
    check("cpu burn bounded (< 0.2s in 3s)", cpu < 0.2, f"(got {cpu:.3f}s)")
    await a._http.close()
    await runner.cleanup()


async def test_wrong_token_connect(mod):
    print("\n2) wrong token: connect() must refuse and start no polling")
    async def h401(request):
        return web.json_response(
            {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401
        )

    runner, port = await start([("*", "/{t:.*}", h401)])
    mod.API_BASE = f"http://127.0.0.1:{port}"
    a = make_adapter(mod, "12345:BADBADBAD")
    ok = await a.connect()
    check("connect() returns False", ok is False, f"(got {ok})")
    check("no poll task started", a._poll_task is None)
    if a._http:
        await a._http.close()
    await runner.cleanup()


async def test_healthy_poll(mod):
    print("\n3) healthy long poll: update delivered, offset advanced")
    state = {"calls": 0}

    async def h_updates(request):
        state["calls"] += 1
        if state["calls"] == 1:
            return web.json_response({"ok": True, "result": [
                {"update_id": 41, "message": {"message_id": 1, "chat": {"id": 5, "type": "private"},
                                              "from": {"id": 5, "first_name": "A"}, "text": "hi"}}
            ]})
        await asyncio.sleep(1.5)  # emulate a real long poll that blocks
        return web.json_response({"ok": True, "result": []})

    async def h_ok(request):
        return web.json_response({"ok": True, "result": {"id": 7, "username": "bot"}})

    runner, port = await start([
        ("GET", "/bottok/getUpdates", h_updates),
        ("GET", "/bottok/getMe", h_ok),
        ("GET", "/bottok/deleteWebhook", h_ok),
    ])
    mod.API_BASE = f"http://127.0.0.1:{port}"
    import types as _types

    a = make_adapter(mod, "tok")
    a.build_source = lambda **kw: _types.SimpleNamespace(**kw)
    a.handle_message = lambda event: _noop()
    ok = await a.connect()
    check("connect() True", ok is True)
    await asyncio.sleep(0.5)
    check("offset advanced to update_id", a._last_update_id == 41, f"(got {a._last_update_id})")
    await a.disconnect()
    if a._http:
        await a._http.close()
    await runner.cleanup()


async def test_failed_send(mod):
    print("\n4) failed send returns a clean SendResult (no crash)")
    async def h400(request):
        return web.json_response(
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}, status=400
        )

    runner, port = await start([("*", "/{t:.*}", h400)])
    mod.API_BASE = f"http://127.0.0.1:{port}"
    import aiohttp

    a = make_adapter(mod, "tok")
    a._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30.0))
    res = await a.send(chat_id="999999999", content="hello")
    check("send() success=False", res.success is False)
    check("send() carries an error", bool(res.error), f"(error={res.error!r})")
    await a._http.close()
    await runner.cleanup()


async def test_image_tempfile_no_leak(mod):
    print("\n5) send_image upload failure must not leak the temp .jpg")
    import glob

    async def h_image(request):
        return web.Response(body=b"\xff\xd8\xff\xe0FAKEJPEG", content_type="image/jpeg")

    async def h_sendphoto(request):
        return web.json_response(
            {"ok": False, "error_code": 500, "description": "boom"}, status=500
        )

    async def h_sendmessage(request):
        return web.json_response({"ok": True, "result": {"message_id": 9}})

    runner, port = await start([
        ("GET", "/img.jpg", h_image),
        ("POST", "/bottok/sendPhoto", h_sendphoto),
        ("POST", "/bottok/sendMessage", h_sendmessage),
    ])
    mod.API_BASE = f"http://127.0.0.1:{port}"
    import aiohttp

    a = make_adapter(mod, "tok")
    a._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30.0))
    before = set(glob.glob("/tmp/tmp*.jpg"))
    await a.send_image(chat_id="1", image_url=f"http://127.0.0.1:{port}/img.jpg", caption="c")
    leaked = set(glob.glob("/tmp/tmp*.jpg")) - before
    check("no temp .jpg leaked", not leaked, f"(leaked={leaked})")
    await a._http.close()
    await runner.cleanup()


async def main():
    mod = load_adapter()
    await test_busy_loop(mod)
    await test_wrong_token_connect(mod)
    await test_healthy_poll(mod)
    await test_failed_send(mod)
    await test_image_tempfile_no_leak(mod)
    print(f"\n=== {len(PASS)} passed, {len(FAIL)} failed ===")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
