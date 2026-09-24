"""Export captured output intervals without loading a full log into Python."""
from __future__ import annotations
import asyncio
from pal.execution.result_snapshots import capture_stream_files, file_preview, render_snapshot_hint


async def materialize_delivery(owner, event, *, runtime, turn_id, call_id, budget, offsets=None):
    limit = runtime._resolve_char_limit(budget) if runtime is not None and budget is not None else None
    total = sum(event.get(s + '_total', 0) for s in ('stdout', 'stderr'))
    if limit is None or total <= limit:
        return await owner.shell.materialize(event)
    paths = await owner.shell.materialize(event, load_output=False)
    intervals = [(stream, paths[stream + '_path'], int((offsets or {}).get(stream, 0)),
                  int(event.get(stream + '_total', 0))) for stream in ('stdout', 'stderr')
                 if paths.get(stream + '_path')]
    if not intervals:
        # Compatibility with embedded adapters that already supplied bytes.
        return await owner.shell.materialize(event)
    lifetime = runtime.logical_context_for_turn(turn_id or call_id).execution_lifetime_id
    worker = asyncio.create_task(asyncio.to_thread(capture_stream_files, runtime.result_snapshots, intervals,
                                                  call_id=call_id, lifetime=lifetime))
    ref = None
    try:
        ref = await asyncio.shield(worker)
        preview = file_preview(ref, budget.preview_chars or 1000)
    except BaseException:
        if ref is None:
            try:
                ref = await worker
            except Exception:
                pass
        if ref is not None:
            runtime.result_snapshots.finish_references((ref,))
        raise
    return {**event, 'truncated': True, '_output_snapshots': (ref,),
            '_snapshot_text': preview + '\n\n' + render_snapshot_hint(ref)}
