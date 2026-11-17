"""A run: the code, its input files and its limits; and what came back.

Shared by the MCP server (which checks a request against the limits) and the runner (which
executes it). Files are flat names, base64 in transit; nothing outside the work directory is
ever read or written.
"""

from __future__ import annotations

import base64
import re
from dataclasses import asdict, dataclass, field
from typing import Any

LANGUAGES = ("python",)
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class Refused(ValueError):
    """The request breaks a limit: the message says which, never echoing the code or data."""


@dataclass(frozen=True)
class Limits:
    seconds: int = 30
    memory_mb: int = 512
    output_kb: int = 64
    files_out: int = 20
    files_out_kb: int = 5120


@dataclass(frozen=True)
class Job:
    language: str
    code: str
    files: dict[str, bytes] = field(default_factory=dict)
    limits: Limits = Limits()

    def wire(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "code": self.code,
            "limits": asdict(self.limits),
            "files": {k: base64.b64encode(v).decode() for k, v in self.files.items()},
        }

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Job:
        return cls(
            language=str(raw["language"]),
            code=str(raw["code"]),
            limits=Limits(**raw.get("limits") or {}),
            files={str(k): base64.b64decode(v) for k, v in (raw.get("files") or {}).items()},
        )


@dataclass(frozen=True)
class Result:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    out_of_memory: bool = False
    truncated: bool = False
    duration_ms: int = 0
    files: dict[str, bytes] = field(default_factory=dict)

    def wire(self) -> dict[str, Any]:
        out = asdict(self)
        out["files"] = {k: base64.b64encode(v).decode() for k, v in self.files.items()}
        return out

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Result:
        files = {str(k): base64.b64decode(v) for k, v in (raw.get("files") or {}).items()}
        return cls(**{**{k: v for k, v in raw.items() if k != "files"}, "files": files})


def check_name(name: str) -> str:
    if not _FILE.match(name) or name in (".", ".."):
        raise Refused(
            f"file names are plain names (letters, digits, '.', '_', '-'), no folders: {name[:40]!r}"
        )
    return name


def check(job: Job, *, max_code_kb: int, max_input_kb: int, max_seconds: int) -> Job:
    if job.language not in LANGUAGES:
        raise Refused(f"language must be one of {', '.join(LANGUAGES)}")
    if not job.code.strip():
        raise Refused("there's no code to run")
    if len(job.code.encode()) > max_code_kb * 1024:
        raise Refused(f"code is limited to {max_code_kb} KB")
    for name in job.files:
        check_name(name)
    if sum(len(v) for v in job.files.values()) > max_input_kb * 1024:
        raise Refused(f"input files are limited to {max_input_kb} KB in all")
    if not 1 <= job.limits.seconds <= max_seconds:
        raise Refused(f"timeout_s must be 1-{max_seconds}")
    return job
