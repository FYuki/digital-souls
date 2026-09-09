from __future__ import annotations

import asyncio

from app.conversation_core.session import _log_unhandled_task_error


def test_failed_core_task_logs_location_without_exception_body(caplog):
    async def run():
        async def failed_operation():
            raise ValueError('private transcript must never appear')
        task = asyncio.create_task(failed_operation())
        await asyncio.gather(task, return_exceptions=True)
        _log_unhandled_task_error(task)
    asyncio.run(run())
    assert 'type=ValueError' in caplog.text
    assert 'failed_operation' in caplog.text
    assert 'private transcript' not in caplog.text


def test_cancelled_and_successful_tasks_do_not_log_errors(caplog):
    async def run():
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        _log_unhandled_task_error(completed)
        cancelled = asyncio.create_task(asyncio.sleep(60))
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        _log_unhandled_task_error(cancelled)
    asyncio.run(run())
    assert caplog.text == ''


def test_os_error_logs_errno_without_sensitive_filename(caplog):
    async def run():
        async def failed_operation():
            raise OSError(24, 'private transcript', '/private/session-secret.json')
        task = asyncio.create_task(failed_operation())
        await asyncio.gather(task, return_exceptions=True)
        _log_unhandled_task_error(task)
    asyncio.run(run())
    assert 'type=OSError errno=24' in caplog.text
    assert 'failed_operation' in caplog.text
    assert 'private transcript' not in caplog.text
    assert 'session-secret' not in caplog.text
