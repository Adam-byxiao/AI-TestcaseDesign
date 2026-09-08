"""结构化产物模型。仅使用标准库，供 CLI、API 和测试共同使用。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SourceRef:
    chunk_id: str
    start: int = 0
    end: int = 0
    headings: list[str] = field(default_factory=list)


@dataclass
class Module:
    id: str
    name: str
    entry: str = ""
    requirement_ids: list[str] = field(default_factory=list)


@dataclass
class Requirement:
    id: str
    module: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    exception_flows: list[str] = field(default_factory=list)
    priority: str = "P1"
    source_refs: list[SourceRef] = field(default_factory=list)
    fingerprint: str = ""
    id_source: str = "prd"


@dataclass
class RunManifest:
    schemaVersion: str = "2.0"
    toolVersion: str = ""
    runId: str = ""
    parentRunId: str | None = None
    product: str = ""
    version: str = ""
    prd: str | None = None
    prdHash: str = ""
    model: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, dict[str, str]] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    artifacts: list[dict[str, str]] = field(default_factory=list)
    status: str = "running"
    createdAt: str = ""
    updatedAt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunState:
    schemaVersion: str = "1.0"
    runId: str = ""
    inputHash: str = ""
    promptHash: str = ""
    status: str = "pending"
    stage: str = ""
    modules: dict[str, str] = field(default_factory=dict)
    updatedAt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

