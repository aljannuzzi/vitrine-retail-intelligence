import hashlib
import json
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    if not account.startswith("vitrine"):
        raise ValueError("Media initialization only targets an independent VITRINE account")
    root = Path(os.environ.get("MEDIA_SOURCE_ROOT", "/app/assets/generated"))
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Rendered media provenance manifest is required")
    json.loads(manifest.read_text(encoding="utf-8"))
    cameras = ("camera-denim", "camera-essentials", "camera-checkout")
    expected = [root / f"{camera}-{variant}.mp4" for camera in cameras for variant in ("baseline", "optimized")]
    for path in expected:
        if not path.is_file() or path.stat().st_size < 1000:
            raise ValueError(f"Missing rendered source: {path.name}")
    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    client = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=credential)
    container = client.get_container_client(os.environ.get("AZURE_STORAGE_CONTAINER", "retail"))
    try:
        for path in expected:
            camera, variant = path.stem.rsplit("-", 1)
            with path.open("rb") as data:
                digest = hashlib.file_digest(data, "sha256").hexdigest()
            blob = f"media/{camera}/{variant}.mp4"
            with path.open("rb") as data:
                container.upload_blob(
                    blob, data, overwrite=True,
                    content_settings=ContentSettings(content_type="video/mp4", cache_control="private, max-age=3600"),
                    metadata={"sha256": digest, "provenance": "original-synthetic-3d"},
                )
            print(json.dumps({"event": "synthetic_media_seeded", "blob": blob, "bytes": path.stat().st_size, "sha256": digest}), flush=True)
        with manifest.open("rb") as data:
            container.upload_blob(
                "media/manifest.json", data, overwrite=True,
                content_settings=ContentSettings(content_type="application/json", cache_control="no-cache"),
            )
    finally:
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
