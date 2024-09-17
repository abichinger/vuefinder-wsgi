from http import HTTPStatus
from typing import Iterable, Mapping
from werkzeug.wrappers import Request, Response
from werkzeug.exceptions import BadRequest
from fs.base import FS
from fs.info import Info
from fs.subfs import SubFS
from fs import errors
from fs import path as fspath
import json
import mimetypes
from shutil import copyfileobj
from collections import OrderedDict


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


def to_vuefinder_resource(storage: str, path: str, info: Info) -> dict:
    if path == "/":
        path = ""
    return {
        "type": "dir" if info.is_dir else "file",
        "path": f"{storage}:/{path}/{info.name}",
        "visibility": "public",
        "last_modified": info.modified.timestamp(),
        "mime_type": mimetypes.guess_type(info.name)[0],
        "extra_metadata": [],
        "basename": info.name,
        "extension": info.name.split(".")[-1],
        "storage": storage,
        "file_size": info.size,
    }


class Adapter(object):
    def __init__(self, key: str, fs: FS):
        self.key = key
        self.fs = fs


class VuefinderApp(object):
    def __init__(self, enable_cors: bool = False):
        self.endpoints = {
            "GET:index": self._index,
            "GET:preview": self._preview,
            "GET:subfolders": self._subfolders,
            "GET:download": self._download,
            "GET:search": self._search,
            "POST:newfolder": self._newfolder,
            "POST:newfile": self._newfile,
            "POST:rename": self._rename,
            "POST:move": self._move,
            "POST:delete": self._delete,
            "POST:upload": self._upload,
            "POST:archive": self._archive,
            "POST:unarchive": self._unarchive,
            "POST:save": self._save,
        }
        self._default: Adapter | None = None
        self._adapters: dict[str, FS] = OrderedDict()
        self.enable_cors = enable_cors

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
        return Adapter(key, self._adapters.get(key, self._default.fs))

    def _get_storages(self):
        return list(self._adapters.keys())

    def _get_full_path(self, request: Request) -> str:
        return request.args.get("path", self._get_adapter(request).key + "://")

    def _fs_path(self, path: str) -> str:
        if ":/" in path:
            return fspath.abspath(path.split(":/")[1])
        return fspath.abspath(path)

    def delegate(self, request: Request) -> tuple[FS, str]:
        adapter = self._get_adapter(request)
        path = self._get_full_path(request)
        return adapter.fs, self._fs_path(path)

    def _index(self, request: Request, filter: str | None = None) -> Response:
        adapter = self._get_adapter(request)
        fs, path = self.delegate(request)
        infos = list(fs.scandir(path, namespaces=["basic", "details"]))

        if filter:
            infos = [info for info in infos if filter in info.name]

        infos.sort(key=lambda i: ("0_" if i.is_dir else "1_") + i.name.lower())

        return json_response(
            {
                "adapter": adapter.key,
                "storages": self._get_storages(),
                "dirname": self._get_full_path(request),
                "files": [
                    to_vuefinder_resource(adapter.key, path, info) for info in infos
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
            "Content-Disposition": f'attachment; filename="{info.name}"',
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
        fs.writetext(fspath.join(path, name), "")
        return self._index(request)

    def _rename(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        payload = request.get_json()
        self.__move(
            fs, payload.get("item", ""), fspath.join(path, payload.get("name", ""))
        )
        return self._index(request)

    def __move(self, fs, src, dst):
        src = self._fs_path(src)
        dst = self._fs_path(dst)
        if fs.isdir(src):
            fs.movedir(src, dst, create=True)
        else:
            fs.move(src, dst)

    def _move(self, request: Request) -> Response:
        fs, _ = self.delegate(request)
        payload = request.get_json()
        dst_dir = payload.get("item", "")
        for item in payload.get("items", []):
            src = item["path"]
            self.__move(fs, src, fspath.combine(dst_dir, fspath.basename(src)))
        return self._index(request)

    def _delete(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        payload = request.get_json()
        for item in payload.get("items", []):
            path = self._fs_path(item["path"])
            if fs.isdir(path):
                fs.removetree(path)
            else:
                fs.remove(path)

        return self._index(request)

    def _upload(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        for fsrc in request.files.values():
            with fs.open(fspath.join(path, request.form.get("name", "")), "wb") as fdst:
                copyfileobj(fsrc.stream, fdst)

        return json_response("ok")

    def _archive(self, request: Request) -> Response:
        raise "unimplemented"

    def _unarchive(self, request: Request) -> Response:
        raise "unimplemented"

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
        except errors.ResourceReadOnly as exc:
            response = json_response({"message": str(exc), "status": False}, 400)

        response.headers.extend(headers)
        return response

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)
