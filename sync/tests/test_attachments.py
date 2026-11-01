"""附件文件操作与同步回归测试；仅使用临时目录。"""
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attachments
import server
import store
import zhs_detail


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-attachment-test-")
        self.base = Path(self.temp.name).resolve()
        self.stack = ExitStack()
        for key, value in {"ROOT": self.base, "STATE": self.base / "state",
                           "DATA": self.base / "data", "LATEST": self.base / "data/latest.json"}.items():
            self.stack.enter_context(patch.object(store, key, value))
        store.STATE.mkdir()
        store.DATA.mkdir()

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def put(self, name="测试.txt", data=b"hello", **kw):
        return attachments.upload(name, io.BytesIO(data), len(data), **kw)

    def test_upload_searchable_listing_and_duplicate(self):
        one = self.put()
        two = self.put()
        info = attachments.listing()
        self.assertEqual(info["path"], str((store.DATA / "att").resolve()))
        self.assertEqual(info["count"], 2)
        self.assertEqual(info["size"], 10)
        self.assertNotEqual(one["local"], two["local"])

    def test_rename_chain_and_rename_back(self):
        item = self.put()
        attachments.rename("测试.txt", "改名.txt")
        self.assertEqual(attachments.normalize([item])[0]["name"], "改名.txt")
        attachments.rename("改名.txt", "最终.txt")
        self.assertEqual(attachments.resolve_local(item["local"]), "att/最终.txt")
        attachments.rename("最终.txt", "测试.txt")
        self.assertEqual(attachments.resolve_local(item["local"]), "att/测试.txt")
        self.assertEqual(attachments.resolve_local("att/改名.txt"), "att/测试.txt")

    def test_delete_removes_references_and_retains_recycle_copy(self):
        item = self.put()
        attachments.rename("测试.txt", "改名.txt")
        attachments.delete("改名.txt")
        self.assertEqual(attachments.normalize([item]), [])
        self.assertEqual(attachments.listing()["count"], 0)
        self.assertEqual(len(list((store.DATA / "att-trash").iterdir())), 1)

    def test_reusing_old_names_does_not_redirect_new_uploads(self):
        original = self.put()
        attachments.rename("测试.txt", "改名.txt")
        uploaded = self.put()
        self.assertNotEqual(uploaded["local"], original["local"])
        self.assertEqual(attachments.normalize([uploaded])[0]["local"], uploaded["local"])
        self.assertEqual(attachments.resolve_local(original["local"]), "att/改名.txt")
        with self.assertRaises(FileExistsError):
            attachments.rename(uploaded["name"], "测试.txt")

    def test_replace_and_incomplete_upload_preserve_original(self):
        self.put()
        with self.assertRaises(ValueError):
            attachments.upload("测试.txt", io.BytesIO(b"bad"), 10, replace=True)
        self.assertEqual(attachments.path_for("测试.txt").read_bytes(), b"hello")
        self.put(data=b"replacement", replace=True)
        self.assertEqual(attachments.path_for("测试.txt").read_bytes(), b"replacement")

    def test_reject_paths_names_collision_and_oversized_upload(self):
        for name in ("../outside", "..\\outside", "C:\\secret.txt", "NUL.txt", "bad.", "a:b", "", "."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.put(name)
        self.put("one.txt")
        self.put("two.txt")
        with self.assertRaises(FileExistsError):
            attachments.rename("one.txt", "two.txt")
        with self.assertRaises(ValueError):
            attachments.upload("big.zip", io.BytesIO(), attachments.MAX_UPLOAD + 1)

    def test_sync_keeps_renames_and_does_not_redownload_deleted(self):
        remote = {"name": "讲义.pdf", "url": "https://example.invalid/file.pdf"}
        import hashlib
        name = hashlib.md5(remote["url"].encode()).hexdigest()[:8] + "_讲义.pdf"
        self.put(name, b"original")
        attachments.rename(name, "交给智能体.pdf")
        with patch("urllib.request.urlopen", side_effect=AssertionError("must not download")):
            files = zhs_detail.download_files([remote])
            self.assertEqual(files[0]["local"], "att/交给智能体.pdf")
            attachments.delete("交给智能体.pdf")
            self.assertEqual(zhs_detail.download_files([remote]), [])

    def test_task_upload_survives_resync_and_detail_enrichment(self):
        task = {"id": "task1", "title": "实验", "due": "2026-11-01", "src": "zhs"}
        store.write_json(store.LATEST, {"ddl": [task]})
        self.put(task_id="task1")
        store.merge_latest("zhs", {"ddl": [task]}, replace=True)
        store.enrich_ddl({"h1": {"title": "实验", "due": "2026-11-01", "files": []}})
        self.assertEqual(store.read_latest()["ddl"][0]["attachments"][0]["name"], "测试.txt")

    def test_prepare_browser_prefers_embedded_and_overrides_missing_cache(self):
        embedded, beside = self.base / "embedded", self.base / "beside"
        for base in (embedded, beside):
            exe = base / "browser/chromium-1248/chrome-win64/chrome.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
        with patch.object(store, "WEB", embedded), patch.object(store, "EXEDIR", beside), patch.dict("os.environ", {"PLAYWRIGHT_BROWSERS_PATH": "C:/missing"}):
            self.assertEqual(store.prepare_browser(), str(embedded / "browser"))

    def test_http_auth_raw_upload_download_and_file_actions(self):
        srv = server.LocalServer(("127.0.0.1", 0), server.Handler)
        host = "127.0.0.1:%d" % srv.server_port
        server.ALLOWED_HOSTS.add(host)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()

        def request(path, data=None, token=True):
            req = urllib.request.Request("http://" + host + path, data=data)
            if token:
                req.add_header("X-WB-Token", server.TOKEN)
            return urllib.request.urlopen(req, timeout=5)

        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/api/attachments", token=False)
            self.assertEqual(denied.exception.code, 401)
            with request("/api/attachments/upload?name=api.txt", b"API test") as r:
                self.assertTrue(json.load(r)["ok"])
            with request("/api/attachments/file?name=api.txt") as r:
                self.assertEqual(r.read(), b"API test")
            for action, body in (("rename", {"name": "api.txt", "newName": "new.txt"}),
                                 ("delete", {"name": "new.txt"})):
                with request("/api/attachments/" + action, json.dumps(body).encode()) as r:
                    self.assertTrue(json.load(r)["ok"])
            with request("/api/attachments") as r:
                self.assertEqual(json.load(r)["count"], 0)
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)
            server.ALLOWED_HOSTS.discard(host)


if __name__ == "__main__":
    unittest.main()
