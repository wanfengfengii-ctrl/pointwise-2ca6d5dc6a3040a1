"""Per-block slot reuse for reels imported with ``reuse_slots=True``.

Each original block owns its fixed-offset base slot plus at most one attached
slot kept in a per-reel side file.  A candidate payload is staged straight into
a slot, so it counts against the two-slot budget as soon as it reaches media.
The catalogue tracks which digest each slot currently holds; every digest that
stays owed to readers (the current published version plus the unread blocks of
every unreleased task) always has its bytes actually present in one of the two
slots.  Equal bytes share a slot.  A block whose candidate would need a third
distinct payload is reported as blocked and the whole batch is dropped before
anything is written or any request number is consumed.
"""
import os
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from archive.block_io import read_base_block, sync_base, sync_directory, write_base_block
from archive.catalog import ReelRecord
from archive.integrity import BlockIntegrityError, verify_block
from archive.layout import BlockLayout, block_offset
from archive.workspace import Workspace
from revision.journal import current_revision, published_checksum
from revision.request import AmendRequest


BASE_SLOT = 0
ATTACHED_SLOT = 1


def attached_path(workspace: Workspace, reel_id: str) -> Path:
    digest = sha256(reel_id.encode('utf-8')).hexdigest()
    return workspace.media / f'slot-{digest}.bin'


def read_attached_block(
    workspace: Workspace, reel_id: str, layout: BlockLayout, block_no: int,
) -> bytes:
    offset = block_offset(layout, block_no)
    try:
        with attached_path(workspace, reel_id).open('rb', buffering=0) as stream:
            stream.seek(offset)
            payload = stream.read(layout.block_width)
    except FileNotFoundError:
        payload = b''
    if len(payload) != layout.block_width:
        raise BlockIntegrityError(f'short attached read at block {block_no}')
    return payload


