"""SDK-side skill management models.

The models in this module mirror the public skill API and local state contract,
but they do not import server DTOs. Response-oriented models ignore extra fields
so older SDKs tolerate additive server changes.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

SkillEvolveMode = Literal["sync", "async"]


class HashState(str, Enum):
    """Local upload/cache state for one managed skill content hash."""

    UNKNOWN = "unknown"
    PENDING_UPLOAD = "pending_upload"
    CONFIRMED = "confirmed"


class SkillVersionStatus(str, Enum):
    """Cloud lifecycle status for one skill version."""

    OBSERVED = "observed"
    DRAFT = "draft"
    EVALUATING = "evaluating"
    READY = "ready"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class SkillOrigin(str, Enum):
    """Origin of one skill version."""

    EDGE = "edge"
    CLOUD = "cloud"


class LocalSkillSyncState(str, Enum):
    """Local synchronization state for one immutable version."""

    LOCAL_ONLY = "local_only"
    PENDING = "pending"
    SYNCED = "synced"
    CONFLICT = "conflict"
    FAILED = "failed"


class LocalSkillFileRole(str, Enum):
    """Purpose of one file in a complete local Skill snapshot."""

    ALGORITHM = "algorithm"
    REFERENCE = "reference"
    SCRIPT = "script"
    ASSET = "asset"


class LocalSkillOperationType(str, Enum):
    """Cloud-facing operation persisted in the local outbox."""

    PUSH_VERSION = "push_version"
    PROMOTE = "promote"
    DELETE_CLOUD_SKILL = "delete_cloud_skill"


class LocalSkillOperationStatus(str, Enum):
    """Retry state for one local outbox operation."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"


class DuplicateSkillAction(str, Enum):
    """Explicit choice when registration finds an identical local snapshot."""

    REUSE = "reuse"
    CREATE_NEW = "create_new"


class SkillUsage(str, Enum):
    """How a skill was used in one add trace."""

    INJECTED = "injected"
    MODIFIED = "modified"


class SkillContext(BaseModel):
    """Lightweight skill reference sent with one memory add request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str
    content_hash: str
    version_id: str = Field(
        validation_alias=AliasChoices("version_id", "base_version_id"),
    )
    version_label: str | None = None
    usage: SkillUsage | str | None = None

    @property
    def base_version_id(self) -> str:
        """Compatibility accessor for callers migrating to ``version_id``."""

        return self.version_id


class SkillRecord(BaseModel):
    """One local skill managed by the SDK registry."""

    model_config = ConfigDict(extra="ignore")

    skill_id: str = ""
    alias: str | None = None
    path: str
    skill_name: str
    cloud_skill_id: str | None = None
    base_version_id: str = ""
    content_hash: str = ""
    hash_state: HashState = HashState.UNKNOWN
    version_label: str | None = None
    registered_at: str | None = None
    updated_at: str | None = None


class LocalSkillManifest(BaseModel):
    """One Skill family manifest stored below ``~/.mindmemos/skills``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    skill_id: str
    name: str
    alias: str | None = None
    cloud_skill_id: str | None = None
    active_version_id: str
    published_head_id: str | None = None
    cloud_revision: int | None = Field(default=None, ge=0)
    version_ids: list[str] = Field(default_factory=list)
    last_sync_at: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_version_pointers(self) -> LocalSkillManifest:
        """Require both pointers to reference complete local versions."""

        if not self.version_ids:
            raise ValueError("version_ids must contain at least one version")
        if len(self.version_ids) != len(set(self.version_ids)):
            raise ValueError("version_ids may not contain duplicates")
        if self.active_version_id not in self.version_ids:
            raise ValueError("active_version_id must belong to version_ids")
        if (
            self.published_head_id is not None
            and self.published_head_id not in self.version_ids
        ):
            raise ValueError("published_head_id must be fully available locally")
        return self


