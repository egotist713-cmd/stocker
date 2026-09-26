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


class NotificationItem(Params):
    asset_id: int = Field(ge=1)
    key: str = Field(min_length=1, max_length=120, description="Dedupe key per asset and kind")


class NotificationParams(Params):
    channel: str = Field(pattern=r"^[a-z0-9_.-]{1,32}$", description="Delivery channel, e.g. test, telegram, email")
    kind: str = Field(pattern=r"^[a-z0-9_]{1,40}$", description="Notification type, e.g. retry_exhausted, daily_digest")
    severity: Literal["info", "attention", "warning"] = "info"
    title: str = Field(min_length=1, max_length=200)
    items: list[NotificationItem] = Field(min_length=1, max_length=200)


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


# Описания видит агент (tool_search / tool_describe в OpenClaw) и выбирает по ним
# инструмент и аргументы. Поэтому в каждом — точные аргументы с примером.
DESCRIPTIONS = {
    "asset.get": 'Full state of one asset: pipeline stages, QC, Vision, metadata, allowed actions. Args: {"asset_id": 5}',
    "asset.list": (
        'List assets. Optional filters are separate top-level args (no "filter" object): '
        "ready (true/false), metadata_state (none|draft|auto_approved|human_review|approved|rejected), "
        "qc (pending|passed|failed), vision (pending|done|failed), limit, offset. "
        'Example: {"ready": true}. Use {} for all assets. For what waits for a human, use review_queue.'
    ),
    "asset.history": 'Processing events of an asset (JSON messages parsed). Args: {"asset_id": 5}, optional "stage": "METADATA"',
    "review.queue": (
        "Overview in one call: summary (total_assets, ready, counts by metadata state, problem_assets with reasons) "
        "and items waiting for a human decision (human_review) with reasons. "
        "Use for 'what is going on', 'how many are ready', 'what needs my review / attention'. Args: {}"
    ),
    "metadata.get": 'Metadata of an asset: title, description, keywords, validation, review gate. Args: {"asset_id": 5}',
    "operations.list": "All operations with JSON Schema of their parameters. Args: {}",
    "incoming.list": (
        "New files in data/incoming that are not registered in Stocker yet; duplicate_of = id of an asset "
        "with the same content (no need to process). Args: {}"
    ),
    "asset.process_file": 'Ingest a new image inside the project and run QC, Vision, metadata and review gate. Args: {"path": "data/incoming/IMG_1.jpg"}',
    "asset.process": 'Re-run source check, QC, Vision and metadata for a registered asset. Args: {"asset_id": 5}, optional "force": true',
    "metadata.build": 'Create metadata with Metadata AI (partial draft if it fails), then review gate. Args: {"asset_id": 5}',
    "metadata.rebuild": 'Re-apply Python rules without AI, keep human edits, then review gate. Args: {"asset_id": 5}',
    "metadata.edit": (
        "Edit title, description or keywords; validation and review gate then decide the new state. "
        'Args: {"asset_id": 5, "title": "..."} or {"asset_id": 5, "add_keywords": ["x"], "remove_keywords": ["y"]}'
    ),
    "metadata.gate": 'Re-evaluate the review gate. Never changes human decisions. Args: {"asset_id": 5}',
    "metadata.escalate": 'Send to human review (raises risk; cannot lower it). Args: {"asset_id": 5, "reason": "..."}',
    "metadata.approve": "Human decision: approve. Human actors only.",
    "metadata.reject": "Human decision: reject with a reason. Human actors only.",
    "notification.record": (
        "Record that a notification was delivered: event NOTIFY/SENT on each listed asset. "
        "Idempotent per (asset, kind, key): repeats return already_sent. "
        'Args: {"channel": "test", "kind": "retry_exhausted", "severity": "warning", "title": "...", '
        '"items": [{"asset_id": 5, "key": "AI:3"}]}'
    ),
}


def build_registry() -> dict[str, Operation]:
    from app.service import operations as ops

    operations = [
        Operation("asset.get", DESCRIPTIONS["asset.get"], AssetParams, READ, ops.asset_get),
        Operation("asset.list", DESCRIPTIONS["asset.list"], ListParams, READ, ops.asset_list),
        Operation("asset.history", DESCRIPTIONS["asset.history"], HistoryParams, READ, ops.asset_history),
        Operation("review.queue", DESCRIPTIONS["review.queue"], PageParams, READ, ops.review_queue),
        Operation("metadata.get", DESCRIPTIONS["metadata.get"], AssetParams, READ, ops.metadata_get),
        Operation("operations.list", DESCRIPTIONS["operations.list"], NoParams, READ, ops.operations_list),
        Operation("incoming.list", DESCRIPTIONS["incoming.list"], NoParams, READ, ops.incoming_list),
        Operation("asset.process_file", DESCRIPTIONS["asset.process_file"], ProcessFileParams, PIPELINE, ops.asset_process_file),
        Operation("asset.process", DESCRIPTIONS["asset.process"], ProcessParams, PIPELINE, ops.asset_process),
        Operation("metadata.build", DESCRIPTIONS["metadata.build"], BuildParams, PIPELINE, ops.metadata_build),
        Operation("metadata.rebuild", DESCRIPTIONS["metadata.rebuild"], AssetParams, PIPELINE, ops.metadata_rebuild),
        Operation("metadata.edit", DESCRIPTIONS["metadata.edit"], EditParams, PIPELINE, ops.metadata_edit),
        Operation("metadata.gate", DESCRIPTIONS["metadata.gate"], AssetParams, PIPELINE, ops.metadata_gate),
        Operation("metadata.escalate", DESCRIPTIONS["metadata.escalate"], ReasonParams, PIPELINE, ops.metadata_escalate),
        Operation("metadata.approve", DESCRIPTIONS["metadata.approve"], ApproveParams, REVIEW, ops.metadata_approve),
        Operation("metadata.reject", DESCRIPTIONS["metadata.reject"], ReasonParams, REVIEW, ops.metadata_reject),
        Operation("notification.record", DESCRIPTIONS["notification.record"], NotificationParams, PIPELINE, ops.notification_record),
    ]
    return {operation.name: operation for operation in operations}
