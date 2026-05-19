"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.audit import AuditLog
from dlw.db.models.casbin_rule import CasbinRule
from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory
from dlw.db.models.source import SourceBlacklist, SourceSpeedSample, SubtaskChunk
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject, SubtaskObjectRef
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.db.models.usage import QuotaSnapshot, UsageRecord

__all__ = [
    "AuditLog", "CasbinRule", "DownloadTask", "Executor", "ExecutorStatusHistory",
    "FileSubTask", "Project", "QuotaSnapshot", "SourceBlacklist", "SourceSpeedSample",
    "StorageBackend", "StorageObject", "SubtaskChunk", "SubtaskObjectRef",
    "Tenant", "UsageRecord", "User",
]
