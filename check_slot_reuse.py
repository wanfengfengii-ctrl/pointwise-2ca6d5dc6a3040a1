"""Joint acceptance checks for the per-block slot-reuse errata mode.

Drives the SLOT_REUSE.md handover sample from examples/slot_reuse.json on real
media: the two-slot budget blocks a batch without consuming its request
number, freeing a slot lets the original number retry, fixed versions keep
reading across reopens, checkpoint crashes only ever expose complete published
versions, and sealed reports are reclaimed by number.  Ordinary reels stay
covered by check_project.py and check_slot_reuse_baseline.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.importer import ingest
from archive.session import open_archive
from archive.workspace import work_transaction
from demo import load_reel
from reader.cursor import next as read_next
from reader.release import finish
from reader.tasks import open as open_task
from reports.issue import report
from revision.publication import amend
from revision.reuse import attached_path


ROOT = Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT / 'examples' / 'slot_reuse.json').read_text(encoding='utf-8'))
EXPECTED = FIXTURE['expected']
CHECKPOINT_EXIT = 73


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def values(observations):
    return [item.value for item in observations]


def fixture_request(reel, name):
    request = FIXTURE['requests'][name]
    replacements = [
        (block_no, value.to_bytes(reel.layout.block_width, 'little', signed=True))
        for block_no, value in request['readings']
    ]
    return request['request_id'], request['parent_version'], replacements


def seed_round(root):
    workspace = open_archive(root)
    reel = ingest(workspace, *load_reel(ROOT / FIXTURE['source']), reuse_slots=True)
    task_a = open_task(workspace, reel.reel_id)
    require(task_a.version == 1, 'task A should bind version 1')
    require(values(read_next(workspace, task_a.task_id, FIXTURE['a_prefix'])) == [12, 18],
            'task A prefix mismatch')
    return workspace, reel, task_a


def submit(workspace, reel, name, **kwargs):
    request_id, parent_version, replacements = fixture_request(reel, name)
    return amend(workspace, reel.reel_id, request_id, parent_version, replacements, **kwargs)


def media_snapshot(workspace, reel):
    slot_path = attached_path(workspace, reel.reel_id)
    return {
        'base': (workspace.media / reel.base_name).read_bytes().hex(),
        'attached': slot_path.read_bytes().hex() if slot_path.exists() else '',
        'files': sorted(path.name for path in workspace.media.iterdir()),
    }


def require_media(workspace, reel, base_values, attached_values):
    width = reel.layout.block_width
    encode = lambda vals: b''.join(
        value.to_bytes(width, 'little', signed=True) for value in vals).hex()
    snapshot = media_snapshot(workspace, reel)
    require(snapshot['base'] == encode(base_values),
            f'base media mismatch: {snapshot["base"]}')
    require(snapshot['attached'] == encode(attached_values),
            f'attached media mismatch: {snapshot["attached"]}')
    require(snapshot['files'] == sorted([reel.base_name, attached_path(workspace, reel.reel_id).name]),
            f'each block must stay within its base slot and one attached slot: {snapshot["files"]}')


def round_blocked_retry(root):
    """Sample steps 1-2: blocked mixed batch, freed slot, retry, reopen, reports."""
    workspace, reel, task_a = seed_round(root)
    require(submit(workspace, reel, 'tail').version == 2, 'tail should publish version 2')
    task_b = open_task(workspace, reel.reel_id)
    require(task_b.version == 2, 'task B should bind version 2')
    require(submit(workspace, reel, 'head').version == 3,
            'head should publish version 3 while A and B stay open')

    blocked = submit(workspace, reel, 'mixed')
    require(blocked == tuple(EXPECTED['blocked']), f'mixed should be blocked on {EXPECTED["blocked"]}')
    with work_transaction(workspace) as database:
        row = database.execute(
            "SELECT COUNT(*) FROM changes WHERE request_id = 'slot-mixed'").fetchone()
        require(row[0] == 0, 'blocked batch must not consume its request number')
    task_c = open_task(workspace, reel.reel_id)
    require(task_c.version == 3, 'blocked batch must not move the current version')
    require(values(read_next(workspace, task_c.task_id, 4)) == EXPECTED['readings']['3'],
            'a new task should read the complete version 3 with block 1 still 18')

    require(values(read_next(workspace, task_a.task_id, 1)) == [25],
            'task A should read 25 at block 2 and free the base slot')
    receipt = submit(workspace, reel, 'mixed')
    require(receipt.version == 4, 'the original request number should retry into version 4')
    request_id, parent_version, replacements = fixture_request(reel, 'mixed')
    require(amend(workspace, reel.reel_id, request_id, parent_version,
                  list(reversed(replacements))) == receipt,
            'reordered replacements should reclaim the same receipt')

    workspace = open_archive(root)
    require(values(read_next(workspace, task_a.task_id, 4)) == [31], 'task A tail mismatch')
    require(values(read_next(workspace, task_b.task_id, 4)) == EXPECTED['readings']['2'],
            'task B fixed version 2 mismatch')
    task_d = open_task(workspace, reel.reel_id)
    require(task_d.version == 4, 'task D should bind version 4')
    require(values(read_next(workspace, task_d.task_id, 4)) == EXPECTED['readings']['4'],
            'task D fixed version 4 mismatch')
    reports = {}
    for task in (task_a, task_b, task_c, task_d):
        sealed = report(workspace, task_id=task.task_id, threshold=FIXTURE['threshold'])
        require([hit['block_no'] for hit in sealed['hits']]
                == EXPECTED['hit_blocks'][str(task.version)],
                f'wrong threshold hits for version {task.version}')
        reports[task.version] = sealed
    for task in (task_a, task_b, task_c, task_d):
        finish(workspace, task.task_id)

    workspace = open_archive(root)
    for sealed in reports.values():
        require(report(workspace, report_id=sealed['report_id']) == sealed,
                'a sealed report changed after reopen')
    require_media(workspace, reel, (12, 18, 27, 31), (13, 19, 26))
    return {'blocked': list(blocked), 'retry_version': receipt.version,
            'reports': [reports[key]['report_id'] for key in sorted(reports)]}


def round_shared_slot(root):
    """Sample step 3: an equal candidate shares the base slot; capacity checked."""
    workspace, reel, task_a = seed_round(root)
    submit(workspace, reel, 'tail')
    task_b = open_task(workspace, reel.reel_id)
    submit(workspace, reel, 'head')
    events = []
    receipt = submit(workspace, reel, 'return', checkpoint=events.append)
    require(receipt.version == 4, 'return should publish version 4')
    require(events == ['publication_committed'],
            'equal bytes must share the base slot without any payload write')
    task = open_task(workspace, reel.reel_id)
    require(task.version == 4, 'a new task should bind version 4')
    require(values(read_next(workspace, task.task_id, 4)) == EXPECTED['return_readings'],
            'return readings mismatch')
    require(values(read_next(workspace, task_b.task_id, 4)) == EXPECTED['readings']['2'],
            'task B must still read 26 at block 2')
    require_media(workspace, reel, (12, 18, 25, 31), (13, 0, 26))
    return {'return_version': receipt.version, 'checkpoints': events}


def run_worker(root, request_id, parent_version, replacements, target):
    payload = json.dumps([[block_no, payload.hex()] for block_no, payload in replacements])
    command = [sys.executable, '-B', str(ROOT / 'check_slot_reuse.py'), '_worker',
               str(root), request_id, str(parent_version), payload, target]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45)
    require(result.returncode == CHECKPOINT_EXIT,
            f'worker exited {result.returncode}: {result.stdout}{result.stderr}')
    require(('CHECKPOINT ' + target) in result.stdout.splitlines(),
            f'worker missed checkpoint {target}')


def round_checkpoint_crash(root):
    """Sample step 4: candidate and publication crashes on a block needing reuse."""
    workspace, reel, task_a = seed_round(root)
    submit(workspace, reel, 'tail')
    task_b = open_task(workspace, reel.reel_id)
    submit(workspace, reel, 'head')
    require(values(read_next(workspace, task_a.task_id, 1)) == [25],
            'task A should read 25 at block 2')
    width = reel.layout.block_width
    solo = [(2, (27).to_bytes(width, 'little', signed=True))]

    run_worker(root, 'slot-solo', 3, solo, 'first_payload_synced')
    workspace = open_archive(root)
    probe = open_task(workspace, reel.reel_id)
    require(probe.version == 3, 'candidate crash must not publish a new version')
    require(values(read_next(workspace, probe.task_id, 4)) == [13, 18, 26, 31],
            'complete version 3 must stay readable after the candidate crash')
    require_media(workspace, reel, (12, 18, 27, 31), (13, 0, 26))

    run_worker(root, 'slot-solo', 3, solo, 'publication_committed')
    workspace = open_archive(root)
    probe = open_task(workspace, reel.reel_id)
    require(probe.version == 4, 'committed crash should publish version 4')
    require(values(read_next(workspace, probe.task_id, 4)) == [13, 18, 27, 31],
            'a new task should read version 4 after the publication crash')
    require(values(read_next(workspace, task_b.task_id, 4)) == [12, 18, 26, 31],
            'task B must keep reading version 2')
    receipt = amend(workspace, reel.reel_id, 'slot-solo', 3, solo)
    require((receipt.request_id, receipt.version) == ('slot-solo', 4),
            'same parameters should reclaim the original receipt')
    try:
        amend(workspace, reel.reel_id, 'slot-solo', 3,
              [(2, (28).to_bytes(width, 'little', signed=True))])
    except ValueError as error:
        require('different inputs' in str(error), f'unexpected rejection: {error}')
    else:
        raise RuntimeError('same request number with different parameters must be rejected')
    require_media(workspace, reel, (12, 18, 27, 31), (13, 0, 26))
    return {'solo_version': receipt.version}


def main():
    summaries = {}
    with TemporaryDirectory(prefix='scan-reel-slot-retry-') as root:
        summaries['freed_slot_retry'] = round_blocked_retry(root)
    with TemporaryDirectory(prefix='scan-reel-slot-share-') as root:
        summaries['shared_slot'] = round_shared_slot(root)
    with TemporaryDirectory(prefix='scan-reel-slot-crash-') as root:
        summaries['checkpoint_crash'] = round_checkpoint_crash(root)
    print('SLOT REUSE SUMMARY ' + json.dumps(summaries, ensure_ascii=False, sort_keys=True))
    return 0


def _worker(arguments):
    _, root, request_id, parent_version, payload, target = arguments
    replacements = [(block_no, bytes.fromhex(hexed))
                    for block_no, hexed in json.loads(payload)]
    reel_id = load_reel(ROOT / FIXTURE['source'])[0]
    workspace = open_archive(root)

    def checkpoint(event):
        if event == target:
            print('CHECKPOINT ' + event, flush=True)
            os._exit(CHECKPOINT_EXIT)

    amend(workspace, reel_id, request_id, int(parent_version), replacements,
          checkpoint=checkpoint)
    print(f'worker returned before checkpoint {target!r}', flush=True)
    return 1


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_worker':
        raise SystemExit(_worker(sys.argv[1:]))
    raise SystemExit(main())
