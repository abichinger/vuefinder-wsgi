from typing import Union
from werkzeug.wrappers import Request, Response
from werkzeug.exceptions import BadRequest
from fs.base import FS
from fs.info import Info
from fs.subfs import SubFS
from fs.zipfs import ZipFS
from fs import path as fspath, errors, copy, walk, move
import json
import mimetypes
from shutil import copyfileobj
from collections import OrderedDict
from pathvalidate import is_valid_filename
import io
from typing import Callable


def fill_fs(fs: FS, d: dict):
    for k, v in d.items():
        if v is None:
            fs.create(k)
        elif isinstance(v, str):
            f = fs.open(k, "w")
            f.write(v)
            f.close()
        else:
            fs.makedir(k)
            fill_fs(SubFS(fs, k), v)


def json_response(response, status: int = 200) -> Response:
    payload = json.dumps(response)
    return Response(
        response=payload,
        mimetype="application/json",
        headers={"content-length": len(payload)},
        status=status,
    )


def to_vuefinder_resource(
    storage: str, path: str, info: Info, include_raw=False
) -> dict:
    if path == "/":
        path = ""
    return {
        "type": "dir" if info.is_dir else "file",
        "path": f"{storage}:/{path}/{info.name}",
        "visibility": "public",
        "last_modified": info.modified.timestamp() if info.modified else None,
        "mime_type": mimetypes.guess_type(info.name)[0],
        "basename": info.name,
        "extension": info.name.split(".")[-1],
        "storage": storage,
        "file_size": info.size,
        "raw": info.raw if include_raw else None,
    }


def fs_type(fs: FS) -> str:
    unwrapped = fs
    while hasattr(unwrapped, "_wrap_fs"):
        unwrapped = unwrapped._wrap_fs
    return f"{unwrapped.__class__.__module__}.{unwrapped.__class__.__qualname__}"


class Adapter(object):
    def __init__(self, key: str, fs: FS):
        self.key = key
        self.fs = fs


