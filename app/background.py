"""Helpers for slow work (model inference, LLM calls) inside the eventlet web process.

The app runs under eventlet without monkey patching, so any blocking call (CPU-bound
inference, or HTTP through a non-green client) would freeze every connection. Such
calls go through ``run_blocking``, which executes them in a real OS thread.

``inline=True`` (tests) runs everything synchronously in the caller.
"""
from app.extensions import socketio


def run_blocking(fn, *args, inline: bool = False):
    if not inline and socketio.async_mode == "eventlet":
        from eventlet import tpool

        return tpool.execute(fn, *args)
    return fn(*args)


def spawn(fn, *args, inline: bool = False) -> None:
    if inline:
        fn(*args)
    else:
        socketio.start_background_task(fn, *args)
