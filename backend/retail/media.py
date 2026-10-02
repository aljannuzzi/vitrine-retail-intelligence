import re
import time

from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse

from .simulation import CAMERAS


def byte_range(value, size):
    if not value:
        return 0, size, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) <= 128 else None
    if not match or not any(match.groups()) or size <= 0:
        raise HTTPException(416, "range_not_satisfiable", headers={"Content-Range": f"bytes */{size}"})
    left, right = match.groups()
    if not left:
        length = min(int(right), size)
        start = size - length
        end = size - 1
    else:
        start, end = int(left), min(int(right), size - 1) if right else size - 1
    if start >= size or start > end or start < 0:
        raise HTTPException(416, "range_not_satisfiable", headers={"Content-Range": f"bytes */{size}"})
    return start, end - start + 1, True


class BlobMedia:
    def __init__(self, settings):
        self.settings = settings
        self.client = self.credential = None
        self.ready = False
        self.checked_at = 0
        self.cached_ready = False

    async def initialize(self):
        if self.settings.environment == "local" or self.ready:
            return
        if self.client:
            await self.close()
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient
        self.credential = DefaultAzureCredential(
            managed_identity_client_id=self.settings.azure_client_id or None,
            exclude_interactive_browser_credential=True)
        self.client = BlobServiceClient(
            f"https://{self.settings.azure_storage_account}.blob.core.windows.net",
            credential=self.credential, retry_total=1, connection_timeout=10, read_timeout=20)
        self.container = self.client.get_container_client(self.settings.azure_storage_container)
        properties = await self.container.get_container_properties()
        if properties.get("public_access"):
            raise RuntimeError("The media container must be private")
        self.ready = True

    async def footage_ready(self, variant):
        if not self.ready:
            return False
        key = (variant, int(time.time() // 30))
        if self.checked_at == key:
            return self.cached_ready
        import asyncio
        self.cached_ready = all(await asyncio.gather(*(
            self.container.get_blob_client(f"media/{c}/{variant}.mp4").exists() for c in CAMERAS)))
        self.checked_at = key
        return self.cached_ready

    async def stream(self, path, request, content_type):
        if not self.ready:
            raise HTTPException(503, "media_unavailable")
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
        try:
            blob = self.container.get_blob_client(path)
            props = await blob.get_blob_properties()
            expected_observation = request.query_params.get("observation") if content_type == "image/jpeg" else None
            if content_type == "image/jpeg" and (
                    not expected_observation or props.metadata.get("observationid") != expected_observation):
                raise HTTPException(409, "observation_frame_changed")
            offset, length, partial = byte_range(request.headers.get("range"), props.size)
            headers = {"Accept-Ranges": "bytes", "Content-Length": str(length),
                       "Cache-Control": "private, no-store", "ETag": props.etag,
                       "X-Content-Type-Options": "nosniff"}
            if expected_observation:
                headers["X-Vitrine-Observation-Id"] = expected_observation
            if partial:
                headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{props.size}"
            status = 206 if partial else 200
            if request.method == "HEAD" or not length:
                return Response(status_code=status, headers=headers, media_type=content_type)
            download = await blob.download_blob(offset=offset, length=length,
                                                etag=props.etag,
                                                match_condition=MatchConditions.IfNotModified,
                                                max_concurrency=1)
            return StreamingResponse(download.chunks(), status_code=status,
                                     media_type=content_type, headers=headers)
        except ResourceNotFoundError as exc:
            raise HTTPException(404, "media_not_found") from exc
        except HttpResponseError as exc:
            if content_type == "image/jpeg" and getattr(exc, "status_code", None) == 412:
                raise HTTPException(409, "observation_frame_changed") from exc
            raise HTTPException(503, "media_unavailable") from exc

    async def close(self):
        if self.client:
            await self.client.close()
        if self.credential:
            await self.credential.close()
        self.client = self.credential = None
        self.ready = False
