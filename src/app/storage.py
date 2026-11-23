"""File storage: the R2 bucket binding in the Worker, an in-memory dict in tests."""

from typing import Any, Protocol


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> tuple[bytes, str] | None: ...
    async def delete(self, key: str) -> None: ...


class R2Storage:
    def __init__(self, binding: Any):
        self.binding = binding

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        from js import Object
        from pyodide.ffi import to_js

        await self.binding.put(
            key,
            to_js(data),
            httpMetadata=to_js(
                {
                    "contentType": content_type,
                    # Keys are unique per upload, so files never change: cache for a year
                    "cacheControl": "public, max-age=31536000, immutable",
                },
                dict_converter=Object.fromEntries,
            ),
        )

    async def get(self, key: str) -> tuple[bytes, str] | None:
        obj = await self.binding.get(key)
        if obj is None:
            return None
        data = bytes(await obj.arrayBuffer())
        content_type = obj.httpMetadata.contentType or "application/octet-stream"
        return data, content_type

    async def delete(self, key: str) -> None:
        await self.binding.delete(key)


class MemoryStorage:
    def __init__(self):
        self.files: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.files[key] = (data, content_type)

    async def get(self, key: str) -> tuple[bytes, str] | None:
        return self.files.get(key)

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
