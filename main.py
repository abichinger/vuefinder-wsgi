from vuefinder import VuefinderApp, fill_fs
from fs.memoryfs import MemoryFS
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
    m2 = MemoryFS()
    fill_fs(m2, {"dir": {"file.txt": None}})

    app = VuefinderApp(enable_cors=True)
    app.add_fs("local", m1)
    app.add_fs("media", m2)
    run_simple("127.0.0.1", 8005, app, use_debugger=True, use_reloader=True)
