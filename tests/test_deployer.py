from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from examples.support import DemoProject
from safe_sqlite_deploy.config import load_config
from safe_sqlite_deploy.deployer import Deployer
from safe_sqlite_deploy.util import DeployError, atomic_json, deployment_lock

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.project=DemoProject(self.temp.name)
        self.p=self.project

    def tearDown(self):
        self.p.stop()
        self.temp.cleanup()

    def test_clean_source_preflight_does_not_touch_database_or_start_service(self):
        before=self.p.database.read_bytes()
        result=self.p.deployer.check()
        self.assertEqual(result['revision'],self.p.revision)
        self.assertEqual(self.p.database.read_bytes(),before)
        self.assertFalse(self.p.state.exists())
        (self.p.repo/'untracked.txt').write_text('uncommitted')
        with self.assertRaises(DeployError) as error:self.p.deployer.deploy()
        self.assertEqual(error.exception.code,'DIRTY_SOURCE')

    def test_release_rejects_symlinks_and_tracked_environment(self):
        (self.p.repo/'external').symlink_to('/etc/passwd')
        self.p.commit('symlink')
        with self.assertRaises(DeployError) as error:self.p.deployer.check()
        self.assertEqual(error.exception.code,'UNSUPPORTED_SOURCE')
        (self.p.repo/'external').unlink();(self.p.repo/'.env').write_text('DEMO_ONLY=not-a-secret')
        self.p.commit('tracked environment')
        with self.assertRaises(DeployError) as error:self.p.deployer.check()
        self.assertEqual(error.exception.code,'TRACKED_ENV')

    def test_successful_additive_upgrade_preserves_live_data_and_backup(self):
        first=self.p.deployer.deploy();self.p.note('existing')
        (self.p.repo/'migrations/002_extra.sql').write_text('ALTER TABLE notes ADD COLUMN extra TEXT;')
        revision=self.p.commit('add nullable column');second=self.p.deployer.deploy()
        self.assertEqual(self.p.get('/health')['revision'],revision)
        self.assertEqual(self.p.get('/notes')[0]['text'],'existing')
        with sqlite3.connect(second['backup']) as db:
            self.assertEqual(db.execute('SELECT text FROM notes').fetchone()[0],'existing')
            self.assertNotIn('extra',[r[1] for r in db.execute('PRAGMA table_info(notes)')])
        self.assertEqual(self.p.deployer.deploy()['status'],'unchanged')
        self.assertFalse((self.p.state/'journal.json').exists())
        self.assertEqual(self.p.git('status','--porcelain'),'')

    def test_failed_tests_never_activate(self):
        self.p.deployer.deploy();before=self.p.get('/health')
        (self.p.repo/'test_app.py').write_text("raise RuntimeError('deliberate test failure')\n")
        self.p.commit('bad tests')
        with self.assertRaises(DeployError) as error:self.p.deployer.deploy()
        self.assertEqual(error.exception.code,'COMMAND_FAILED')
        self.assertEqual(self.p.get('/health'),before)
        self.assertFalse((self.p.state/'journal.json').exists())

    def test_old_runtime_read_write_contract_is_mandatory(self):
        # New schema is additive, but the previous release's positional INSERT breaks.
        original=(self.p.repo/'compatibility.py').read_text()
        (self.p.repo/'compatibility.py').write_text("import os,sqlite3\nwith sqlite3.connect(os.environ['SQLITE_PATH']) as db: db.execute(\"INSERT INTO notes VALUES(999,'old-shape')\"); db.execute('DELETE FROM notes WHERE id=999')\n")
        self.p.commit('old positional insert contract')
        self.p.deployer.deploy()
        (self.p.repo/'compatibility.py').write_text(original)
        (self.p.repo/'migrations/002_extra.sql').write_text('ALTER TABLE notes ADD COLUMN extra TEXT;')
        self.p.commit('column breaks old positional insert')
        with self.assertRaises(DeployError) as error:self.p.deployer.deploy()
        self.assertEqual(error.exception.code,'COMMAND_FAILED')
        with sqlite3.connect(self.p.database) as db:self.assertEqual(len(db.execute('PRAGMA table_info(notes)').fetchall()),2)

    def test_activation_failure_rolls_back_runtime_without_losing_writes(self):
        first=self.p.deployer.deploy();self.p.note('preserve')
        (self.p.repo/'FAIL_PRODUCTION').write_text('fail only after activation')
        (self.p.repo/'migrations/002_extra.sql').write_text('ALTER TABLE notes ADD COLUMN extra TEXT;')
        self.p.commit('bad runtime')
        with self.assertRaises(DeployError) as error:self.p.deployer.deploy()
        self.assertEqual(error.exception.code,'HEALTH_FAILED')
        self.assertEqual(self.p.get('/health')['revision'],first['revision'])
        self.assertEqual(self.p.get('/notes')[0]['text'],'preserve')
        with sqlite3.connect(self.p.database) as db:self.assertIn('extra',[r[1] for r in db.execute('PRAGMA table_info(notes)')])
        reports=[json.loads(p.read_text()) for p in (self.p.state/'reports').glob('*.json')]
        self.assertTrue(any(r['phase']=='rolled_back' for r in reports))

    def test_deployment_lock_excludes_another_process(self):
        with deployment_lock(self.p.state):
            result=subprocess.run([sys.executable,'-m','safe_sqlite_deploy','--config',str(self.p.config_path),'deploy'],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('DEPLOYMENT_LOCKED',result.stderr)

    def test_interrupted_activation_requires_recovery_and_restores_previous(self):
        first=self.p.deployer.deploy();previous=self.p.deployer.current()
        (self.p.repo/'readme.txt').write_text('second release')
        self.p.commit('second');self.p.deployer.deploy();candidate=self.p.deployer.current()
        active=json.loads((self.p.state/'active.json').read_text())
        atomic_json(self.p.state/'journal.json',{'id':'interrupted-test','phase':'switching','candidate':candidate.name,'previous':previous.name,'instance':active['instance']})
        with self.assertRaises(DeployError) as error:self.p.deployer.deploy()
        self.assertEqual(error.exception.code,'RECOVERY_REQUIRED')
        self.assertEqual(Deployer(load_config(self.p.config_path)).recover()['status'],'recovered')
        self.assertEqual(self.p.get('/health')['revision'],first['revision'])

    def test_source_and_state_must_be_separate(self):
        config=json.loads(self.p.config_path.read_text());config['stateDir']=str(self.p.repo/'state')
        self.p.config_path.write_text(json.dumps(config))
        with self.assertRaises(DeployError) as error:load_config(self.p.config_path)
        self.assertEqual(error.exception.code,'UNSAFE_PATH')
