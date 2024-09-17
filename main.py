from vuefinder import VuefinderApp, fill_fs
from fs.memoryfs import MemoryFS
from fs.wrap import WrapReadOnly
from fs.osfs import OSFS
from werkzeug.serving import run_simple

if __name__ == "__main__":
    m1 = MemoryFS()
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

    app = VuefinderApp(enable_cors=True)
    app.add_fs("local", m1)
    app.add_fs("media", WrapReadOnly(OSFS("./tests/media")))
    app.add_fs("media-rw", OSFS("./tests/media"))
    run_simple("127.0.0.1", 8005, app, use_debugger=True, use_reloader=True)
