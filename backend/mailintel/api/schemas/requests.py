# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PayloadModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    def payload(self) -> dict[str, object]:
        return self.model_dump(exclude_unset=True)


class QueuePriorityPayload(BaseModel):
    priority: int = Field(ge=0, le=2147483647, strict=True)


class AccountPayload(PayloadModel):
    id: int | None = None
    name: str = ""
    provider_type: str = ""
    auth_mode: str = "config"
    secret_locator: str | None = None
    password: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    sync_window_days: int | None = None
    status: str = "active"


class MailboxUpdatePayload(PayloadModel):
    monitored: bool = False


class UserProfilePayload(PayloadModel):
    name: str = ""
    role: str = ""
    organization: str = ""
    aliases: list[str] = Field(default_factory=list)
    email_addresses: list[str] = Field(default_factory=list)
    collaborators: list[str] = Field(default_factory=list)
    communication_preferences: str = ""
    about: str = ""


class LLMPresetPayload(PayloadModel):
    id: int | None = None
    name: str = "Local Ollama"
    provider: str = "ollama"
    enabled: bool = False
    priority: int = 100
    auth_mode: str = "env"
    secret_locator: str = ""
    endpoint_url: str = "http://127.0.0.1:11434"
    text_model: str = "llama3.1"
    embedding_model: str = "nomic-embed-text"
    temperature: float = 0.2
    text_context_window: int = Field(default=8192, ge=1)
    embedding_context_window: int = Field(default=8192, ge=1)
    max_output_tokens: int = 800
    initial_timeout_seconds: float | None = Field(default=None, gt=0, le=86400)
    streaming_timeout_seconds: float | None = Field(default=None, gt=0, le=86400)
    notes: str = ""
    concurrency: int = Field(default=1, ge=1, le=20)
    capabilities: list[str] | None = None


class PresetReorderPayload(PayloadModel):
    presets: list[dict[str, Any]] = Field(default_factory=list)


class PipelineSettingsPayload(PayloadModel):
    canonical_embedding_model: str | None = None


class InterestProfilePayload(PayloadModel):
    id: int | None = None
    name: str = ""
    description: str = ""
    collaborators: list[str] = Field(default_factory=list)
    signals: str = ""
    priority_weight: float = 1.0
    dashboard_prominence: int = 1
    summary_detail: str = "standard"
    watch_mode: str = "watch"


class MaintenanceResetPayload(PayloadModel):
    source_ids: list[int] = Field(default_factory=list)


class ConfigBackupPayload(PayloadModel):
    path: str = ""
    passphrase: str = ""


class SummaryListPayload(PayloadModel):
    requests: list[dict[str, Any]] = Field(default_factory=list)
    mailbox_ids: list[int] | None = None


class SummaryRebuildPayload(PayloadModel):
    start_date: str = ""
    duration: str = ""


class TopicRenamePayload(PayloadModel):
    name: str = ""


class ChatPayload(PayloadModel):
    query: str


class SmartSearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=12000)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class MessageStarPayload(BaseModel):
    star: int = Field(ge=0, le=2147483647, strict=True)