class VuefinderApp(object):
    def __init__(
        self,
        enable_cors: bool = False,
        include_raw=False,
        fs_type: Callable[[FS], str] | None = None,
    ):
        self.endpoints = {
            "GET:index": self._index,
            "GET:preview": self._preview,
            "GET:subfolders": self._subfolders,
            "GET:download": self._download,
            "GET:download_archive": self._download_archive,
            "GET:search": self._search,
            "POST:newfolder": self._newfolder,
            "POST:newfile": self._newfile,
            "POST:rename": self._rename,
            "POST:move": self._move,
            "POST:copy": self._copy,
            "POST:delete": self._delete,
            "POST:upload": self._upload,
            "POST:archive": self._archive,
            "POST:unarchive": self._unarchive,
            "POST:save": self._save,
        }
        self._default: Union[Adapter, None] = None
        self._adapters: dict[str, FS] = OrderedDict()
        self.enable_cors = enable_cors
        self.include_raw = include_raw
        self.fs_type = fs_type

    def add_fs(self, key: str, fs: FS):
        self._adapters[key] = fs
        if len(self._adapters) == 1:
            self._default = Adapter(key, fs)

    def remove_fs(self, key: str):
        self._adapters.pop(key, None)

    def clear(self):
        self._adapters = OrderedDict()

    def _get_adapter(self, request: Request) -> Adapter:
        key = request.args.get("adapter")
        return (
            Adapter(key, self._adapters[key])
            if key in self._adapters
            else self._default
        )

    def _get_full_path(self, request: Request) -> str:
        return request.args.get("path", self._get_adapter(request).key + "://")

    def _abspath(self, path: str) -> str:
        if ":/" in path:
            return fspath.abspath(path.split(":/")[1])
        return fspath.abspath(path)

    def delegate(self, request: Request) -> tuple[FS, str]:
        adapter = self._get_adapter(request)
        path = self._get_full_path(request)
        return adapter.fs, self._abspath(path)

    def _split_path(self, path: str, fallback_fs: FS = None) -> tuple[FS, str]:
        """Splits the full path into filesystem and absolute path"""
        fallback_fs = fallback_fs or self._default.fs
        if ":/" not in path:
            return fallback_fs, self._abspath(path)
        key = path.split(":/")[0]
        return self._adapters.get(key, None) or fallback_fs, self._abspath(path)

    def _index(self, request: Request, filter: Union[str, None] = None) -> Response:
        adapter = self._get_adapter(request)
        fs, path = self.delegate(request)
        infos = list(fs.scandir(path, namespaces=["basic", "details"]))

        if filter:
            infos = [info for info in infos if filter in info.name]

        infos.sort(key=lambda i: ("0_" if i.is_dir else "1_") + i.name.lower())

        return json_response(
            {
                "adapter": adapter.key,
                "storages": list(self._adapters.keys()),
                "storage_info": {
                    name: {
                        "filesystem": self.fs_type(fs) if self.fs_type else fs_type(fs)
                    }
                    for name, fs in self._adapters.items()
                },
                "dirname": self._get_full_path(request),
                "files": [
                    to_vuefinder_resource(adapter.key, path, info, self.include_raw)
                    for info in infos
                ],
            }
        )

    def _download(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        info = fs.getinfo(path, ["basic", "details"])

        headers = {
            "Content-Disposition": f'attachment; filename="{info.name}"',
        }
        if info.size is not None:
            headers["Content-Length"] = info.size

        # CREDIT: https://stackoverflow.com/a/56184787/3140799
        return Response(
            fs.open(path, "rb"),
            direct_passthrough=True,
            mimetype="application/octet-stream",
            headers=headers,
        )

    def _preview(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        info = fs.getinfo(path, ["basic", "details"])

        headers = {
            "Content-Disposition": f'inline; filename="{info.name}"',
        }
        if info.size is not None:
            headers["Content-Length"] = info.size

        return Response(
            fs.open(path, "rb"),
            direct_passthrough=True,
            mimetype=mimetypes.guess_type(info.name)[0] or "application/octet-stream",
            headers=headers,
        )

    def _subfolders(self, request: Request) -> Response:
        adapter = self._get_adapter(request)
        fs, path = self.delegate(request)
        infos = fs.scandir(path, namespaces=["basic", "details"])
        return json_response(
            {
                "folders": [
                    to_vuefinder_resource(adapter.key, path, info)
                    for info in infos
                    if info.is_dir
                ]
            }
        )

    def _search(self, request: Request) -> Response:
        filter = request.args.get("filter", None)
        return self._index(request, filter)

    def _newfolder(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        name = request.get_json().get("name", "")
        fs.makedir(fspath.join(path, name))
        return self._index(request)

    def _newfile(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        name = request.get_json().get("name", "")
        with fs.openbin(fspath.join(path, name), "wb"):
            pass
        return self._index(request)

    def _rename(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        payload = request.get_json()
        self.__move(
            fs, payload.get("item", ""), fspath.join(path, payload.get("name", ""))
        )
        return self._index(request)

    def _transfer(
        self, request: Request, transfer_dir: Callable, transfer_file: Callable
    ) -> Response:
        fs, _ = self.delegate(request)
        payload = request.get_json()
        dst_fs, dst_dir = self._split_path(payload.get("item", ""), fs)
        for item in payload.get("items", []):
            src_fs, src_path = self._split_path(item["path"], fs)
            dst_path = fspath.combine(dst_dir, fspath.basename(src_path))

            if src_fs.isdir(src_path):
                transfer_dir(src_fs, src_path, dst_fs, dst_path)
            else:
                transfer_file(src_fs, src_path, dst_fs, dst_path)

        return self._index(request)

    def _move(self, request: Request) -> Response:
        return self._transfer(request, move.move_dir, move.move_file)

    def _copy(self, request: Request) -> Response:
        return self._transfer(request, copy.copy_dir, copy.copy_file)

    def _delete(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        payload = request.get_json()
        for item in payload.get("items", []):
            path = self._abspath(item["path"])
            if fs.isdir(path):
                fs.removetree(path)
            else:
                fs.remove(path)

        return self._index(request)

    def _upload(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        for fsrc in request.files.values():
            full_path = fspath.join(path, request.form.get("name", ""))
            if not fs.exists(fspath.dirname(full_path)):
                fs.makedirs(fspath.dirname(full_path))
            with fs.open(full_path, "wb") as fdst:
                copyfileobj(fsrc.stream, fdst)

        return json_response("ok")

    def _write_zip(self, zip: FS, fs: FS, paths: list[str], base="/"):
        # ZipFS Docs: https://docs.pyfilesystem.org/en/latest/reference/zipfs.html#fs.zipfs.ZipFS
        while len(paths) > 0:
            path = paths.pop()
            dst_path = fspath.relativefrom(base, path)
            if fs.isdir(path):
                zip.makedir(dst_path)
                paths = [fspath.join(path, name) for name in fs.listdir(path)] + paths
            else:
                with fs.openbin(path) as f:
                    zip.writefile(dst_path, f)

    def _get_filename(self, payload: dict, param: str = "name", ext: str = "") -> str:
        name = payload.get("name", None)
        if name is None or not is_valid_filename(name, platform="universal"):
            raise BadRequest("Invalid archive name")

        if ext.startswith(".") and fspath.splitext(name)[1] != ext:
            name = name + ext

        return name

    def _archive(self, request: Request) -> Response:
        payload = request.get_json()
        name = self._get_filename(payload, ext=".zip")

        fs, path = self.delegate(request)
        items: list[dict] = payload.get("items", [])
        paths = [self._abspath(item["path"]) for item in items if "path" in item]
        archive_path = fspath.join(path, name)

        if fs.exists(archive_path):
            raise BadRequest(f"Archive {archive_path} already exists")

        with fs.openbin(archive_path, mode="w") as f:
            with ZipFS(f, write=True) as zip:
                self._write_zip(zip, fs, paths, path)

        return self._index(request)

    def _download_archive(self, request: Request):
        name = self._get_filename(request.args, ext=".zip")

        fs, path = self.delegate(request)
        paths: list[str] = json.loads(request.args.get("paths", "[]"))
        paths = [self._abspath(path) for path in paths]

        stream = io.BytesIO()

        with ZipFS(stream, write=True) as zip:
            self._write_zip(zip, fs, paths, path)

        return Response(
            stream.getvalue(),
            direct_passthrough=True,
            mimetype="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Type": "application/zip",
            },
        )

    def _unarchive(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        archive_path = self._abspath(request.get_json().get("item"))

        with fs.openbin(archive_path) as zip_file:
            with ZipFS(zip_file) as zip:
                # check if any file already exists
                walker = walk.Walker()
                for file_path in walker.files(zip):
                    dst_path = fspath.join(path, fspath.relpath(file_path))
                    if fs.exists(dst_path):
                        raise BadRequest(
                            f"File {dst_path} would be overridden by unarchive"
                        )

                copy.copy_dir(zip, "/", fs, path)

        return self._index(request)

    def _save(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        payload = request.get_json()
        with fs.open(path, "w") as f:
            f.write(payload.get("content", ""))

        return self._preview(request)

    def dispatch_request(self, request: Request):
        headers = {}
        if self.enable_cors:
            headers.update(
                {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "*",
                }
            )
        if request.method == "OPTIONS":
            return Response(headers=headers)

        endpoint = request.method + ":" + request.args.get("q")
        if endpoint not in self.endpoints:
            raise BadRequest()

        response = None
        try:
            response = self.endpoints[endpoint](request)
        except errors.FSError as exc:
            response = json_response({"message": str(exc), "status": False}, 400)
        except BadRequest as exc:
            response = json_response({"message": exc.description, "status": False}, 400)

        response.headers.extend(headers)
        return response

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)