def write_attached_block(
    workspace: Workspace, reel_id: str, layout: BlockLayout, block_no: int, payload: bytes,
) -> None:
    offset = block_offset(layout, block_no)
    if len(payload) != layout.block_width:
        raise ValueError(f'wrong attached payload length at block {block_no}')
    descriptor = os.open(attached_path(workspace, reel_id), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+b', buffering=0) as stream:
        stream.seek(offset)
        if stream.write(payload) != layout.block_width:
            raise OSError(f'short attached write at block {block_no}')


def sync_attached_area(workspace: Workspace, reel_id: str) -> None:
    with attached_path(workspace, reel_id).open('r+b', buffering=0) as stream:
        os.fsync(stream.fileno())
    sync_directory(workspace.media)


def slot_map(database: sqlite3.Connection, reel_id: str) -> dict[int, dict[int, str]]:
    slots: dict[int, dict[int, str]] = {}
    rows = database.execute(
        'SELECT block_no, slot, digest FROM reel_slots WHERE reel_id = ?', (reel_id,),
    )
    for row in rows:
        slots.setdefault(row['block_no'], {})[row['slot']] = row['digest']
    return slots


def locate_slot(database: sqlite3.Connection, reel_id: str, block_no: int, digest: str) -> int:
    row = database.execute(
        'SELECT slot FROM reel_slots WHERE reel_id = ? AND block_no = ? AND digest = ?',
        (reel_id, block_no, digest),
    ).fetchone()
    if row is None:
        raise BlockIntegrityError(f'no slot currently holds block {block_no}')
    return row['slot']


def _needed_digests(
    database: sqlite3.Connection, reel: ReelRecord,
) -> dict[int, set[str]]:
    """Digests each block must keep readable: the current published version plus
    the unread tail of every unreleased task's fixed version."""
    current, _ = current_revision(database, reel)
    needed = {
        block_no: {published_checksum(database, reel, current, block_no)}
        for block_no in range(reel.layout.block_count)
    }
    rows = database.execute(
        'SELECT version, cursor FROM tasks WHERE reel_id = ? AND released = 0',
        (reel.reel_id,),
    )
    for row in rows:
        for block_no in range(row['cursor'], reel.layout.block_count):
            needed[block_no].add(published_checksum(database, reel, row['version'], block_no))
    return needed


@dataclass(frozen=True)
class SlotWrite:
    block_no: int
    slot: int
    digest: str
    payload: bytes


def plan_reuse(
    database: sqlite3.Connection, reel: ReelRecord, request: AmendRequest,
) -> tuple[tuple[int, ...], tuple[SlotWrite, ...]]:
    """Split a candidate batch into shared blocks, slot writes and blocked blocks.

    A block is blocked exactly when its candidate payload differs from every
    digest still owed to readers and both slots already hold two distinct owed
    digests, so a third distinct payload would be required.
    """
    slots = slot_map(database, reel.reel_id)
    needed = _needed_digests(database, reel)
    checks = dict(request.checks)
    blocked = []
    writes = []
    for block_no, payload in request.replacements:
        digest = checks[block_no]
        if digest in needed[block_no]:
            continue  # equal bytes already occupy a slot that stays readable
        if len(needed[block_no]) >= 2:
            blocked.append(block_no)
            continue
        held = slots.get(block_no, {})
        if ATTACHED_SLOT not in held:
            target = ATTACHED_SLOT
        elif held.get(BASE_SLOT) not in needed[block_no]:
            target = BASE_SLOT
        else:
            target = ATTACHED_SLOT
        writes.append(SlotWrite(block_no, target, digest, payload))
    return tuple(sorted(blocked)), tuple(writes)


def publish_reuse(
    workspace: Workspace, database: sqlite3.Connection, reel: ReelRecord,
    request: AmendRequest, writes: tuple[SlotWrite, ...], identity: str, *,
    checkpoint=None,
) -> None:
    """Stage candidate payloads into their slots and commit the batch as published.

    Runs inside the caller's transaction: a crash after staging leaves the
    catalogue untouched, and a later retry rewrites the same slot (staging only
    ever targets slots whose previous payload nobody still needs).
    """
    base_path = workspace.media / reel.base_name
    synced = False
    for write in writes:
        if write.slot == BASE_SLOT:
            write_base_block(base_path, reel.layout, write.block_no, write.payload)
            sync_base(base_path)
        else:
            write_attached_block(workspace, reel.reel_id, reel.layout, write.block_no,
                                 write.payload)
            sync_attached_area(workspace, reel.reel_id)
        if not synced:
            synced = True
            if checkpoint is not None:
                checkpoint('first_payload_synced')
    planned = {write.block_no: write.slot for write in writes}
    checks = dict(request.checks)
    for block_no, _payload in request.replacements:
        digest = checks[block_no]
        slot = planned.get(block_no)
        if slot is None:
            slot = locate_slot(database, reel.reel_id, block_no, digest)
        if slot == BASE_SLOT:
            payload = read_base_block(base_path, reel.layout, block_no)
        else:
            payload = read_attached_block(workspace, reel.reel_id, reel.layout, block_no)
        verify_block(payload, digest, block_no)
    database.executemany(
        'INSERT OR REPLACE INTO reel_slots(reel_id, block_no, slot, digest) VALUES (?, ?, ?, ?)',
        ((reel.reel_id, write.block_no, write.slot, write.digest) for write in writes),
    )
    database.execute(
        "INSERT INTO changes(request_id, reel_id, parent_version, version, identity, state) "
        "VALUES (?, ?, ?, ?, ?, 'finalized')",
        (request.request_id, request.reel_id, request.parent_version,
         request.parent_version + 1, identity),
    )
    database.executemany(
        'INSERT INTO change_blocks(request_id, block_no, slot, digest) VALUES (?, ?, ?, ?)',
        ((request.request_id, block_no, slot, digest)
         for slot, (block_no, digest) in enumerate(request.checks)),
    )
