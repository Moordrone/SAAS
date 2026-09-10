"""Engineering-layer errors.

These are physics and schema problems, not HTTP problems. The API layer maps
them onto problem+json; the engine itself knows nothing about HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class EngineeringError(Exception):
    """Base for everything the engine rejects."""


class UnitError(EngineeringError):
    pass


class UnknownComponent(EngineeringError):
    pass


class UnknownMaterial(EngineeringError):
    pass


@dataclass
class Issue:
    """One validation finding, addressed to a specific parameter."""

    path: str
    code: str
    message: str
    severity: str = "error"          # error | warning | info
    suggestion: str | None = None

    def as_dict(self) -> dict:
        d = {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_valid(self) -> bool:
        """A project is runnable when nothing is missing and nothing is wrong.

        Warnings do not block: telling an engineer their substrate is unusually
        thick is useful, but refusing to simulate it would be presumptuous.
        """
        return not self.errors and not self.missing

    def as_dict(self) -> dict:
        return {
            "valid": self.is_valid,
            "missing": self.missing,
            "issues": [i.as_dict() for i in self.issues],
        }
