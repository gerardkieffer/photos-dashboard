"""Safety regressions using only synthetic databases, never real Photos libraries.

Run with: python3 -m unittest -v test_safety
"""
import hashlib
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from email.message import Message
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import photo_dashboard as cli
import test_readonly
from photodash import collectors as C
from photodash.history import History
from photodash.server import Handler, LibrarySession, Sampler, Supervisor, serve


def make_library(root):
    lib = root / 'Fixture.photoslibrary'
    db = lib / 'database' / 'Photos.sqlite'
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript('''
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES (1, 'Asset', 1);
        CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY);
        INSERT INTO ZASSET VALUES (1);
    ''')
    con.close()
    return lib, db


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='photodash-regression-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib, self.db = make_library(self.root)

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).digest()

    def test_history_rejects_library_and_symlink_without_writes(self):
        before = self.digest(self.db)
        alias = self.root / 'alias'
        alias.symlink_to(self.lib, target_is_directory=True)
        for dest in [self.db, self.lib / 'new.db', alias / 'new.db']:
            with self.subTest(dest=dest), self.assertRaises(ValueError):
                History(dest)
        self.assertEqual(before, self.digest(self.db))
        self.assertFalse((self.lib / 'new.db').exists())

    def test_history_rejects_unrelated_and_hardlinked_databases(self):
        dest = self.root / 'unrelated.db'
        dest.write_bytes(self.db.read_bytes())
        before = self.digest(dest)
        with self.assertRaises(ValueError):
            History(dest)
        self.assertEqual(before, self.digest(dest))
        alias = self.root / 'hardlink.db'
        alias.hardlink_to(self.db)
        with self.assertRaises(ValueError):
            History(alias)

    def test_history_rejects_renamed_library(self):
        renamed = self.root / 'renamed'
        self.lib.rename(renamed)
        with self.assertRaises(ValueError):
            History(renamed / 'new.db', renamed)

    def test_history_create_and_reopen(self):
        dest = self.root / 'history.db'
        h = History(dest)
        h.record('live', {'example': 42})
        h.close()
        h = History(dest)
        try:
            self.assertEqual(h.stats()['metrics'], 1)
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        finally:
            h.close()

    def test_read_guards_and_pragma_authorizer_independently(self):
        ro = C.open_db(self.lib)
        try:
            for sql in ['DELETE FROM ZASSET', 'CREATE TABLE nope(x)',
                        'PRAGMA user_version=42', 'PRAGMA journal_mode=DELETE',
                        "ATTACH ':memory:' AS other"]:
                with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                    ro.execute(sql)
            self.assertEqual(ro.execute('SELECT COUNT(*) FROM ZASSET').fetchone()[0], 1)
            self.assertTrue(ro.execute('PRAGMA table_info(ZASSET)').fetchall())
        finally:
            ro.close()
        # The authorizer alone must block writable PRAGMAs, even on a writable handle.
        con = sqlite3.connect(':memory:')
        try:
            con.set_authorizer(C._readonly_authorizer)
            with self.assertRaises(sqlite3.Error):
                con.execute('PRAGMA user_version=42')
        finally:
            con.close()

    def test_uri_special_characters(self):
        special = self.root / 'Library?#.photoslibrary'
        self.lib.rename(special)
        con = C.open_db(special)
        try:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM ZASSET').fetchone()[0], 1)
        finally:
            con.close()

    def test_query_budget_and_cancellation(self):
        con = C.open_db(self.lib, query_timeout=0.001)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                C.scalar(con, '''WITH RECURSIVE n(x) AS
                    (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100000000)
                    SELECT SUM(x) FROM n''')
        finally:
            con.close()

    def test_pk_check_uses_consistent_transaction(self):
        writer = sqlite3.connect(self.db)
        writer.execute('PRAGMA journal_mode=WAL')
        reader = C.open_db(self.lib)
        class Interleaved:
            in_transaction = False
            def execute(inner, sql, *args):
                if sql.startswith('SELECT COALESCE(MAX'):
                    writer.executescript('BEGIN; INSERT INTO ZASSET VALUES(2); '
                                         'UPDATE Z_PRIMARYKEY SET Z_MAX=2; COMMIT;')
                return reader.execute(sql, *args)
            def rollback(inner):
                reader.rollback()
        try:
            self.assertTrue(C.collect_pk_health(Interleaved())['ok'])
            self.assertFalse(reader.in_transaction)
        finally:
            reader.close()
            writer.close()

    def test_pk_check_preserves_callers_transaction(self):
        con = C.open_db(self.lib)
        try:
            con.execute('BEGIN')
            self.assertTrue(C.collect_pk_health(con)['ok'])
            self.assertTrue(con.in_transaction)
        finally:
            con.close()

    def test_snapshot_includes_uncheckpointed_wal_preserves_source(self):
        writer = sqlite3.connect(self.db)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('INSERT INTO ZASSET VALUES(2)')
        writer.commit()
        wal = self.db.with_name(self.db.name + '-wal')
        before = [self.digest(p) for p in (self.db, wal)]
        snap = C.Snapshotter(self.lib, self.root / 'scratch')
        try:
            self.assertTrue(snap.refresh(force=True), snap.error)
            con = sqlite3.connect(snap.dir / 'Photos.sqlite')
            try:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM ZASSET').fetchone()[0], 2)
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                con.close()
            self.assertEqual(before, [self.digest(p) for p in (self.db, wal)])
        finally:
            snap.cleanup()
            writer.close()

    def test_snapshot_failure_keeps_previous_generation(self):
        snap = C.Snapshotter(self.lib, self.root / 'scratch')
        self.assertTrue(snap.refresh(force=True))
        old = snap.dir
        before = self.digest(old / 'Photos.sqlite')
        def fail(src, dst, deadline):
            dst.write_bytes(b'incomplete')
            raise OSError('disk full')
        with patch.object(snap, '_backup', side_effect=fail):
            self.assertFalse(snap.refresh(force=True))
        self.assertEqual(snap.dir, old)
        self.assertEqual(before, self.digest(old / 'Photos.sqlite'))
        self.assertEqual(list(snap.root.iterdir()), [old])
        self.assertIn('disk full', snap.error)
        self.assertFalse(snap.refresh())  # back off after a failed attempt

    def test_snapshot_cancellation_publishes_nothing(self):
        snap = C.Snapshotter(self.lib, self.root / 'scratch')
        snap.stop_event.set()
        self.assertFalse(snap.refresh(force=True))
        self.assertFalse(snap.dir.exists())
        self.assertEqual(snap.last_copy, 0)

    def test_snapshot_rejects_unsafe_scratch(self):
        with self.assertRaises(ValueError):
            C.Snapshotter(self.lib, self.lib / 'scratch')
        occupied = self.root / 'occupied'
        occupied.mkdir()
        (occupied / 'keep.txt').write_text('keep')
        with self.assertRaises(ValueError):
            C.Snapshotter(self.lib, occupied)
        self.assertTrue((occupied / 'keep.txt').exists())

    def test_snapshot_startup_does_not_probe_original(self):
        with patch.object(C, 'readable', side_effect=AssertionError('direct probe')):
            self.assertEqual(cli.pick_library(self.lib, True), self.lib)

    def test_snapshot_once_passes_complete_copy_to_collectors(self):
        def collect(lib, dbdir):
            self.assertEqual(lib, self.lib)
            self.assertNotEqual(dbdir, self.lib / 'database')
            con = C.open_db(lib, dbdir)
            con.close()
            return 0
        with patch.object(cli, '_cmd_once', side_effect=collect) as fn:
            self.assertEqual(cli.cmd_once(self.lib, True), 0)
            fn.assert_called_once()

    def test_cli_routes_snapshot_once(self):
        with patch('sys.argv', ['photo_dashboard.py', '--library', str(self.lib),
                               '--snapshot-mode', '--once']), \
             patch.object(cli, 'cmd_once', return_value=0) as once:
            self.assertEqual(cli.main(), 0)
            once.assert_called_once_with(self.lib, True, None)

    def test_filesystem_cancellation(self):
        (self.lib / 'originals').mkdir()
        (self.lib / 'originals' / 'test.jpg').touch()
        event = threading.Event()
        event.set()
        with self.assertRaises(InterruptedError):
            C.collect_filesystem(self.lib, event)

    def test_shutdown_does_not_close_live_workers_resources(self):
        release = threading.Event()
        sampler = threading.Thread(target=lambda: release.wait(3))
        sampler.stop_event = threading.Event()
        sampler.start()
        history, snapshotter = Mock(), Mock()
        session = LibrarySession(sampler, history, snapshotter)
        cleanup = threading.Thread(target=session.close)
        cleanup.start()
        try:
            self.assertTrue(sampler.stop_event.wait(2))
            history.close.assert_not_called()
            snapshotter.cleanup.assert_not_called()
        finally:
            release.set()
            cleanup.join(3)
        self.assertFalse(cleanup.is_alive())
        history.close.assert_called_once()
        snapshotter.cleanup.assert_called_once()

    def test_slow_filesystem_worker_does_not_block_live_sampling(self):
        sampler = Sampler(self.lib, Mock(), walk_fs=True)
        entered, release, sampled = threading.Event(), threading.Event(), threading.Event()
        def slow_fs(con):
            entered.set()
            release.wait(3)
        def live(con):
            sampled.set()
        with patch.object(sampler, '_log_loop'), \
             patch.object(sampler, 'tier_rare', side_effect=slow_fs), \
             patch.object(sampler, 'tier_live', side_effect=live), \
             patch.object(sampler, 'tier_medium'), patch.object(sampler, 'tier_slow'):
            sampler.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(sampled.wait(2))
                self.assertFalse(release.is_set())
            finally:
                sampler.stop_event.set()
                release.set()
                sampler.join(3)
            self.assertFalse(sampler.is_alive())

    def test_non_loopback_bind_rejected_before_reading_library(self):
        with self.assertRaises(ValueError):
            serve(self.lib, Mock(), '0.0.0.0', 8787, 5, 180, False)

    def test_optional_readonly_suite_only_attempts_writes_on_copy(self):
        con = sqlite3.connect(self.db)
        for column in ['ZDATECREATED', 'ZTRASHEDSTATE', *[f'EXTRA{i}' for i in range(10)]]:
            con.execute(f'ALTER TABLE ZASSET ADD COLUMN {column} INTEGER')
        con.commit()
        con.close()
        original = test_readonly.test_enforcement
        def check_copy(lib):
            self.assertNotEqual(lib.resolve(), self.lib.resolve())
            original(lib)
        before = self.digest(self.db)
        with patch('sys.argv', ['test_readonly.py', '--library', str(self.lib)]), \
             patch.object(test_readonly, 'test_enforcement', side_effect=check_copy), \
             patch.object(test_readonly, 'failures', 0), \
             patch.object(C, 'sh', return_value=''), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(test_readonly.main(), 0, out.getvalue())
        self.assertEqual(before, self.digest(self.db))


