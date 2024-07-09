# vuefinder-wsgi

[![PyPI version](https://img.shields.io/pypi/v/vuefinder-wsgi)](https://pypi.org/project/vuefinder-wsgi/)
[![LICENSE](https://img.shields.io/github/license/abichinger/vuefinder-wsgi)](https://github.com/abichinger/vuefinder-wsgi/blob/main/LICENSE)

WSGI app for [vuefinder](https://github.com/n1crack/vuefinder). This is a vuefinder backend to access [PyFilesystem2](https://github.com/pyfilesystem/pyfilesystem2) filesystems.

# Unimplemented

- archive
- unarchive

# Installation

```sh
pip install vuefinder-wsgi
```

# Usage

```python
from vuefinder import VuefinderApp, fill_fs
from fs.memoryfs import MemoryFS
from werkzeug.serving import run_simple

if __name__ == "__main__":
    # Initialize filesystem
    memfs = MemoryFS()
    fill_fs(
        memfs,
        {
            "foo": {
                "file.txt": "Hello World!",
                "foo.txt": "foo bar baz",
                "bar": {"baz": None},
            },
            "foobar": {"empty": None, "hello.txt": "Hello!"},
        },
    )

    # Create and run the WSGI app
    app = VuefinderApp(enable_cors=True)
    app.add_fs("memory", memfs)
    run_simple("127.0.0.1", 8005, app)

```
