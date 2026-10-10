"""Simple TCP port forwarder: 8320 -> 127.0.0.1:8317 (all traffic passthrough)."""
import asyncio
import os
import signal
import sys

LISTEN_HOST = os.environ.get("VISION_PREPROCESS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("VISION_PREPROCESS_LISTEN_PORT", "8320"))
UPSTREAM_ORIGIN = os.environ.get("VISION_PREPROCESS_UPSTREAM_ORIGIN", "http://127.0.0.1:8317")

# Parse upstream host/port from origin URL
_up = UPSTREAM_ORIGIN.replace("http://", "").replace("https://", "")
UPSTREAM_HOST = _up.split(":")[0] if ":" in _up else _up
UPSTREAM_PORT = int(_up.split(":")[1]) if ":" in _up else 8317


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle_client(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except Exception:
        client_writer.close()
        return
    await asyncio.gather(
        pipe(client_reader, upstream_writer),
        pipe(upstream_reader, client_writer),
    )


async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    print(f"Port forwarder listening on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)

    loop = asyncio.get_event_loop()
    stop = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set_result, None)

    async with server:
        await stop
    print("Shutting down.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
