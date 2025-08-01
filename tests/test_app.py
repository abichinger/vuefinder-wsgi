import unittest
from werkzeug.test import Client, EnvironBuilder
from vuefinder import VuefinderApp, fill_fs
from fs.memoryfs import MemoryFS
import urllib.parse
import concurrent.futures


def create_test_app() -> VuefinderApp:
    app = VuefinderApp()
    m1 = MemoryFS()
    m2 = MemoryFS()
    fill_fs(
        m1,
        {
            "foo": {
                "file.txt": "Hello World!",
                "foo.txt": "foo bar baz",
                "bar": {"baz": None},
            },
            "foobar": {"empty": None, "hello.txt": "Hello!"},
        },
    )
    app.add_fs("m1", m1)
    app.add_fs("m2", m2)
    return app


def get_request(*args, **kwargs):
    builder = EnvironBuilder(*args, **kwargs)
    return builder.get_request()


class TestApp(unittest.TestCase):
    def test_index(self):
        app = create_test_app()
        client = Client(app)

        params = {"q": "index", "adapter": "m1", "path": "m1://"}
        resp = client.get("/?" + urllib.parse.urlencode(params))

        self.assertEqual(resp.status_code, 200)

        files = [f["basename"] for f in resp.json["files"]]
        self.assertListEqual(sorted(files), sorted(["foo", "foobar"]))

    def test_threading(self):
        app = create_test_app()

        # mock = Mock()
        # mock.__enter__ = Mock()
        # mock.__exit__ = Mock()
        # app._lock = mock

        n = 1000
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=n)

        def run_thread(i: int):
            fs = MemoryFS()
            key = f"fs{i}"
            params = {"q": "index", "adapter": key, "path": f"{key}://"}
            request = get_request("/?" + urllib.parse.urlencode(params))

            app.add_fs(key, fs)
            resp = app.dispatch_request(request)
            app.remove_fs(key)

            return resp.status_code

        futures = [executor.submit(run_thread, i) for i in range(n)]
        results = [future.result() for future in futures]

        for res in results:
            self.assertEqual(res, 200)

    def test_move(self):
        app = create_test_app()
        m1 = app._adapters["m1"]
        m2 = app._adapters["m2"]

        params = {"q": "move", "adapter": "m1", "path": "m1://foo"}
        request = get_request(
            "/?" + urllib.parse.urlencode(params),
            method="POST",
            json={
                "item": "m2://",
                "items": [
                    {"path": "m1://foo/foo.txt", "type": "file"},
                    {"path": "m1://foo/bar", "type": "dir"},
                ],
            },
        )

        app.dispatch_request(request)
        self.assertListEqual(sorted(m1.listdir("/foo")), ["file.txt"])
        self.assertListEqual(sorted(m2.listdir("/")), ["bar", "foo.txt"])

    def test_copy(self):
        app = create_test_app()
        m1 = app._adapters["m1"]
        m2 = app._adapters["m2"]

        params = {"q": "copy", "adapter": "m1", "path": "m1://foo"}
        request = get_request(
            "/?" + urllib.parse.urlencode(params),
            method="POST",
            json={
                "item": "m2://",
                "items": [
                    {"path": "m1://foo/foo.txt", "type": "file"},
                    {"path": "m1://foo/bar", "type": "dir"},
                ],
            },
        )

        app.dispatch_request(request)
        self.assertListEqual(sorted(m1.listdir("/foo")), ["bar", "file.txt", "foo.txt"])
        self.assertListEqual(sorted(m2.listdir("/")), ["bar", "foo.txt"])
