"""
Реестр операций service layer (docs/SERVICE_CONTRACT.md §3).

Одно описание — много транспортов: из реестра строятся JSON CLI (app.api),
манифест operations.list и будущие MCP/HTTP-адаптеры.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

READ = "read"
PIPELINE = "pipeline"
REVIEW = "review"


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


class AssetParams(Params):
    asset_id: int = Field(ge=1)


class ListParams(Params):
    qc: Literal["pending", "passed", "failed"] | None = None
    vision: Literal["pending", "done", "failed"] | None = None
    metadata_state: Literal["none", "draft", "auto_approved", "human_review", "approved", "rejected"] | None = None
    ready: bool | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class PageParams(Params):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class HistoryParams(AssetParams):
    stage: str | None = None


class ProcessFileParams(Params):
    path: str = Field(min_length=1, description="Path inside the project; relative paths resolve from the project root")


class ProcessParams(AssetParams):
    force: bool = False


class BuildParams(AssetParams):
    force: bool = False


class EditParams(AssetParams):
    title: str | None = None
    description: str | None = None
    keywords: list[str] | None = Field(default=None, description="Replace the whole keyword list")
    add_keywords: list[str] = Field(default_factory=list)
    remove_keywords: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _something_to_change(self):
        if (
            self.title is None
            and self.description is None
            and self.keywords is None
            and not self.add_keywords
            and not self.remove_keywords
        ):
            raise ValueError("nothing to change")
        return self


class ReasonParams(AssetParams):
    reason: str = Field(min_length=1)


class ApproveParams(AssetParams):
    allow_partial: bool = False
    confirm_claims: bool = False


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    params: type[Params]
    access: str
    handler: Callable[[Params], dict]

    @property
    def mutating(self) -> bool:
        return self.access != READ

    def manifest(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "access": self.access,
            "mutating": self.mutating,
            "params_schema": self.params.model_json_schema(),
        }


def build_registry() -> dict[str, Operation]:
    from app.service import operations as ops

    operations = [
        Operation("asset.get", "Full state of one asset: pipeline stages, QC, Vision, metadata, allowed actions.", AssetParams, READ, ops.asset_get),
        Operation("asset.list", "List assets filtered by derived pipeline state.", ListParams, READ, ops.asset_list),
        Operation("asset.history", "Processing events of an asset (JSON messages parsed).", HistoryParams, READ, ops.asset_history),
        Operation("review.queue", "Assets waiting for a human decision (human_review) with gate reasons.", PageParams, READ, ops.review_queue),
        Operation("metadata.get", "metadata_json of an asset.", AssetParams, READ, ops.metadata_get),
        Operation("operations.list", "This manifest: operations with JSON Schema of parameters.", NoParams, READ, ops.operations_list),
        Operation("asset.process_file", "Ingest a new image inside the project and run QC, Vision, metadata draft and review gate.", ProcessFileParams, PIPELINE, ops.asset_process_file),
        Operation("asset.process", "Re-run source check, QC, Vision (force) and metadata draft for a registered asset.", ProcessParams, PIPELINE, ops.asset_process),
        Operation("metadata.build", "Create metadata with Metadata AI (partial draft if it fails), then review gate.", BuildParams, PIPELINE, ops.metadata_build),
        Operation("metadata.rebuild", "Re-apply Python rules without AI, keep human edits, then review gate.", AssetParams, PIPELINE, ops.metadata_rebuild),
        Operation("metadata.edit", "Edit title, description or keywords; then validation and review gate decide the new state.", EditParams, PIPELINE, ops.metadata_edit),
        Operation("metadata.gate", "Re-evaluate the review gate. Never changes human decisions.", AssetParams, PIPELINE, ops.metadata_gate),
        Operation("metadata.escalate", "Send to human review (raises risk; cannot lower it).", ReasonParams, PIPELINE, ops.metadata_escalate),
        Operation("metadata.approve", "Human decision: approve. Human actors only.", ApproveParams, REVIEW, ops.metadata_approve),
        Operation("metadata.reject", "Human decision: reject with a reason. Human actors only.", ReasonParams, REVIEW, ops.metadata_reject),
    ]
    return {operation.name: operation for operation in operations}
