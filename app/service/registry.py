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
    state: Literal[
        "rejected", "source_invalid", "blocked", "error", "stale", "processing",
        "human_review", "metadata_approved", "platform_ready",
    ] | None = None
    metadata_approved: bool | None = None
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


REPROCESS_STAGES = Literal["normalize", "qc", "enhancement", "vision", "metadata"]


class ReprocessParams(AssetParams):
    reprocess_from: REPROCESS_STAGES | None = None
    through: REPROCESS_STAGES | None = None
    dry_run: bool = True


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


class CreativeParams(AssetParams):
    # Профиль оценки (app/creative_profiles.py); по умолчанию — STOCKER_CREATIVE_PROFILE.
    profile: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")


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
    "asset.get": (
        "Full state of one asset: state (overall: stale / source_invalid / blocked / error / processing / "
        "human_review / metadata_approved / platform_ready / rejected, with reasons, problems and per-stage "
        'status), pipeline stage summaries, QC, Vision, metadata, allowed actions. Args: {"asset_id": 5}'
    ),
    "asset.list": (
        'List assets. Optional filters are separate top-level args (no "filter" object): '
        "state (rejected|source_invalid|blocked|error|stale|processing|human_review|metadata_approved|platform_ready), "
        "metadata_approved (true/false: metadata approved — NOT ready for stock), "
        "metadata_state (none|draft|auto_approved|human_review|approved|rejected), "
        "qc (pending|passed|failed), vision (pending|done|failed), limit, offset. "
        'Example: {"state": "platform_ready"}. Use {} for all assets. For what waits for a human, use review_queue.'
    ),
    "asset.history": 'Processing events of an asset (JSON messages parsed). Args: {"asset_id": 5}, optional "stage": "METADATA"',
    "review.queue": (
        "Overview in one call: summary (total_assets, by_state, metadata_approved, platform_ready — ready for at "
        "least one stock platform, by_metadata_state, problem_counts, problem_assets) and items waiting for a human "
        "decision (state human_review) with reasons. "
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
    "asset.reprocess": (
        "Controlled recompute of stale results: the given stage (default: state.reprocess_from) and its downstream "
        "(qc -> enhancement -> vision -> metadata), only stages that are not current; upstream is never rewritten. "
        "Refused for source_invalid / rejected / non-current upstream. dry_run=true by default: returns the plan "
        "(what would run and be replaced) without writing. Args: "
        '{"asset_id": 5} or {"asset_id": 5, "reprocess_from": "vision", "dry_run": false}'
    ),
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
    "normalize.evaluate": (
        "Describe the original file without changing it: format, color profile, bit depth, HDR / Ultra HDR, "
        'Motion Photo video, extra streams, orientation, metadata present. Args: {"asset_id": 5}'
    ),
    "normalize.run": (
        "Build the internal representation for analysis: JPEG / PNG / TIFF are used as is, AVIF gets a lossless "
        "internal copy. Never improves the photo and never changes the original. Unsafe cases fail with a reason "
        '(MISSING_CODEC, UNSUPPORTED_HDR, COLOR_SPACE_UNDECLARED for CMYK without ICC…). Args: {"asset_id": 5}'
    ),
    "normalize.get": (
        "Last facts about the original file (format, color profile, HDR, Motion Photo) and its internal "
        'representation manifest. Read only. Args: {"asset_id": 5}'
    ),
    "enhancement.assess": (
        "Image quality check before Vision: noise, sharpness, compression artifacts, resolution. "
        "Rules decide clear cases (enhancement_not_needed / enhancement_recommended / enhancement_risky); "
        "borderline cases are 'disputed'. Recommendation only: nothing is enhanced. "
        'Args: {"asset_id": 5}'
    ),
    "enhancement.advise": (
        "Ask the local model for a recommendation only when the quality rules could not decide (disputed). "
        "Cases decided by the rules return NOT_DISPUTED without calling the model; the model never overrides them. "
        'Recommendation only: nothing is enhanced. Args: {"asset_id": 5}'
    ),
    "enhancement.get": (
        "Last enhancement decision of an asset: decision, reasons (noise/sharpness/artifacts/resolution), "
        'metrics and notes. Read only. Args: {"asset_id": 5}'
    ),
    "creative.review": (
        "Commercial value review by the local model: composition, uniqueness, demand, use cases, quality notes "
        "and a recommendation (proceed / attention / skip_suggested); commercial_score is computed from them. "
        "Judged for a content profile: industrial_stock, architecture_stock, nature_stock, or \"auto\" "
        "(chosen from the Vision description). "
        'Recommendation only: never blocks export. Args: {"asset_id": 5}, optional "profile": "auto"'
    ),
    "creative.get": (
        "Last commercial value review of an asset: commercial_score, commercial_potential, recommendation "
        'and the model\'s features. Read only. Args: {"asset_id": 5}'
    ),
    "readiness.evaluate": (
        "Check an asset against Adobe Stock and Shutterstock rules (no upload): status per platform, "
        "blockers, warnings and the export plan. Only for metadata auto_approved/approved; "
        'repeats with unchanged inputs return UNCHANGED. Args: {"asset_id": 5}'
    ),
    "readiness.get": (
        "Last Stock Readiness result of an asset: status per platform (ready/blocked/stale/not_evaluated), "
        'ready_for, checks and export plan. Read only. Args: {"asset_id": 5}'
    ),
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
        Operation("asset.reprocess", DESCRIPTIONS["asset.reprocess"], ReprocessParams, PIPELINE, ops.asset_reprocess),
        Operation("metadata.build", DESCRIPTIONS["metadata.build"], BuildParams, PIPELINE, ops.metadata_build),
        Operation("metadata.rebuild", DESCRIPTIONS["metadata.rebuild"], AssetParams, PIPELINE, ops.metadata_rebuild),
        Operation("metadata.edit", DESCRIPTIONS["metadata.edit"], EditParams, PIPELINE, ops.metadata_edit),
        Operation("metadata.gate", DESCRIPTIONS["metadata.gate"], AssetParams, PIPELINE, ops.metadata_gate),
        Operation("metadata.escalate", DESCRIPTIONS["metadata.escalate"], ReasonParams, PIPELINE, ops.metadata_escalate),
        Operation("metadata.approve", DESCRIPTIONS["metadata.approve"], ApproveParams, REVIEW, ops.metadata_approve),
        Operation("metadata.reject", DESCRIPTIONS["metadata.reject"], ReasonParams, REVIEW, ops.metadata_reject),
        Operation("normalize.evaluate", DESCRIPTIONS["normalize.evaluate"], AssetParams, PIPELINE, ops.normalize_evaluate),
        Operation("normalize.run", DESCRIPTIONS["normalize.run"], AssetParams, PIPELINE, ops.normalize_run),
        Operation("normalize.get", DESCRIPTIONS["normalize.get"], AssetParams, READ, ops.normalize_get),
        Operation("enhancement.assess", DESCRIPTIONS["enhancement.assess"], AssetParams, PIPELINE, ops.enhancement_assess),
        Operation("enhancement.advise", DESCRIPTIONS["enhancement.advise"], AssetParams, PIPELINE, ops.enhancement_advise),
        Operation("enhancement.get", DESCRIPTIONS["enhancement.get"], AssetParams, READ, ops.enhancement_get),
        Operation("creative.review", DESCRIPTIONS["creative.review"], CreativeParams, PIPELINE, ops.creative_review),
        Operation("creative.get", DESCRIPTIONS["creative.get"], AssetParams, READ, ops.creative_get),
        Operation("readiness.evaluate", DESCRIPTIONS["readiness.evaluate"], AssetParams, PIPELINE, ops.readiness_evaluate),
        Operation("readiness.get", DESCRIPTIONS["readiness.get"], AssetParams, READ, ops.readiness_get),
        Operation("notification.record", DESCRIPTIONS["notification.record"], NotificationParams, PIPELINE, ops.notification_record),
    ]
    return {operation.name: operation for operation in operations}