class LocalSkillVersionMetadata(BaseModel):
    """Immutable metadata for one locally materialized Skill version."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    version_id: str
    skill_id: str
    parent_version_id: str | None = None
    skill_name: str
    content_hash: str
    local_snapshot_hash: str
    version_label: str | None = None
    commit_message: str | None = None
    origin: SkillOrigin = SkillOrigin.EDGE
    cloud_status: SkillVersionStatus | None = None
    sync_state: LocalSkillSyncState = LocalSkillSyncState.LOCAL_ONLY
    created_at: str


class LocalSkillFileEntry(BaseModel):
    """One restorable file referenced by an immutable local version."""

    model_config = ConfigDict(extra="forbid")

    path: str
    blob_hash: str
    byte_size: int = Field(ge=0)
    encoding: Literal["utf-8"] = "utf-8"
    media_type: str | None = None
    mode: int | None = None
    role: LocalSkillFileRole


class LocalSkillVersionFiles(BaseModel):
    """Complete file manifest stored beside one local version's metadata."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    version_id: str
    algorithm_files: list[LocalSkillFileEntry]
    linked_files: list[LocalSkillFileEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_roles_and_paths(self) -> LocalSkillVersionFiles:
        """Keep algorithm and private file manifests structurally separate."""

        if not self.algorithm_files:
            raise ValueError("algorithm_files must contain at least one file")
        if any(
            entry.role != LocalSkillFileRole.ALGORITHM
            for entry in self.algorithm_files
        ):
            raise ValueError("algorithm_files may contain only algorithm entries")
        if any(
            entry.role == LocalSkillFileRole.ALGORITHM
            for entry in self.linked_files
        ):
            raise ValueError("linked_files may not contain algorithm entries")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("files.json contains duplicate paths")
        return self

    @property
    def files(self) -> list[LocalSkillFileEntry]:
        """Return the complete snapshot manifest in storage order."""

        return [*self.algorithm_files, *self.linked_files]


class LocalSkillSnapshot(BaseModel):
    """Validated in-memory snapshot read from an explicit source directory."""

    model_config = ConfigDict(extra="forbid")

    content: str
    content_hash: str
    local_snapshot_hash: str
    files: list[LocalSkillFileEntry]
    file_contents: dict[str, str]

    @property
    def linked_files(self) -> dict[str, str]:
        """Return private non-algorithm files without exposing them to cloud DTOs."""

        algorithm_paths = {item.path for item in self.files if item.role == LocalSkillFileRole.ALGORITHM}
        return {path: value for path, value in self.file_contents.items() if path not in algorithm_paths}


class RegisterLocalRequest(BaseModel):
    """Request for importing one external directory as a local Skill root version."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    source_path: str
    name: str | None = None
    alias: str | None = None
    version_label: str | None = None
    commit_message: str | None = None
    duplicate_action: DuplicateSkillAction | None = None


class DuplicateSkillMatch(BaseModel):
    """Existing local version with the same complete snapshot."""

    model_config = ConfigDict(extra="forbid")

    local_snapshot_hash: str
    skill_id: str
    name: str
    active_version_id: str
    matched_version_id: str
    cloud_skill_id: str | None = None
    last_sync_at: str | None = None


class RegisterLocalResult(BaseModel):
    """Outcome of importing or reusing a complete local Skill snapshot."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "reused"]
    skill_id: str
    version_id: str
    active_version_id: str
    summary: DuplicateSkillMatch | None = None


class PublishLocalRequest(BaseModel):
    """Request for adding one immutable version to an existing local Skill."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    base_version_id: str | None = None
    source_path: str | None = None
    content: str | None = None
    version_label: str | None = None
    commit_message: str | None = None
    activate: bool = False


class PublishLocalResult(BaseModel):
    """Outcome of creating a new immutable local Skill version."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version_id: str
    active_version_id: str
    local_snapshot_hash: str


class ExportSkillRequest(BaseModel):
    """Request for materializing one managed version into an external directory."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    target_path: str
    version_id: str | None = None
    replace: bool = True


class ExportSkillResult(BaseModel):
    """Outcome of exporting a complete local Skill snapshot."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str
    version_id: str
    target_path: str
    exported_files: list[str] = Field(default_factory=list)
    local_snapshot_hash: str


class LocalSyncOperation(BaseModel):
    """One idempotent cloud operation persisted locally for retry."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    operation_type: LocalSkillOperationType
    skill_id: str
    version_id: str | None = None
    expected_cloud_revision: int | None = None
    status: LocalSkillOperationStatus = LocalSkillOperationStatus.PENDING
    attempt_count: int = 0
    next_retry_at: str | None = None
    last_error_code: str | None = None
    created_at: str
    updated_at: str


class LocalSyncOutbox(BaseModel):
    """Top-level centralized local Skill outbox."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    operations: list[LocalSyncOperation] = Field(default_factory=list)


class PushVersionRequest(BaseModel):
    """Strict cloud payload for uploading one immutable algorithm version."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    cloud_skill_id: str | None = None
    name: str
    version_id: str
    parent_version_id: str | None = None
    content: str
    expected_content_hash: str
    version_label: str | None = None
    commit_message: str | None = None
    created_at: str


class PushVersionResult(BaseModel):
    """Cloud acknowledgement for one idempotent version Push."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    version_id: str
    content_hash: str
    status: SkillVersionStatus
    created_at: str
    received_at: str


class PullVersionSummary(BaseModel):
    """Cloud-visible immutable version metadata returned during Pull/Sync."""

    model_config = ConfigDict(extra="forbid")

    version_id: str
    parent_version_id: str | None = None
    content_hash: str
    version_label: str | None = None
    commit_message: str | None = None
    origin: SkillOrigin
    status: SkillVersionStatus
    created_at: str
    received_at: str


class PullVersionContent(BaseModel):
    """Cloud algorithm content for one version; private local files are forbidden."""

    model_config = ConfigDict(extra="forbid")

    version: PullVersionSummary
    content: str


class PullVersionsPage(BaseModel):
    """One cursor page of cloud version metadata."""

    model_config = ConfigDict(extra="forbid")

    versions: list[PullVersionSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class CloudSkillSummary(BaseModel):
    """Project-scoped cloud Skill family summary."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    name: str
    published_head_id: str | None = None
    cloud_revision: int
    latest_version_id: str | None = None
    updated_at: str


class CloudSkillsPage(BaseModel):
    """One cursor page of cloud Skill families."""

    model_config = ConfigDict(extra="forbid")

    skills: list[CloudSkillSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class SyncCloudItem(BaseModel):
    """One local family state sent to cloud Sync."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    known_version_ids: list[str] = Field(default_factory=list)
    known_published_head_id: str | None = None
    known_cloud_revision: int | None = None


class SyncCloudRequest(BaseModel):
    """Strict multi-family cloud Sync request."""

    model_config = ConfigDict(extra="forbid")

    items: list[SyncCloudItem] = Field(default_factory=list)


class SyncCloudResultItem(BaseModel):
    """Cloud versions and pointer state missing from one local family."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    versions: list[PullVersionSummary] = Field(default_factory=list)
    published_head_id: str | None = None
    cloud_revision: int


class SyncCloudResult(BaseModel):
    """Strict multi-family cloud Sync result."""

    model_config = ConfigDict(extra="forbid")

    items: list[SyncCloudResultItem] = Field(default_factory=list)


class PromoteCloudRequest(BaseModel):
    """CAS request for changing only the cloud published pointer."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    version_id: str
    expected_cloud_revision: int


class PromoteCloudResult(BaseModel):
    """Cloud published pointer after a successful promote/rollback."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    published_head_id: str
    cloud_revision: int
    updated_at: str


class EvolveCloudRequest(BaseModel):
    """Idempotent request for cloud-orchestrated Skill evolution."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    cloud_skill_id: str
    base_version_id: str
    algorithm: str | None = None
    mode: SkillEvolveMode = "sync"


class EvolveCloudResult(BaseModel):
    """Result of sync or queued cloud evolution without a fabricated job id."""

    model_config = ConfigDict(extra="forbid")

    cloud_skill_id: str
    status: Literal["ok", "queued"]
    evolved: bool
    new_version_ids: list[str] = Field(default_factory=list)
    new_version_id: str | None = None
    pending_count: int
    threshold: int
    summarized_count: int = 0
    consumed_count: int = 0


class SkillPendingUpload(BaseModel):
    """One local outbox entry for an unconfirmed skill content snapshot."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    skill_id: str
    path: str
    skill_name: str
    cloud_skill_id: str | None = None
    parent_version_id: str = ""
    content_hash: str
    version_label: str | None = None
    content_cache_key: str
    attempts: int = 0
    next_retry_at: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class SkillPendingUploadsFile(BaseModel):
    """Top-level ``skill_pending_uploads.json`` schema."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    uploads: dict[str, SkillPendingUpload] = Field(default_factory=dict)


class SkillFlushResult(BaseModel):
    """Outcome of one pending upload retry."""

    skill_id: str
    content_hash: str
    parent_version_id: str
    uploaded: bool
    version_id: str | None = None
    registry_advanced: bool = False
    error: str | None = None


class SkillVersion(BaseModel):
    """Cloud version metadata returned by ``/v1/skills/*``."""

    model_config = ConfigDict(extra="ignore")

    version_id: str
    project_id: str | None = None
    cloud_skill_id: str
    skill_name: str
    content_hash: str
    parent_version_id: str | None = None
    version_label: str | None = None
    status: SkillVersionStatus
    origin: SkillOrigin
    created_at: str


class LocalSkillVersion(BaseModel):
    """Version metadata persisted in ``skill_history.json``."""

    model_config = ConfigDict(extra="ignore")

    version_id: str
    parent_version_id: str | None = None
    version_label: str | None = None
    status: SkillVersionStatus
    origin: SkillOrigin
    content_hash: str
    created_at: str

    @classmethod
    def from_cloud(cls, version: SkillVersion) -> LocalSkillVersion:
        """Create a local history entry from cloud metadata."""

        return cls(
            version_id=version.version_id,
            parent_version_id=version.parent_version_id,
            version_label=version.version_label,
            status=version.status,
            origin=version.origin,
            content_hash=version.content_hash,
            created_at=version.created_at,
        )


class SkillSummary(BaseModel):
    """Project-scoped cloud skill summary."""

    model_config = ConfigDict(extra="ignore")

    cloud_skill_id: str
    skill_name: str
    latest_version: SkillVersion
    published_head: SkillVersion | None = None


class SkillListData(BaseModel):
    """Response data returned by ``GET /v1/skills``."""

    model_config = ConfigDict(extra="ignore")

    skills: list[SkillSummary] = Field(default_factory=list)


class SkillRegisterData(BaseModel):
    """Response data returned by ``POST /v1/skills/register``."""

    model_config = ConfigDict(extra="ignore")

    cloud_skill_id: str
    version_id: str
    version_label: str | None = None
    content_hash: str
    status: SkillVersionStatus


class SkillVersionsData(BaseModel):
    """Response data returned by ``GET .../versions``."""

    model_config = ConfigDict(extra="ignore")

    versions: list[SkillVersion] = Field(default_factory=list)


class SkillContentData(BaseModel):
    """Response data returned by ``GET .../versions/{version_id}/content``."""

    model_config = ConfigDict(extra="ignore")

    version: SkillVersion
    content: str


class SkillEvolveData(BaseModel):
    """Response data returned by ``POST /v1/skills/evolve``.

    Mirrors :class:`mindmemos.typing.skill.SkillEvolveResult`. ``evolved`` is
    false when the pending trajectory count is below ``threshold``; otherwise
    ``new_version_id`` is the newest minted version and ``new_version_ids`` lists
    every version minted by the call (one per serial batch, oldest-first).
    """

    model_config = ConfigDict(extra="ignore")

    cloud_skill_id: str
    status: str = "ok"
    evolved: bool
    pending_count: int
    threshold: int
    new_version_id: str | None = None
    new_version_ids: list[str] = Field(default_factory=list)
    summarized_count: int = 0
    consumed_count: int = 0


class SkillSyncRequestItem(BaseModel):
    """One local skill state sent to ``POST /v1/skills/sync``."""

    cloud_skill_id: str
    local_version_id: str


class SkillSyncResult(BaseModel):
    """Published-head diff result returned by ``POST /v1/skills/sync``."""

    model_config = ConfigDict(extra="ignore")

    cloud_skill_id: str
    local_version_id: str
    has_update: bool
    published_head: SkillVersion | None = None
    gating_status: str


class SkillSyncData(BaseModel):
    """Response data returned by ``POST /v1/skills/sync``."""

    model_config = ConfigDict(extra="ignore")

    results: list[SkillSyncResult] = Field(default_factory=list)


class SkillHistoryEntry(BaseModel):
    """One skill bucket inside ``skill_history.json``."""

    model_config = ConfigDict(extra="ignore")

    skill_name: str
    versions: list[LocalSkillVersion] = Field(default_factory=list)
    last_pulled_at: str | None = None


class SkillHistoryFile(BaseModel):
    """Top-level ``skill_history.json`` schema."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    skills: dict[str, SkillHistoryEntry] = Field(default_factory=dict)


class SkillCheckoutPlan(BaseModel):
    """Planned local replacement for one managed skill."""

    skill_id: str
    path: str
    from_version_id: str
    to_version_id: str
    from_content_hash: str
    to_content_hash: str
    files: list[str] = Field(default_factory=list)
    backup_path: str | None = None


class SkillDiffResult(BaseModel):
    """Text diff between two cached skill versions."""

    skill_id: str
    from_version_id: str
    to_version_id: str
    diff: str


class SkillUpdateResult(BaseModel):
    """Outcome of checking or applying one skill update."""

    skill_id: str
    skill_name: str
    had_update: bool
    plan: SkillCheckoutPlan | None = None
    record: SkillRecord | None = None
    message: str = ""


SkillUpdatePlan = SkillCheckoutPlan
RollbackPlan = SkillCheckoutPlan
