"""Source evidence from one timestamped recording; no inferred clock alignment."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: str
    severity: Literal["warning", "error"]
    offset: int
    message: str
    record_index: int


@dataclass(frozen=True, slots=True)
class Record:
    index: int
    offset: int
    timestamp_us: int
    wire_version: Literal[1, 2]
    sequence: int
    system_id: int
    component_id: int
    message_id: int
    message_name: str | None
    fields: Mapping[str, object] | None
    checksum_status: Literal["valid", "unverified"]
    raw_frame: bytes

    @property
    def frame_offset(self) -> int:
        return self.offset + 8

    @property
    def end_offset(self) -> int:
        return self.frame_offset + len(self.raw_frame)


@dataclass(frozen=True, slots=True)
class ImportResult:
    source_name: str
    raw_bytes: bytes
    sha256: str
    profile: str
    dialect: str
    decoder_version: str
    records: tuple[Record, ...]
    issues: tuple[ImportIssue, ...]
    traversal: Literal["complete", "stopped", "empty"]
    consumed_bytes: int

    @property
    def remaining_bytes(self) -> int:
        return len(self.raw_bytes) - self.consumed_bytes

    @property
    def decoded_count(self) -> int:
        return sum(record.fields is not None for record in self.records)

    @property
    def opaque_count(self) -> int:
        return len(self.records) - self.decoded_count
