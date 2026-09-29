"""Regressions for the independent review; fixtures never use real libraries."""
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import photo_dashboard as cli
from photodash import collectors as C
from photodash.history import History
from photodash.server import LibrarySession, Sampler, Supervisor, serve
from test_safety import make_library


def interrupt():
    # Also exercises the Python 3.10 fallback without sqlite_errorcode.
    return sqlite3.OperationalError('interrupted')


class ReviewRegressions(unittest.TestCase):
    def test_census_matches_mixed_case_entity_names(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript('''
            CREATE TABLE Z_PRIMARYKEY (Z_NAME TEXT, Z_MAX INTEGER);
            INSERT INTO Z_PRIMARYKEY VALUES ('Asset', 42), ('CHANGE', 7);
            CREATE TABLE ZASSET (Z_PK INTEGER);
            INSERT INTO ZASSET VALUES (1);
            CREATE TABLE ACHANGE (Z_PK INTEGER);
            INSERT INTO ACHANGE VALUES (2);
        ''')
        rows = {r['table']: r for r in C.collect_table_census(con)}
        self.assertEqual(rows['ZASSET']['z_max'], 42)
        self.assertEqual(rows['ACHANGE']['z_max'], 7)

    def test_analysis_does_not_invent_video_eligibility_or_zero_progress(self):
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        con.executescript('''
            CREATE TABLE ZMEDIAANALYSISASSETATTRIBUTES (
                ZVIDEOCAPTIONVERSION INTEGER, ZVIDEOEMBEDDINGVERSION INTEGER,
                ZIMAGECAPTIONVERSION INTEGER, ZVALOCATIONANALYSISVERSION INTEGER);
            INSERT INTO ZMEDIAANALYSISASSETATTRIBUTES VALUES (1, 2, 1, 0), (0, 0, 1, 0);
        ''')
        result = C.collect_analysis_coverage(con)
        workers = {w['column']: w for w in result['workers']}
        self.assertEqual(result['total'], 2)
        for column in ('ZVIDEOCAPTIONVERSION', 'ZVIDEOEMBEDDINGVERSION'):
            self.assertEqual(workers[column]['done'], 1)
            self.assertIsNone(workers[column]['total'])
            self.assertIsNone(workers[column]['pct'])
        self.assertEqual(workers['ZIMAGECAPTIONVERSION']['pct'], 100)
        self.assertIsNone(workers['ZVALOCATIONANALYSISVERSION']['pct'])
        con.execute('DELETE FROM ZMEDIAANALYSISASSETATTRIBUTES')
        self.assertTrue(all(w['pct'] is None for w in C.collect_analysis_coverage(con)['workers']))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='photodash-review-tests-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib, self.db = make_library(self.root)

    def options(self, **overrides):
        return dict(history_for=lambda lib: self.root / (lib.stem + '-history.db'),
                    snapshot_mode=True, snapshot_interval=300, log_minutes=5,
                    log_interval=180, walk_fs=False, **overrides)

    def large_wal(self):
        writer = sqlite3.connect(self.db)
        writer.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA wal_autocheckpoint=0;
            CREATE TABLE padding(value BLOB);
            CREATE TABLE marker(value INTEGER);
            INSERT INTO marker VALUES (0);
        ''')
        writer.executemany('INSERT INTO padding VALUES (zeroblob(16384))', [()] * 512)
        writer.commit()
        self.addCleanup(writer.close)
        return writer

    def backup_hook(self, on_progress):
        original = C.open_readonly
        class Source:
            def __init__(self, con):
                self.con = con
            def __getattr__(self, name):
                return getattr(self.con, name)
            def backup(self, target, **kwargs):
                progress = kwargs['progress']
                def callback(status, remaining, total):
                    on_progress(status, remaining, total)
                    progress(status, remaining, total)
                kwargs['progress'] = callback
                return self.con.backup(target, **kwargs)
        return patch.object(C, 'open_readonly', side_effect=lambda *a, **kw: Source(original(*a, **kw)))

    def test_backup_finishes_with_wal_commits_between_every_chunk(self):
        writer = self.large_wal()
        snap = C.Snapshotter(self.lib, self.root / 'scratch', timeout=3)
        remaining_pages = []
        def commit(status, remaining, total):
            remaining_pages.append(remaining)
            writer.execute('UPDATE marker SET value=value+1')
            writer.commit()
        with self.backup_hook(commit):
            self.assertTrue(snap.refresh(force=True), snap.error)
        self.assertGreater(len(remaining_pages), 2)
        self.assertTrue(all(a > b for a, b in zip(remaining_pages, remaining_pages[1:])))
        con = sqlite3.connect(snap.dir / 'Photos.sqlite')
        try:
            self.assertEqual(con.execute('SELECT value FROM marker').fetchone()[0], 0)
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            con.close()
        self.assertGreater(writer.execute('SELECT value FROM marker').fetchone()[0], 1)

    def test_mid_backup_cancel_cleans_stage_and_releases_reader(self):
        writer = self.large_wal()
        snap = C.Snapshotter(self.lib, self.root / 'scratch')
        calls = []
        def cancel(status, remaining, total):
            if remaining:
                calls.append(remaining)
                snap.stop_event.set()
        with self.backup_hook(cancel):
            self.assertFalse(snap.refresh(force=True))
        self.assertTrue(calls)
        self.assertEqual(list(snap.root.iterdir()), [])
        # A leaked pinned read would prevent a truncating checkpoint.
        self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)

    def test_mid_backup_deadline_preserves_previous_copy(self):
        self.large_wal()
        snap = C.Snapshotter(self.lib, self.root / 'scratch')
        self.assertTrue(snap.refresh(force=True))
        previous = snap.dir
        real_clock = time.monotonic
        expired = [False]
        def clock():
            return real_clock() + (1000 if expired[0] else 0)
        def expire(status, remaining, total):
            if remaining:
                expired[0] = True
        with patch.object(C.time, 'monotonic', side_effect=clock), self.backup_hook(expire):
            self.assertFalse(snap.refresh(force=True))
        self.assertEqual(snap.dir, previous)
        self.assertEqual(list(snap.root.iterdir()), [previous])
        self.assertIn('time budget', snap.error)

    def test_unexpected_backup_exception_removes_stage(self):
        for exc in [KeyboardInterrupt(), RuntimeError('unexpected')]:
            with self.subTest(exception=type(exc).__name__):
                snap = C.Snapshotter(self.lib, self.root / type(exc).__name__)
                with patch.object(snap, '_backup', side_effect=exc), self.assertRaises(type(exc)):
                    snap.refresh(force=True)
                self.assertEqual(list(snap.root.iterdir()), [])

    def test_all_list_collectors_propagate_interrupts(self):
        for fn in [C.collect_queue_mix, C.collect_queue_hot_assets, C.collect_authors,
                   C.collect_recent_changes, C.collect_top_people, C.collect_top_keywords,
                   C.collect_resources, C.collect_analysis_coverage, C.collect_pk_health,
                   C.collect_table_census]:
            con = Mock()
            con.execute.side_effect = interrupt()
            with self.subTest(collector=fn.__name__), self.assertRaises(sqlite3.OperationalError):
                fn(con)

    def test_partial_list_interrupt_does_not_publish_or_record(self):
        con = Mock()
        # A real partially-consumed result uses the SQL aliases expected by the collector.
        def people_rows():
            yield {'name': 'partial', 'n': 1}
            raise interrupt()
        con.execute.return_value = people_rows()
        with self.assertRaises(sqlite3.OperationalError):
            C.collect_top_people(con)
        sampler = Sampler(self.lib, Mock())
        previous = {'buckets': [{'count': 42}]}
        sampler.state['queue_mix'] = previous
        with patch.object(C, 'collect_queue_mix', side_effect=interrupt()), \
             self.assertRaises(sqlite3.OperationalError):
            sampler.tier_medium(Mock())
        self.assertIs(sampler.state['queue_mix'], previous)
        sampler.history.record.assert_not_called()
        sampler.history.record_snapshot.assert_not_called()

    def test_real_query_can_cancel_mid_execution(self):
        stop = threading.Event()
        con = C.open_db(self.lib, stop_event=stop)
        seen = [0]
        def observe(n):
            seen[0] += 1
            if seen[0] == 500:
                stop.set()
            return n
        con.create_function('observe', 1, observe)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                con.execute('''WITH RECURSIVE n(x) AS
                    (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000000)
                    SELECT SUM(observe(x)) FROM n''').fetchone()
            self.assertGreaterEqual(seen[0], 500)
            self.assertLess(seen[0], 1000000)
        finally:
            con.close()

    def test_search_connect_error_does_not_abort_medium_tier_or_record_zero(self):
        search = self.lib / 'database' / 'search'
        search.mkdir()
        (search / 'psi.sqlite').touch()
        sampler = Sampler(self.lib, Mock())
        con = Mock()
        with patch.object(C, 'open_readonly', side_effect=sqlite3.OperationalError('cannot open')), \
             patch.object(C, 'collect_queue_mix', return_value={'buckets': []}), \
             patch.object(C, 'collect_queue_hot_assets', return_value=[]), \
             patch.object(C, 'collect_authors', return_value=[]), \
             patch.object(C, 'collect_recent_changes', return_value=[]), \
             patch.object(C, 'collect_processes', return_value={
                 'holder_count': 0, 'photos_running': False, 'unattended': False}):
            sampler.tier_medium(con)
        self.assertIn('error', sampler.state['search'])
        self.assertIn('authors', sampler.state)
        metrics = sampler.history.record.call_args.args[1]
        self.assertFalse(any(k.startswith('search.') for k in metrics))

    def test_search_interrupt_is_not_downgraded_to_unavailable(self):
        search = self.lib / 'database' / 'search'
        search.mkdir()
        (search / 'psi.sqlite').touch()
        with patch.object(C, 'open_readonly', side_effect=interrupt()), \
             self.assertRaises(sqlite3.OperationalError):
            C.collect_search_index(self.lib, Mock())

    def test_empty_history_recovers_but_partial_foreign_schema_is_rejected(self):
        for sqlite_header in (False, True):
            path = self.root / f'empty-{sqlite_header}.db'
            path.touch()
            if sqlite_header:
                con = sqlite3.connect(path)
                con.execute('PRAGMA user_version=1')
                con.close()
            history = History(path)
            try:
                history.record('live', {'count': 1})
                self.assertEqual(history.stats()['metrics'], 1)
            finally:
                history.close()
        foreign = self.root / 'foreign.db'
        con = sqlite3.connect(foreign)
        con.execute('CREATE TABLE sample(x)')
        con.close()
        with self.assertRaises(ValueError):
            History(foreign)

    def test_interrupted_schema_creation_rolls_back_and_can_retry(self):
        path = self.root / 'history.db'
        with patch('photodash.history.SCHEMA', 'BEGIN; CREATE TABLE sample(x); INVALID SQL;'), \
             self.assertRaises(sqlite3.Error):
            History(path)
        history = History(path)
        history.close()

    def test_cli_reports_snapshot_errors_without_traceback(self):
        for extra in [['--snapshot-dir', str(self.lib)], ['--once']]:
            with self.subTest(args=extra), \
                 patch('sys.argv', ['photo_dashboard.py', '--library', str(self.lib),
                                    '--snapshot-mode', *extra]), \
                 patch.object(C.Snapshotter, '_backup', side_effect=OSError('disk full')), \
                 redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(), 2)
                self.assertIn('ERROR:', err.getvalue())
                self.assertNotIn('Traceback', err.getvalue())

    def test_switch_preparation_failure_keeps_previous_session(self):
        sup = Supervisor(self.options(snapshot_dir=self.root / 'missing'))
        previous = Mock()
        previous.sampler.library = self.root / 'old.photoslibrary'
        previous.sampler.snapshot.return_value = {'status': 'ok', 'library': 'old'}
        sup.session = previous
        result = sup.switch(self.lib)
        self.assertFalse(result['ok'])
        self.assertIs(sup.session, previous)
        previous.request_stop.assert_not_called()
        state = sup.snapshot()
        self.assertEqual(state['library'], 'old')
        self.assertIn('switch_error', state)

    def test_sampler_start_failure_cleans_candidate_keeps_previous(self):
        sup = Supervisor(self.options(snapshot_dir=self.root))
        previous = Mock()
        previous.sampler.library = self.root / 'old.photoslibrary'
        sup.session = previous
        with patch.object(Sampler, 'start', side_effect=RuntimeError('thread unavailable')):
            result = sup.switch(self.lib)
        self.assertFalse(result['ok'])
        self.assertIs(sup.session, previous)
        previous.request_stop.assert_not_called()
        self.assertEqual(list(self.root.glob('photodash-snap-*')), [])

    def test_slow_preparation_does_not_block_api_reads(self):
        sup = Supervisor(self.options())
        previous = Mock()
        previous.sampler.library = self.root / 'old'
        previous.sampler.snapshot.return_value = {'status': 'ok'}
        previous.history.series.return_value = {'cached': []}
        sup.session = previous
        entered, release, read_finished = threading.Event(), threading.Event(), threading.Event()
        def fail_after_wait(lib):
            entered.set()
            release.wait(3)
            raise OSError('setup failed')
        def read():
            sup.snapshot()
            sup.series([], 60)
            sup.events()
            sup.snapshot_history('queue_mix')
            read_finished.set()
        with patch.object(sup, '_prepare', side_effect=fail_after_wait):
            switcher = threading.Thread(target=lambda: sup.switch(self.lib))
            switcher.start()
            reader = threading.Thread(target=read)
            try:
                self.assertTrue(entered.wait(2))
                reader.start()
                self.assertTrue(read_finished.wait(1), 'API blocked behind preparation')
            finally:
                release.set()
                switcher.join(3)
                if reader.ident is not None:
                    reader.join(3)

    def test_first_backup_is_async_and_shutdown_waits_for_cleanup(self):
        sup = Supervisor(self.options(snapshot_dir=self.root))
        entered, release = threading.Event(), threading.Event()
        def slow_refresh(snap, force=False):
            entered.set()
            release.wait(3)
            return False
        with patch.object(C.Snapshotter, 'refresh', slow_refresh), \
             patch.object(Sampler, '_log_loop'):
            started = threading.Event()
            starter = threading.Thread(target=lambda: (sup.start(self.lib), started.set()))
            starter.start()
            shutdown = None
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(started.wait(1), 'start waited for initial backup')
                self.assertEqual(sup.snapshot()['status'], 'starting')
                scratch = sup.session.snapshotter.root
                sampler = sup.session.sampler
                shutdown = threading.Thread(target=sup.shutdown)
                shutdown.start()
                self.assertTrue(sampler.stop_event.wait(2))
                self.assertTrue(scratch.exists())
                self.assertTrue(shutdown.is_alive())
            finally:
                release.set()
                starter.join(3)
                if shutdown:
                    shutdown.join(3)
                else:
                    sup.shutdown()
            self.assertFalse(shutdown.is_alive())
            self.assertFalse(scratch.exists())

    def test_successful_switch_keeps_api_available_while_old_session_stops(self):
        sup = Supervisor(self.options(snapshot_dir=self.root))
        release_old = threading.Event()
        def sample_loop(sampler):
            if sampler.library == self.lib:
                release_old.wait(3)
            else:
                sampler.stop_event.wait(3)
        other = self.root / 'Other.photoslibrary'
        (other / 'database').mkdir(parents=True)
        (other / 'database' / 'Photos.sqlite').write_bytes(self.db.read_bytes())
        with patch.object(Sampler, '_log_loop'), patch.object(Sampler, '_sample_loop', sample_loop):
            sup.start(self.lib)
            old = sup.session
            old_scratch = old.snapshotter.root
            try:
                result = sup.switch(other)
                self.assertTrue(result['ok'])
                self.assertEqual(sup.snapshot()['library'], str(other))
                self.assertTrue(old.sampler.stop_event.is_set())
                self.assertTrue(old_scratch.exists(), 'scratch deleted while old reader active')
                self.assertTrue(old.sampler.is_alive())
            finally:
                release_old.set()
                sup.shutdown()
            self.assertEqual(list(self.root.glob('photodash-snap-*')), [])

    def test_ctrl_c_serve_path_cleans_snapshot_scratch(self):
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with patch('photodash.server.ThreadingHTTPServer', return_value=server), \
             patch.object(Sampler, '_log_loop'), \
             patch.object(Sampler, '_sample_loop', lambda s: s.stop_event.wait(3)), \
             redirect_stdout(io.StringIO()):
            serve(self.lib, lambda _: self.root / 'history.db', '127.0.0.1', 8787,
                  5, 180, False, True, 300, self.root)
        server.server_close.assert_called_once()
        self.assertEqual(list(self.root.glob('photodash-snap-*')), [])


if __name__ == '__main__':
    unittest.main()
