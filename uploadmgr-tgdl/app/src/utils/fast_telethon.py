import math
import os
import asyncio
from telethon.tl.functions.upload import GetFileRequest
from telethon.utils import get_input_location

CHUNK_SIZE = 512 * 1024  # 512 KB per chunk (Telegram MTProto max limit)

async def _download_part(client, location, offset, limit, dc_id=None):
    """Fetches a single chunk from Telegram with retries."""
    for attempt in range(5):
        try:
            req = GetFileRequest(location=location, offset=offset, limit=limit)
            if dc_id:
                sender = await client._borrow_exported_sender(dc_id)
                try:
                    result = await sender.send(req)
                finally:
                    await client._return_exported_sender(sender)
            else:
                result = await client(req)
            return result.bytes if result else b""
        except Exception as e:
            if attempt < 4:
                wait = getattr(e, 'seconds', 1.0)
                await asyncio.sleep(min(wait, 3.0))
            else:
                raise e

async def fast_download_file(client, location, target_path, file_size, dc_id=None, progress_callback=None, cancel_event=None, workers=4):
    """
    Downloads media at maximum throughput using parallel chunk streams.
    Falls back gracefully if parallel streaming is not supported for the media.
    """
    if file_size <= 0:
        return False

    total_parts = math.ceil(file_size / CHUNK_SIZE)
    if total_parts <= 1 or workers <= 1:
        # Small file: single chunk is fast enough directly
        return False

    # Extract dc_id and InputFileLocation TLObject
    if isinstance(location, tuple) and len(location) == 2:
        dc_id, location = location
    elif not hasattr(location, 'SUBCLASS_OF_ID') or location.SUBCLASS_OF_ID != 0x1523d462:
        try:
            from telethon.utils import get_input_location
            dc_id, location = get_input_location(location)
        except Exception:
            return False

    if not location or not hasattr(location, 'SUBCLASS_OF_ID'):
        return False

    # Pre-allocate output file
    temp_path = target_path + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    with open(temp_path, "wb") as f:
        f.truncate(file_size)

    queue = asyncio.Queue()
    for idx in range(total_parts):
        queue.put_nowait(idx)

    downloaded_bytes = [0]
    lock = asyncio.Lock()
    file_handle = open(temp_path, "r+b")

    class DownloadCancelled(Exception): pass

    async def worker():
        while not queue.empty():
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled()

            try:
                part_idx = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            offset = part_idx * CHUNK_SIZE
            limit = CHUNK_SIZE # Telegram requires limit to be a multiple of 4KB and power of 2

            chunk = await _download_part(client, location, offset, limit, dc_id=dc_id)
            if not chunk:
                continue

            # If last chunk returned extra padding past file_size, trim it
            if offset + len(chunk) > file_size:
                chunk = chunk[:file_size - offset]

            async with lock:
                file_handle.seek(offset)
                file_handle.write(chunk)
                downloaded_bytes[0] += len(chunk)
                current = downloaded_bytes[0]

            if progress_callback:
                res = progress_callback(current, file_size)
                if asyncio.iscoroutine(res):
                    await res

            queue.task_done()

    try:
        tasks = [asyncio.create_task(worker()) for _ in range(min(workers, total_parts))]
        await asyncio.gather(*tasks)
    finally:
        file_handle.close()

    if cancel_event and cancel_event.is_set():
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except: pass
        return False

    # Atomically replace final file
    if os.path.exists(target_path):
        try: os.remove(target_path)
        except: pass
    os.rename(temp_path, target_path)
    return True
