from werkzeug.wrappers import Request, Response
from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import BadRequest
from fs.base import FS
from fs.info import Info
from fs.subfs import SubFS
from fs import path as fspath
import json
import mimetypes
from shutil import copyfileobj


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


def json_response(response) -> Response:
    payload = json.dumps(response)
    return Response(
        response=payload,
        mimetype="application/json",
        headers={"content-length": len(payload)},
        status=200,
    )


def listinfo(fs: FS, path: str, namespaces=["basic", "details"]) -> list[Info]:
    items = fs.listdir(path)
    return [fs.getinfo(fspath.join(path, item), namespaces) for item in items]


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
        self._adapters: list[Adapter] = []
        self._adapterIndex: dict[str, int] = {}
        self.enable_cors = enable_cors

    def add_fs(self, key: str, fs: FS):
        self._adapters.append(Adapter(key, fs))
        self._adapterIndex[key] = len(self._adapters) - 1

    def remove_fs(self, key: str):
        index = self._adapterIndex[key]
        del self._adapters[index]
        del self._adapterIndex[key]

    def _get_adapter(self, request: Request) -> Adapter:
        key = request.args.get("adapter")
        index = self._adapterIndex.get(key, 0)
        return self._adapters[index]

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
        infos = listinfo(fs, path)

        if filter:
            infos = [info for info in infos if filter in info.name]

        return json_response(
            {
                "adapter": adapter.key,
                "storages": [a.key for a in self._adapters],
                "dirname": self._get_full_path(request),
                "files": [
                    to_vuefinder_resource(adapter.key, path, info) for info in infos
                ],
            }
        )

    def _preview(self, request: Request) -> Response:
        fs, path = self.delegate(request)
        info = fs.getinfo(path, ["basic", "details"])

        # CREDIT: https://stackoverflow.com/a/56184787/3140799
        return Response(
            fs.open(path, "rb"),
            direct_passthrough=True,
            mimetype="application/octet-stream",
            headers={
                "Content-Length": info.size,
                "Content-Disposition": f'attachment; filename="{info.name}"',
            },
        )

    def _subfolders(self, request: Request) -> Response:
        adapter = self._get_adapter(request)
        fs, path = self.delegate(request)
        infos = listinfo(fs, path)
        return json_response(
            {
                "folders": [
                    to_vuefinder_resource(adapter.key, path, info)
                    for info in infos
                    if info.is_dir
                ]
            }
        )

    def _download(self, request: Request) -> Response:
        return self._preview(request)

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
            with fs.open(
                fspath.join(path, request.form.get("name", fsrc.name)), "wb"
            ) as fdst:
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

        response = self.endpoints[endpoint](request)
        response.headers.extend(headers)
        return response

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)