class HTTPTests(unittest.TestCase):
    def handler(self, path='/api/snapshot', host='localhost:8787', token='secret',
                origin=None, site=None):
        h = Handler.__new__(Handler)
        h.path = path
        h.server = SimpleNamespace(server_port=8787, api_token='secret')
        h.headers = Message()
        h.headers['Host'] = host
        if token is not None:
            h.headers['X-PhotosDashboard-Token'] = token
        if origin is not None:
            h.headers['Origin'] = origin
        if site is not None:
            h.headers['Sec-Fetch-Site'] = site
        h.sup = Mock()
        h.sup.snapshot.return_value = {'status': 'ok'}
        h._json = Mock()
        h._send = Mock()
        return h

    def test_missing_or_wrong_token_is_rejected(self):
        for token in (None, 'wrong'):
            h = self.handler(token=token)
            h.do_GET()
            self.assertEqual(h._json.call_args.args[1], 403)
            h.sup.snapshot.assert_not_called()

    def test_bad_host_origin_and_cross_site_are_rejected(self):
        for kw in ({'host': 'attacker.example:8787'},
                   {'origin': 'https://attacker.example'}, {'site': 'cross-site'},
                   {'site': 'same-site'}, {'origin': 'null'}):
            h = self.handler(**kw)
            h.do_GET()
            self.assertEqual(h._json.call_args.args[1], 403)
            h.sup.snapshot.assert_not_called()

    def test_authorized_same_origin_request(self):
        h = self.handler(origin='http://localhost:8787', site='same-origin')
        h.do_GET()
        h._json.assert_called_once_with({'status': 'ok'})

    def test_bootstrap_embeds_token_only_in_index(self):
        h = self.handler(path='/', token=None)
        h.do_GET()
        body = h._send.call_args.args[1]
        self.assertIn(b'name="dashboard-token" content="secret"', body)
        self.assertNotIn(b'__DASHBOARD_TOKEN__', body)

    def test_post_checks_content_type_and_length_before_discovery(self):
        h = self.handler('/api/select-library')
        h.headers['Content-Length'] = '1000000'
        h.headers['Content-Type'] = 'application/json'
        h.do_POST()
        self.assertEqual(h._json.call_args.args[1], 400)
        h.sup.discover.assert_not_called()

    def test_switch_uses_supervisor_discovery(self):
        h = self.handler('/api/select-library', origin='http://localhost:8787')
        body = json.dumps({'path': '/example.photoslibrary'}).encode()
        h.headers['Content-Length'] = str(len(body))
        h.headers['Content-Type'] = 'application/json'
        h.rfile = io.BytesIO(body)
        h.sup.discover.return_value = [{'path': '/example.photoslibrary'}]
        h.sup.switch.return_value = {'ok': True}
        with patch.object(C, 'discover_libraries', side_effect=AssertionError('direct discovery')):
            h.do_POST()
        h.sup.switch.assert_called_once_with(Path('/example.photoslibrary'))

    def test_static_sibling_path_rejected(self):
        h = self.handler('/static/../static-private/secret.txt')
        h.do_GET()
        self.assertEqual(h._json.call_args.args[1], 404)

    def test_query_limits_are_bounded(self):
        h = self.handler('/api/series?metrics=test&points=0&window=9999999999999')
        h.sup.series.return_value = {}
        h.do_GET()
        h.sup.series.assert_called_once_with(['test'], 400 * 86400, 1)

    def test_real_http_bootstrap_and_authenticated_api(self):
        class TestHandler(Handler):
            sup = Mock()
        TestHandler.sup.snapshot.return_value = {'status': 'ok'}
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        httpd.api_token = 'integration-token'
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        con = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
        try:
            con.request('GET', '/')
            response = con.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'content="integration-token"', response.read())
            self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
            con.request('GET', '/api/snapshot')
            response = con.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            con.request('GET', '/api/snapshot', headers={
                'X-PhotosDashboard-Token': httpd.api_token})
            response = con.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {'status': 'ok'})
        finally:
            con.close()
            httpd.shutdown()
            httpd.server_close()
            worker.join(3)


if __name__ == '__main__':
    unittest.main()
