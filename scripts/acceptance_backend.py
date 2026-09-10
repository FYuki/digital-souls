"""使い捨て受入Backendの停止診断。frame位置だけを記録する。"""

import asyncio
import faulthandler
import sys
from contextlib import suppress

import uvicorn


async def trace_tasks() -> None:
    while True:
        await asyncio.sleep(60)
        for index, task in enumerate(asyncio.all_tasks()):
            print(
                f"Acceptance task: {index} cancelling={task.cancelling()}",
                file=sys.stderr,
                flush=True,
            )
            current = task.get_coro()
            seen = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                frame = getattr(current, "cr_frame", None) or getattr(
                    current, "gi_frame", None
                )
                if frame is not None:
                    code = frame.f_code
                    print(
                        f"Acceptance frame: {code.co_filename}:{frame.f_lineno} in {code.co_name}",
                        file=sys.stderr,
                        flush=True,
                    )
                current = getattr(current, "cr_await", None) or getattr(
                    current, "gi_yieldfrom", None
                )


serve = uvicorn.Server.serve


async def serve_with_diagnostics(self, *args, **kwargs):
    diagnostic = asyncio.create_task(trace_tasks())
    try:
        return await serve(self, *args, **kwargs)
    finally:
        diagnostic.cancel()
        with suppress(asyncio.CancelledError):
            await diagnostic


if __name__ == "__main__":
    faulthandler.dump_traceback_later(90, repeat=True)
    uvicorn.Server.serve = serve_with_diagnostics
    uvicorn.main()
