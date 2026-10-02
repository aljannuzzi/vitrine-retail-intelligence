from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


COSMOS_SYSTEM_KEYS = frozenset({"_etag", "_rid", "_self", "_attachments", "_ts"})
CameraId = Literal["camera-denim", "camera-essentials", "camera-checkout"]
SanitizedCode = Annotated[str, Field(min_length=1, max_length=120,
                                     pattern=r"^[a-z0-9_:-]+$")]
QualitativeText = Annotated[str, Field(min_length=1, max_length=600)]


def strip_known_cosmos_metadata(value: dict) -> dict:
    return {key: item for key, item in value.items() if key not in COSMOS_SYSTEM_KEYS}


def qualitative_doc_id(camera_id: str) -> str:
    return f"qualitative-{camera_id}"


class Login(StrictModel):
    password: str = Field(min_length=1, max_length=1024)


class Control(StrictModel):
    action: Literal["pause", "resume", "reset"]
    speed: Literal[1, 2, 5] | None = None

    @model_validator(mode="before")
    @classmethod
    def integer_speed(cls, value):
        if isinstance(value, dict) and value.get("speed") is not None and type(value["speed"]) is not int:
            raise ValueError("speed must be an integer")
        return value


class AgentRequest(StrictModel):
    agent: Literal["merchandising", "experiment"]
    question: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.question.strip():
            raise ValueError("question cannot be blank")
        return self


class Approval(StrictModel):
    runId: str = Field(min_length=1, max_length=128)
    recommendationId: str = Field(min_length=1, max_length=128)
    actionId: Literal["reposition-denim", "replenish-essentials"]


Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Detection(StrictModel):
    label: Literal["person"]
    score: Coordinate
    box: list[Coordinate] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def ordered(self):
        if self.box[0] >= self.box[2] or self.box[1] >= self.box[3]:
            raise ValueError("Invalid normalized xyxy box")
        return self


class VisionObservation(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    cameraId: CameraId
    status: Literal["ready", "warming", "unavailable"]
    observedAt: str | None = Field(max_length=64)
    sourceSeconds: Annotated[float, Field(ge=0, lt=60, allow_inf_nan=False)] | None
    people: Annotated[int, Field(ge=0, le=1000)] | None
    detections: list[Detection] = Field(max_length=1000)
    frameUri: str | None = Field(max_length=200)
    latencyMs: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    model: str = Field(min_length=1, max_length=200)
    modelRevision: str = Field(min_length=1, max_length=200)
    provenance: Literal["model-inference"]
    error: str | None = Field(max_length=4000)

    @model_validator(mode="after")
    def coherent(self):
        if self.status == "ready":
            if (self.people is None or self.sourceSeconds is None or not self.observedAt or
                    self.frameUri != f"/api/vision/frames/{self.cameraId}.jpg" or
                    self.people != len(self.detections) or self.error is not None):
                raise ValueError("Incomplete ready observation")
            observed = datetime.fromisoformat(self.observedAt.replace("Z", "+00:00"))
            if observed.tzinfo is None:
                raise ValueError("Observation requires UTC offset")
        elif self.people is not None or self.detections or self.frameUri is not None:
            raise ValueError("Unavailable inference cannot contain fake detections")
        return self


class QualitativeObservation(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    pk: Literal["vision"]
    cameraId: CameraId
    epochId: str = Field(min_length=1, max_length=200)
    layoutVersion: Annotated[int, Field(ge=0)]
    variant: Literal["baseline", "optimized"]
    sourceObservationId: str = Field(min_length=1, max_length=200)
    sourceSeconds: Annotated[float, Field(ge=0, lt=60, allow_inf_nan=False)]
    observedAt: str = Field(min_length=1, max_length=64)
    frameSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: Literal["retail-vision"]
    modelRevision: Literal["gpt-5.4"]
    version: Literal[1]
    provenance: Literal["model-inference"]
    source: Literal["synthetic-store-video"]
    configured: bool
    status: Literal["ready", "unavailable"]
    error: SanitizedCode | None = None
    summary: str = Field(max_length=1200)
    observations: list[QualitativeText] = Field(max_length=8)
    limitations: list[QualitativeText] = Field(min_length=1, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("version must be an integer")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.id != qualitative_doc_id(self.cameraId):
            raise ValueError("Qualitative document ID must match the camera")
        try:
            UUID(self.sourceObservationId)
        except ValueError as exc:
            raise ValueError("sourceObservationId must be a UUID") from exc
        observed = datetime.fromisoformat(self.observedAt.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Observation requires UTC offset")
        if self.status == "ready":
            if not self.configured or self.error is not None or not self.summary.strip():
                raise ValueError("Incomplete ready qualitative evidence")
        else:
            if self.error is None:
                raise ValueError("Unavailable qualitative evidence must include a sanitized error")
            if self.summary or self.observations:
                raise ValueError("Unavailable qualitative evidence must be empty")
        if not self.configured and (self.status != "unavailable" or self.error != "qualitative_disabled"):
            raise ValueError("Disabled qualitative evidence must use qualitative_disabled")
        return self
