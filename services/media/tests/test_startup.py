import pytest

from media_service import events_runtime


@pytest.mark.asyncio
async def test_bucket_is_set_up_once_object_storage_comes_up(monkeypatch):
    """Object storage may start after the media service; the public-read policy is applied by ensure_bucket,
    so one failed attempt at startup must not mean photos stay private."""

    class Store:
        calls = 0

        async def ensure_bucket(self):
            Store.calls += 1
            if Store.calls < 3:
                raise ConnectionError("storage is not up yet")

    monkeypatch.setattr(events_runtime, "get_store", lambda: Store())
    await events_runtime.ensure_bucket_eventually(interval=0)
    assert Store.calls == 3
