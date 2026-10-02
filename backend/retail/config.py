from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    environment: str = "azure"
    local_mode: bool = False
    local_database: str = ".local/retail.db"
    app_password: str = ""
    session_secret: str = ""
    public_origin: str = ""
    azure_client_id: str = ""
    cosmos_endpoint: str = ""
    cosmos_database: str = "vitrine"
    cosmos_container: str = "retail"
    azure_storage_account: str = ""
    azure_storage_container: str = "retail"
    foundry_project_endpoint: str = ""
    agent_model_deployment: str = "retail-agent"
    azure_openai_endpoint: str = ""
    vision_model_deployment: str = "retail-vision"
    frontend_dist: str = "frontend/dist"

    @classmethod
    def from_env(cls, *, for_bootstrap: bool = False) -> "Settings":
        values = {name: os.environ[name.upper()] for name in cls.__dataclass_fields__
                  if name.upper() in os.environ and name != "local_mode"}
        values["local_mode"] = os.environ.get("LOCAL_MODE", "").lower() == "true"
        result = cls(**values)
        result.validate(for_bootstrap=for_bootstrap)
        return result

    def validate(self, *, for_bootstrap: bool = False) -> None:
        if self.environment not in ("azure", "local"):
            raise ValueError("ENVIRONMENT must be azure or local")
        if self.local_mode != (self.environment == "local"):
            raise ValueError("Local persistence requires ENVIRONMENT=local and LOCAL_MODE=true")
        if not for_bootstrap:
            if len(self.app_password) < 12:
                raise ValueError("APP_PASSWORD must contain at least 12 characters")
            if self.session_secret and len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET must contain at least 32 characters")
            origin = urlsplit(self.public_origin)
            if (origin.scheme != "https" or not origin.netloc or origin.path or
                    origin.query or origin.fragment or origin.username or origin.password):
                raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without trailing slash")
        if self.environment == "azure":
            if for_bootstrap and not self.azure_client_id.strip():
                raise ValueError("AZURE_CLIENT_ID is required for bootstrap")
            if not all((self.cosmos_endpoint, self.azure_storage_account,
                        self.foundry_project_endpoint)):
                raise ValueError("Azure configuration is required; no local fallback exists")
            if urlsplit(self.cosmos_endpoint).scheme != "https":
                raise ValueError("COSMOS_ENDPOINT must use HTTPS")
