"""Execute the panel tasks in a disposable container; substitute only network packages/artifacts and systemd.

The real Ansible modules, account/ownership migration, registry parsing, password handling and deployment state
run unchanged. The installer/manager fixtures check their CLI contract; real web serving is covered upstream and
by tests/e2e/run.sh. Never run this on a host: it owns the standard REST paths inside the test container.
"""
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CALLS = Path('/tmp/rest-test-calls.jsonl')
SECRET = 'synthetic-migration-password-ONLY-TEST'
INSTALLER = '''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path('/root/ispconfig-rest-ansible/db-admin-password')
assert '--db-admin-pass-file' in sys.argv and p.stat().st_mode & 0o777 == 0o600
assert p.stat().st_uid == 0 and p.read_text().strip() == 'synthetic-migration-password-ONLY-TEST'
assert 'ISPC_REST_DB_ADMIN_PASS' not in os.environ
assert os.environ['ISPC_REST_USER']=='ispconfig-rest'
with open('/tmp/rest-test-calls.jsonl','a') as f:f.write(json.dumps(['installer',sys.argv[1:]])+'\\n')
if os.environ.get('REST_TEST_FAIL')=='yes':
 print(p.read_text());sys.exit(23)
root=pathlib.Path(os.environ['ISPC_REST_DIR']);root.mkdir(exist_ok=True)
if not (root/'.env').exists():(root/'.env').write_text('APP_KEY=synthetic-preserved-key\\n')
import pwd
user=pwd.getpwnam('ispconfig-rest')
for item in [root,root/'.env']:os.chown(item,user.pw_uid,user.pw_gid)
(root/'.env').chmod(0o600)
state=pathlib.Path('/etc/ispconfig-rest');state.mkdir(exist_ok=True)
conf=state/'install.conf'
old=conf.read_text() if conf.exists() else ''
extensions=[x for x in old.splitlines() if x.startswith(('EXTENSIONS=', 'EXTENSION_'))]
args=sys.argv[1:]
for i,arg in enumerate(args):
 if arg=='--extension':
  name,ref=args[i+1].split('@')
  assert name=='hosting' and not extensions, 'Already installed extension was passed to installer'
  extensions=['EXTENSIONS="hosting"','EXTENSION_HOSTING_REF="'+ref+'"']
conf.write_text('INSTALL_DIR="'+str(root)+'"\\nRUN_USER="ispconfig-rest"\\nBRANCH="'+os.environ['ISPC_REST_BRANCH']+'"\\n'+ '\\n'.join(extensions)+'\\n')
conf.chmod(0o600)
import shutil
shutil.copyfile('/tmp/rest-fixtures/manager','/usr/local/bin/ispconfig-rest');os.chmod('/usr/local/bin/ispconfig-rest',0o755)
'''
MANAGER = '''#!/usr/bin/env python3
import json,pathlib,re,sys
with open('/tmp/rest-test-calls.jsonl','a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='extension:update':
 p=pathlib.Path('/etc/ispconfig-rest/install.conf');s=p.read_text()
 assert 'RUN_USER="ispconfig-rest"' in s
 ref=sys.argv[-1] if '--tag' in sys.argv or '--branch' in sys.argv else 'main'
 p.write_text(re.sub(r'EXTENSION_HOSTING_REF="[^"]+"','EXTENSION_HOSTING_REF="'+ref+'"',s))
'''


