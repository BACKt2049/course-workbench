import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import data_safety
import durable_json
import runtime_assets
import runtime_config
import server
import store
import ui_assets
import launcher
import browser_login


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workbench-reliability-')
        self.base = Path(self.temp.name).resolve()
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ,{'LOCALAPPDATA':str(self.base/'local'),'WB_DATA':str(self.base/'data-root')}))
        for name,value in {'ROOT':self.base/'data-root','STATE':self.base/'data-root/state',
                           'DATA':self.base/'data-root/data','RAW':self.base/'data-root/raw',
                           'LATEST':self.base/'data-root/data/latest.json'}.items():
            self.stack.enter_context(patch.object(store,name,value))
        for path in (store.ROOT,store.STATE,store.DATA,store.RAW):
            path.mkdir(parents=True,exist_ok=True)

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_corrupt_json_recovers_last_good_and_keeps_broken_file(self):
        path = store.DATA/'latest.json'
        durable_json.write(path,{'courses':[1]})
        durable_json.write(path,{'courses':[2]})
        path.write_text('{broken',encoding='utf-8')
        self.assertEqual(durable_json.read(path),{'courses':[1]})
        self.assertEqual(json.loads(path.read_text()),{'courses':[1]})
        self.assertEqual(len(list(path.parent.glob('latest.json.damaged-*'))),1)

    def test_deleted_json_recovers_from_backup(self):
        path = store.DATA/'workspace.json'
        durable_json.write(path,{'notes':['safe']})
        path.unlink()
        self.assertEqual(durable_json.read(path),{'notes':['safe']})

    def test_concurrent_json_writes_are_complete(self):
        path = store.DATA/'same.json'
        threads = [threading.Thread(target=durable_json.write,args=(path,{'value':i,'text':'x'*4000})) for i in range(12)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertEqual(len(durable_json.read(path)['text']),4000)
        self.assertFalse(list(path.parent.glob('*.tmp')))

    def test_first_use_chooses_folder_then_remembers_it(self):
        os.environ.pop('WB_DATA')
        target = self.base/'用户选择的目录'
        with patch.object(runtime_config,'pick_directory',return_value=target) as choose:
            self.assertEqual(runtime_config.prepare_desktop(),target.resolve())
            self.assertEqual(runtime_config.prepare_desktop(),target.resolve())
            choose.assert_called_once()
        self.assertTrue((target/'.workbench-settings.json').is_file())

    def test_cancel_first_use_does_not_save_configuration(self):
        os.environ.pop('WB_DATA')
        with patch.object(runtime_config,'pick_directory',return_value=None):
            self.assertIsNone(runtime_config.prepare_desktop())
        self.assertFalse(runtime_config.settings_path().exists())

    @unittest.skipUnless(os.name=='nt','Windows 原生选择器')
    def test_native_folder_dialog_structure_and_cancel(self):
        import ctypes
        with patch.object(ctypes.windll.shell32,'SHBrowseForFolderW',return_value=0):
            self.assertIsNone(runtime_config.pick_directory(self.base))

    def test_settings_corruption_recovers_selected_directory(self):
        target = runtime_config.save_directory(self.base/'selected')
        runtime_config.settings_path().write_text('broken',encoding='utf-8')
        os.environ.pop('WB_DATA')
        self.assertEqual(runtime_config.configured_root(),target)

    def test_missing_drive_never_silently_opens_empty_default(self):
        target = runtime_config.save_directory(self.base/'selected')
        for p in target.iterdir():p.unlink()
        target.rmdir()
        os.environ.pop('WB_DATA')
        with self.assertRaises(OSError):runtime_config.configured_root()

    def test_migration_preserves_source_and_does_not_overwrite_destination(self):
        source, destination = self.base/'old',self.base/'new'
        (source/'data').mkdir(parents=True)
        (destination/'data').mkdir(parents=True)
        (source/'data/file.txt').write_text('old')
        (source/'data/new.txt').write_text('new')
        (destination/'data/file.txt').write_text('existing')
        runtime_config.migrate(source,destination)
        self.assertEqual((destination/'data/file.txt').read_text(),'existing')
        self.assertEqual((destination/'data/new.txt').read_text(),'new')
        self.assertEqual((source/'data/new.txt').read_text(),'new')
        self.assertTrue(list((source/'backups').glob('*.zip')))

    def test_backups_include_data_answers_notes_and_exclude_kernel(self):
        for relative in ('data/att/file.txt','answers/user/course/hw.json','edge-profile/Default/Local Storage/leveldb/data.ldb','runtime/browser/chrome.exe'):
            path = store.ROOT/relative; path.parent.mkdir(parents=True,exist_ok=True);path.write_text('example')
        result = data_safety.backup(store.ROOT,force=True)
        with zipfile.ZipFile(result['file']) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('answers/user/course/hw.json',archive.namelist())
            self.assertIn('data/att/file.txt',archive.namelist())
            self.assertIn('edge-profile/Default/Local Storage/leveldb/data.ldb',archive.namelist())
            self.assertNotIn('runtime/browser/chrome.exe',archive.namelist())
        self.assertFalse(data_safety.backup(store.ROOT)['created'])

    def test_existing_json_gets_recovery_copy(self):
        path = store.DATA/'latest.json';path.write_text('{"courses":[1]}')
        data_safety.seed_recovery(store.ROOT)
        path.write_text('broken')
        self.assertEqual(store.read_json(path),{'courses':[1]})

    def test_runtime_kernel_repairs_truncation_and_same_size_damage(self):
        source,dest = self.base/'bundle',self.base/'runtime'
        source.mkdir();(source/'chrome.exe').write_bytes(b'good-engine')
        manifest = {'chrome.exe':hashlib.sha256(b'good-engine').hexdigest()}
        runtime_assets.browser(source,dest,manifest)
        (dest/'chrome.exe').write_bytes(b'bad-engine!')
        runtime_assets.browser(source,dest,manifest)
        self.assertEqual((dest/'chrome.exe').read_bytes(),b'good-engine')

    def test_page_assembly_survives_deleted_extraction_files(self):
        bundle = self.base/'bundle';(bundle/'web').mkdir(parents=True)
        (bundle/'app.html').write_text('<html><head></head><body>saved</body></html>')
        (bundle/'web/answers.css').write_text('body{}');(bundle/'web/answers.js').write_text('window.ok=true;')
        with patch.object(store,'WEB',bundle),patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',str(bundle),create=True),patch.object(ui_assets,'_CACHED',None):
            first = ui_assets.load_html()
            for file in bundle.rglob('*'):
                if file.is_file():file.unlink()
            self.assertEqual(ui_assets.load_html(),first)
            ui_assets._CACHED = None
            self.assertEqual(ui_assets.load_html(),first)

    def test_playwright_driver_uses_persistent_files_after_temp_loss(self):
        import playwright._impl._driver as driver
        import playwright._impl._transport as transport
        bundle,dest=self.base/'bundle',self.base/'stable-driver'
        source=bundle/'playwright/driver'
        (source/'package').mkdir(parents=True)
        (source/'node.exe').write_bytes(b'node')
        (source/'package/cli.js').write_bytes(b'cli')
        with patch.object(driver,'compute_driver_executable'),patch.object(transport,'compute_driver_executable'):
            runtime_assets.playwright_driver(bundle,dest)
            for file in source.rglob('*'):
                if file.is_file():file.unlink()
            node,cli=transport.compute_driver_executable()
            self.assertEqual(Path(node).read_bytes(),b'node')
            self.assertEqual(Path(cli).read_bytes(),b'cli')

    def test_correct_qr_instructions(self):
        self.assertIn('知到',browser_login.HINTS['zhs'][0])
        self.assertFalse("'用微信扫码'" in Path(__file__).resolve().parents[1].joinpath('app.html').read_text(encoding='utf-8'))

    def test_bootstrap_restores_connection_and_workspace_is_validated(self):
        srv = server.LocalServer(('127.0.0.1',0),server.Handler)
        host = '127.0.0.1:%s'%srv.server_port;server.ALLOWED_HOSTS.add(host)
        thread = threading.Thread(target=srv.serve_forever,daemon=True);thread.start()
        def request(path,value=None,token=True):
            req = urllib.request.Request('http://'+host+path,data=json.dumps(value).encode() if value is not None else None)
            if token:req.add_header('X-WB-Token',server.TOKEN)
            return urllib.request.urlopen(req,timeout=5)
        try:
            with request('/api/bootstrap',token=False) as response:self.assertEqual(json.load(response)['token'],server.TOKEN)
            with self.assertRaises(urllib.error.HTTPError):request('/api/workspace',token=False)
            value={'settings':{},'notes':[{'html':'my note'}],'courses':[],'ddl':[]}
            with request('/api/workspace',{'workspace':value}) as response:self.assertTrue(json.load(response)['ok'])
            with self.assertRaises(urllib.error.HTTPError):request('/api/workspace',{'workspace':{}})
            with request('/api/workspace') as response:self.assertEqual(json.load(response)['workspace'],value)
            with request('/api/storage/backup',{}) as response:self.assertTrue(json.load(response)['ok'])
        finally:
            srv.shutdown();srv.server_close();thread.join(5);server.ALLOWED_HOSTS.discard(host)

    def _response(self,value):
        result=io.BytesIO(json.dumps(value).encode());result.status=200;return result

    def test_current_healthy_service_is_reused(self):
        store.write_json(store.STATE/'server.json',{'port':12345,'token':'test'})
        health={'version':launcher.APP_VERSION,'pageReady':True,'dataDir':str(store.ROOT)}
        with patch('urllib.request.urlopen',side_effect=[self._response({'ok':True,'sites':{}}),self._response(health)]),patch.object(launcher,'retire_instance') as retire:
            self.assertEqual(launcher.existing_instance(),12345)
            retire.assert_not_called()

    def test_old_service_is_backed_up_and_retired(self):
        store.write_json(store.STATE/'server.json',{'port':12345,'token':'test'})
        with patch('urllib.request.urlopen',side_effect=[self._response({'ok':True,'sites':{}}),urllib.error.HTTPError('url',404,'old',{},None)]),patch.object(launcher,'retire_instance',return_value=True) as retire:
            self.assertIsNone(launcher.existing_instance())
            retire.assert_called_once_with(12345,'test')
        self.assertTrue(list((store.ROOT/'backups').glob('*.zip')))

    def test_failed_handoff_does_not_open_another_service(self):
        store.write_json(store.STATE/'server.json',{'port':12345,'token':'test'})
        with patch('urllib.request.urlopen',side_effect=[self._response({'ok':True,'sites':{}}),OSError('missing')]),patch.object(launcher,'retire_instance',return_value=False):
            with self.assertRaises(RuntimeError):launcher.existing_instance()


if __name__=='__main__':unittest.main()
