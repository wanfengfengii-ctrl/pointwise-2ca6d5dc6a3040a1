import hashlib
import json

from archive.importer import ingest
from archive.layout import BlockLayout
from archive.workspace import work_transaction
from demo import EXAMPLES, load_reel
from reader.cursor import next as read_next
from reader.release import finish
from reader.tasks import open as open_task
from reports.issue import report
from revision.publication import amend
from revision.reuse import attached_path
from tests.support import WorkspaceCase, pairs, raw, scenario


FIXTURE = json.loads((EXAMPLES / 'slot_reuse.json').read_text(encoding='utf-8'))
EXPECTED = FIXTURE['expected']
OLD = [(0, 12), (1, 18), (2, 25), (3, 31)]


class SlotReuseTests(WorkspaceCase):
    def seed_reuse(self):
        return ingest(self.workspace, *load_reel(EXAMPLES / 'reel.json'), reuse_slots=True)

    def submit(self, reel, name, **kwargs):
        request = FIXTURE['requests'][name]
        replacements = [(block_no, raw(value)) for block_no, value in request['readings']]
        return amend(self.workspace, reel.reel_id, request['request_id'],
                     request['parent_version'], replacements, **kwargs)

    def reach_v3(self, reel):
        task_a = open_task(self.workspace, reel.reel_id)
        self.assertEqual(pairs(read_next(self.workspace, task_a.task_id, FIXTURE['a_prefix'])),
                         [(0, 12), (1, 18)])
        self.assertEqual(self.submit(reel, 'tail').version, 2)
        task_b = open_task(self.workspace, reel.reel_id)
        self.assertEqual(task_b.version, 2)
        self.assertEqual(self.submit(reel, 'head').version, 3)
        return task_a, task_b

    def test_t9_blocked_retry_and_report_reclaim(self):
        reel = self.seed_reuse()
        task_a, task_b = self.reach_v3(reel)
        blocked = self.submit(reel, 'mixed')
        self.assertEqual(blocked, tuple(EXPECTED['blocked']))
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM changes WHERE request_id = 'slot-mixed'"),
                         0, 'blocked batch must not consume its request number')
        task_c = open_task(self.workspace, reel.reel_id)
        self.assertEqual(task_c.version, 3)
        self.assertEqual(pairs(read_next(self.workspace, task_c.task_id, 4)),
                         list(enumerate(EXPECTED['readings']['3'])))
        self.assertEqual(pairs(read_next(self.workspace, task_a.task_id, 1)), [(2, 25)])
        receipt = self.submit(reel, 'mixed')
        self.assertEqual(receipt.version, 4)
        reordered = [(2, raw(27)), (1, raw(19))]
        self.assertEqual(amend(self.workspace, reel.reel_id, 'slot-mixed', 3, reordered), receipt)
        self.reopen()
        tail_a = pairs(read_next(self.workspace, task_a.task_id, 4))
        self.assertEqual(tail_a, [(3, 31)])
        readings_b = pairs(read_next(self.workspace, task_b.task_id, 4))
        self.assertEqual(readings_b, list(enumerate(EXPECTED['readings']['2'])))
        task_d = open_task(self.workspace, reel.reel_id)
        self.assertEqual(task_d.version, 4)
        readings_d = pairs(read_next(self.workspace, task_d.task_id, 4))
        self.assertEqual(readings_d, list(enumerate(EXPECTED['readings']['4'])))
        actual_readings = {
            1: [(0, 12), (1, 18), (2, 25)] + tail_a,
            2: readings_b,
            3: list(enumerate(EXPECTED['readings']['3'])),
            4: readings_d,
        }
        reports = {}
        for task in (task_a, task_b, task_c, task_d):
            sealed = report(self.workspace, task_id=task.task_id, threshold=FIXTURE['threshold'])
            self.assertEqual([hit['block_no'] for hit in sealed['hits']],
                             EXPECTED['hit_blocks'][str(task.version)])
            reports[task.version] = sealed
        for task in (task_a, task_b, task_c, task_d):
            finish(self.workspace, task.task_id)
        self.reopen()
        for sealed in reports.values():
            self.assertEqual(report(self.workspace, report_id=sealed['report_id']), sealed)
        self.assertEqual((self.workspace.media / reel.base_name).read_bytes(),
                         raw(12) + raw(18) + raw(27) + raw(31))
        self.assertEqual(attached_path(self.workspace, reel.reel_id).read_bytes(),
                         raw(13) + raw(19) + raw(26))
        media_files = sorted(path.name for path in self.workspace.media.iterdir())
        self.assertEqual(media_files, sorted([reel.base_name,
                                              attached_path(self.workspace, reel.reel_id).name]))
        scenario('t9', {'blocked': EXPECTED['blocked'], 'retry': 'slot-mixed', 'threshold': 26},
                 [EXPECTED['readings'][key] for key in ('1', '2', '3', '4')],
                 [actual_readings[key] for key in (1, 2, 3, 4)],
                 [EXPECTED['hit_blocks'][key] for key in ('1', '2', '3', '4')],
                 [[hit['block_no'] for hit in reports[key]['hits']] for key in (1, 2, 3, 4)],
                 ['revision/publication.py', 'revision/reuse.py', 'reader/blocks.py',
                  'reports/issue.py'],
                 '按两槽预算手算受阻块 2，腾槽后四版读数与阈值 26 命中均按原块号核对。')

    def test_t9_equal_candidate_shares_slot(self):
        reel = self.seed_reuse()
        task_a, task_b = self.reach_v3(reel)
        events = []
        receipt = self.submit(reel, 'return', checkpoint=events.append)
        self.assertEqual(receipt.version, 4)
        self.assertEqual(events, ['publication_committed'],
                         'equal bytes must share the base slot without a payload write')
        task = open_task(self.workspace, reel.reel_id)
        self.assertEqual(task.version, 4)
        self.assertEqual(pairs(read_next(self.workspace, task.task_id, 4)),
                         list(enumerate(EXPECTED['return_readings'])))
        self.assertEqual(pairs(read_next(self.workspace, task_b.task_id, 4)),
                         list(enumerate(EXPECTED['readings']['2'])))
        self.assertEqual((self.workspace.media / reel.base_name).read_bytes(),
                         raw(12) + raw(18) + raw(25) + raw(31))
        self.assertEqual(attached_path(self.workspace, reel.reel_id).read_bytes(),
                         raw(13) + raw(0) + raw(26),
                         'block 2 stays within its base slot and its one attached slot')

    def test_t9_candidate_and_publication_crashes(self):
        reel = self.seed_reuse()
        task_a, task_b = self.reach_v3(reel)
        self.assertEqual(pairs(read_next(self.workspace, task_a.task_id, 1)), [(2, 25)])
        solo = json.dumps([[2, raw(27).hex()]])
        self.interrupt('slot-amend', 'first_payload_synced', 'slot-solo', 3, solo)
        self.reopen()
        probe = open_task(self.workspace, reel.reel_id)
        self.assertEqual(probe.version, 3)
        self.assertEqual(pairs(read_next(self.workspace, probe.task_id, 4)),
                         [(0, 13), (1, 18), (2, 26), (3, 31)])
        self.assertEqual((self.workspace.media / reel.base_name).read_bytes(),
                         raw(12) + raw(18) + raw(27) + raw(31),
                         'staged candidate occupies the freed base slot')
        self.assertEqual(attached_path(self.workspace, reel.reel_id).read_bytes(),
                         raw(13) + raw(0) + raw(26))
        self.interrupt('slot-amend', 'publication_committed', 'slot-solo', 3, solo)
        self.reopen()
        probe = open_task(self.workspace, reel.reel_id)
        self.assertEqual(probe.version, 4)
        self.assertEqual(pairs(read_next(self.workspace, probe.task_id, 4)),
                         [(0, 13), (1, 18), (2, 27), (3, 31)])
        self.assertEqual(pairs(read_next(self.workspace, task_b.task_id, 4)),
                         [(0, 12), (1, 18), (2, 26), (3, 31)])
        receipt = amend(self.workspace, reel.reel_id, 'slot-solo', 3, [(2, raw(27))])
        self.assertEqual((receipt.request_id, receipt.version), ('slot-solo', 4))
        with self.assertRaisesRegex(ValueError, 'different inputs'):
            amend(self.workspace, reel.reel_id, 'slot-solo', 3, [(2, raw(28))])
        self.assertEqual((self.workspace.media / reel.base_name).read_bytes(),
                         raw(12) + raw(18) + raw(27) + raw(31))
        self.assertEqual(attached_path(self.workspace, reel.reel_id).read_bytes(),
                         raw(13) + raw(0) + raw(26))

    def test_t9_ordinary_reel_keeps_whole_reel_gate(self):
        reel = self.seed()
        first = open_task(self.workspace, reel.reel_id)
        self.assertEqual(self.read(first, 2), [(0, 12), (1, 18)])
        self.assertEqual(self.submit(reel, 'tail').version, 2)
        with self.assertRaisesRegex(ValueError, 'unfinalized'):
            self.submit(reel, 'head')
        self.assertEqual(self.scalar('SELECT reuse_slots FROM reels'), 0)

    def test_t9_reports_every_blocked_block(self):
        blocks = [raw(value) for value in (1, 2, 3)]
        checks = [hashlib.sha256(block).hexdigest() for block in blocks]
        reel = ingest(self.workspace, 'multi-blocked', BlockLayout(3, 4), blocks, checks,
                      reuse_slots=True)
        task_a = open_task(self.workspace, reel.reel_id)
        self.assertEqual(self.read(task_a, 1), [(0, 1)])
        self.assertEqual(amend(self.workspace, reel.reel_id, 'multi-first', 1,
                               [(1, raw(20)), (2, raw(30))]).version, 2)
        task_b = open_task(self.workspace, reel.reel_id)
        self.assertEqual(task_b.version, 2)
        self.assertEqual(amend(self.workspace, reel.reel_id, 'multi-second', 2,
                               [(0, raw(10))]).version, 3)
        blocked = amend(self.workspace, reel.reel_id, 'multi-third', 3,
                        [(1, raw(21)), (2, raw(31))])
        self.assertEqual(blocked, (1, 2), 'every block needing a third payload is reported')
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM changes WHERE request_id = 'multi-third'"),
                         0)
        probe = open_task(self.workspace, reel.reel_id)
        self.assertEqual(probe.version, 3)
        self.assertEqual(self.read(probe), [(0, 10), (1, 20), (2, 30)])
        self.assertEqual(self.read(task_a), [(1, 2), (2, 3)])
        self.assertEqual(self.read(task_b), [(0, 1), (1, 20), (2, 30)])

    def test_t9_legacy_catalogue_gains_reuse_flag(self):
        reel = self.seed()
        with work_transaction(self.workspace) as database:
            database.execute('ALTER TABLE reels DROP COLUMN reuse_slots')
        self.reopen()
        self.assertEqual(self.scalar('SELECT reuse_slots FROM reels'), 0)
        task = open_task(self.workspace, reel.reel_id)
        self.assertEqual(self.read(task), OLD)
        _reel_id, layout, blocks, checks = load_reel(EXAMPLES / 'reel.json')
        reused = ingest(self.workspace, 'coil-05', layout, blocks, checks, reuse_slots=True)
        self.assertEqual(self.scalar('SELECT reuse_slots FROM reels WHERE reel_id = ?',
                                     (reused.reel_id,)), 1)

    def test_t9_crash_then_different_candidate_rewrites_slot(self):
        reel = self.seed_reuse()
        task_a, _task_b = self.reach_v3(reel)
        self.assertEqual(pairs(read_next(self.workspace, task_a.task_id, 1)), [(2, 25)])
        solo = json.dumps([[2, raw(27).hex()]])
        self.interrupt('slot-amend', 'first_payload_synced', 'slot-solo', 3, solo)
        self.reopen()
        receipt = amend(self.workspace, reel.reel_id, 'slot-other', 3, [(2, raw(28))])
        self.assertEqual(receipt.version, 4)
        probe = open_task(self.workspace, reel.reel_id)
        self.assertEqual(self.read(probe), [(0, 13), (1, 18), (2, 28), (3, 31)])
        with self.assertRaisesRegex(ValueError, 'expected parent version 4'):
            amend(self.workspace, reel.reel_id, 'slot-solo', 3, [(2, raw(27))])
        self.assertEqual((self.workspace.media / reel.base_name).read_bytes(),
                         raw(12) + raw(18) + raw(28) + raw(31))