@unittest.skipUnless(os.geteuid()==0 and os.environ.get('ISPC_REST_TEST_CONTAINER')=='1', 'disposable root container only')
class DeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix='rest-playbook-'))
        cls.role = cls.work/'roles/ispconfig_panel'
        shutil.copytree(ROOT/'roles/ispconfig_panel',cls.role)
        fixtures=Path('/tmp/rest-fixtures');fixtures.mkdir(exist_ok=True)
        (fixtures/'installer').write_text("#!/bin/bash\nexec python3 - \"$@\" <<'PY'\n"+INSTALLER+"\nPY\n")
        (fixtures/'manager').write_text(MANAGER)
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(fixtures),**kwargs)
            def log_message(self,*args):pass
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        tasks=yaml.safe_load((cls.role/'tasks/rest.yml').read_text())
        def adapt(tasks):
            for task in tasks:
                if 'ansible.builtin.apt' in task:
                    task.pop('ansible.builtin.apt');task['ansible.builtin.debug']={'msg':'PHP packages supplied by fixture'}
                if 'ansible.builtin.get_url' in task:
                    module=task['ansible.builtin.get_url'];kind='manager' if module['dest']=='/usr/local/bin/ispconfig-rest' else 'installer'
                    module['url']='http://127.0.0.1:'+str(cls.http.server_port)+'/'+kind
                    module['checksum']='sha256:'+hashlib.sha256((fixtures/kind).read_bytes()).hexdigest()
                if task.get('name')=='Run the ispconfig-rest installer':task['environment']['REST_TEST_FAIL']="{{ rest_test_fail | default('no') }}"
                for key in ['block','always']:adapt(task.get(key,[]))
        adapt(tasks)
        (cls.role/'tasks/rest.yml').write_text(yaml.safe_dump(tasks,sort_keys=False))
        play=[{'hosts':'localhost','gather_facts':False,'vars':{'ispconfig_php_tools':'8.3','ispconfig_httpd':'nginx'},'tasks':[
          {'ansible.builtin.include_role':{'name':'ispconfig_panel','tasks_from':'rest'}},
          {'ansible.builtin.include_role':{'name':'ispconfig_panel','tasks_from':'server_tools'}}]}]
        (cls.work/'play.yml').write_text(yaml.safe_dump(play))
        Path('/usr/local/bin/systemctl').write_text('#!/bin/sh\nexit 0\n');Path('/usr/local/bin/systemctl').chmod(0o755)
        Path('/etc/mysql').mkdir(exist_ok=True)
        Path('/etc/mysql/debian.cnf').write_text('[client]\npassword = '+SECRET+'\n')

    @classmethod
    def tearDownClass(cls):cls.http.shutdown();shutil.rmtree(cls.work)

    def setUp(self):
        for p in ['/opt/ispconfig-rest','/etc/ispconfig-rest','/root/ispconfig-rest-ansible']:
            shutil.rmtree(p,ignore_errors=True)
        Path('/usr/local/bin/ispconfig-rest').unlink(missing_ok=True)
        CALLS.unlink(missing_ok=True)

    def run_play(self,values=None,success=True):
        extra=self.work/'extra.json';extra.write_text(json.dumps(values or {}))
        result=subprocess.run(['ansible-playbook','-i','localhost,','-c','local',str(self.work/'play.yml'),'-e','@'+str(extra)],
            cwd=self.work,env=dict(os.environ,ANSIBLE_NOCOLOR='1',ANSIBLE_LOCAL_TEMP='/tmp/rest-ansible-local'),capture_output=True,text=True)
        self.assertNotIn(SECRET,result.stdout+result.stderr)
        self.assertEqual(result.returncode==0,success,result.stdout[-12000:]+result.stderr)
        self.assertFalse(Path('/root/ispconfig-rest-ansible/db-admin-password').exists())
        return result

    def calls(self):return [json.loads(x) for x in CALLS.read_text().splitlines()] if CALLS.exists() else []

    def test_fresh_install_repeat_and_hosting_release_refresh(self):
        self.run_play()
        calls=self.calls()
        self.assertEqual(len([x for x in calls if x[0]=='installer']),1)
        self.assertIn('--extension',calls[0][1])
        self.assertEqual(len([x for x in calls if x[0]=='server-tools']),1)
        self.run_play()
        self.assertEqual(len([x for x in self.calls() if x[0]=='installer']),1)
        self.assertEqual(len([x for x in self.calls() if x[0]=='server-tools']),1)
        self.run_play({'ispconfig_rest_extensions':['hosting@v1.1.5']})
        self.assertIn(['extension:update','hosting','--tag','v1.1.5'],self.calls())
        self.assertEqual(len([x for x in self.calls() if x[0]=='server-tools']),2)
        self.assertNotIn('--extension',[x for x in self.calls() if x[0]=='installer'][-1][1])

    def test_legacy_identity_migrates_and_preserves_key(self):
        self.run_play()
        import pwd
        user=pwd.getpwnam('www-data')
        root=Path('/opt/ispconfig-rest')
        for p in [root,root/'.env']:os.chown(p,user.pw_uid,user.pw_gid)
        conf=Path('/etc/ispconfig-rest/install.conf')
        conf.write_text(conf.read_text().replace('RUN_USER="ispconfig-rest"','RUN_USER="www-data"'))
        # Even an unchanged deployment marker must not hide a drift back to the shared identity.
        self.run_play()
        self.assertEqual((root/'.env').read_text(),'APP_KEY=synthetic-preserved-key\n')
        self.assertEqual(root.stat().st_uid,pwd.getpwnam('ispconfig-rest').pw_uid)
        self.assertIn('RUN_USER="ispconfig-rest"',conf.read_text())

    def test_failed_installer_cleans_secret_and_does_not_record_success(self):
        self.run_play({'rest_test_fail':'yes'},success=False)
        self.assertFalse(Path('/etc/ispconfig-rest/ansible-deployment.json').exists())
        self.run_play()
        self.assertTrue(Path('/etc/ispconfig-rest/ansible-deployment.json').exists())

    def test_old_release_and_shared_runtime_are_rejected(self):
        for values in [{'ispconfig_rest_version':'v1.4.0'},{'ispconfig_rest_extensions':['hosting@v1.1.3']},{'ispconfig_rest_user':'www-data'}]:
            with self.subTest(values=values):self.run_play(values,success=False)
        self.assertFalse(CALLS.exists())

    def test_failed_upgrade_keeps_last_success_and_retries(self):
        self.run_play()
        marker=Path('/etc/ispconfig-rest/ansible-deployment.json')
        previous=marker.read_text()
        self.run_play({'ispconfig_rest_version':'v1.4.2','rest_test_fail':'yes'},success=False)
        self.assertEqual(marker.read_text(),previous)
        self.run_play({'ispconfig_rest_version':'v1.4.2'})
        self.assertEqual(json.loads(marker.read_text())['version'],'v1.4.2')


if __name__=='__main__':unittest.main()
