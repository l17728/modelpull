"""ORM models. Importing this module also registers them with Base.metadata."""

from dlw.db.models.ai import AIConversation, AIMessage, AITokenUsage, AIToolCall
from dlw.db.models.audit import AuditLog
from dlw.db.models.casbin_rule import CasbinRule
from dlw.db.models.chunk_throughput import ChunkThroughputSample
from dlw.db.models.device_auth import DeviceAuthSession
from dlw.db.models.executor import Executor
from dlw.db.models.executor_status_history import ExecutorStatusHistory
from dlw.db.models.local_credentials import LocalCredential
from dlw.db.models.replication import ReplicationJob
from dlw.db.models.source import SourceBlacklist, SourceSpeedSample, SubtaskChunk
from dlw.db.models.storage import StorageBackend
from dlw.db.models.storage_object import StorageObject, StoragePhysicalKey, SubtaskObjectRef
from dlw.db.models.task import DownloadTask, FileSubTask
from dlw.db.models.tenant import Project, Tenant, User
from dlw.db.models.usage import QuotaSnapshot, UsageRecord

__all__ = [
    "AIConversation", "AIMessage", "AIToolCall", "AITokenUsage",
    "AuditLog", "CasbinRule", "ChunkThroughputSample",
    "DeviceAuthSession", "DownloadTask", "Executor",
    "ExecutorStatusHistory", "FileSubTask", "LocalCredential", "Project", "QuotaSnapshot",
    "ReplicationJob", "SourceBlacklist", "SourceSpeedSample", "StorageBackend", "StorageObject",
    "StoragePhysicalKey", "SubtaskChunk", "SubtaskObjectRef",
    "Tenant", "UsageRecord", "User",
]
