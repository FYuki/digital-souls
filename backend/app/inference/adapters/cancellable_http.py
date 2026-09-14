"""抽出中断時は当該HTTP接続だけを閉じ、共有clientや推論サーバーは停止しない。"""

import asyncio
from collections.abc import Mapping
from contextlib import suppress

import httpx

from app.inference.cancellation import current_cancellation_token, raise_if_cancelled


def post(
    client: httpx.Client, url: str, *, json: object,
    timeout: httpx.Timeout, headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    token = current_cancellation_token()
    if token is None:
        return client.post(url, json=json, timeout=timeout, headers=headers)
    raise_if_cancelled()
    request = client.build_request("POST", url, json=json, timeout=timeout, headers=headers)

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False) as transport:
            pending = asyncio.create_task(transport.send(request))
            try:
                while not pending.done():
                    raise_if_cancelled()
                    await asyncio.wait({pending}, timeout=0.025)
                raise_if_cancelled()
                return await pending
            finally:
                if not pending.done():
                    pending.cancel()
                # 接続の終了まで待ち、バックグラウンドのHTTPを残さない。
                with suppress(asyncio.CancelledError, Exception):
                    await pending

    return asyncio.run(run())
