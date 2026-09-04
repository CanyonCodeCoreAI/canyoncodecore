"""Shared result and YAML primitives for validation checks."""

import os
from typing import ClassVar

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a CanyonOS dependency
    raise RuntimeError("validate.py needs pyyaml: pip install pyyaml") from None


ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"


class LineDict(dict):
    """A mapping that remembers where it and each of its keys were written."""

    line = 0
    key_lines: ClassVar[dict] = {}


class LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node):
    data = LineDict()
    yield data
    data.update(loader.construct_mapping(node, deep=False))
    data.line = node.start_mark.line + 1
    data.key_lines = {
        key.value: key.start_mark.line + 1
        for key, _ in node.value
        if isinstance(key, yaml.ScalarNode)
    }


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def line_of(mapping, key=None):
    if not isinstance(mapping, LineDict):
        return 0
    if key is not None:
        return mapping.key_lines.get(key, mapping.line)
    return mapping.line


def load_yaml(path):
    """Parse path and return ``(data, error)`` without raising."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.load(handle, Loader=LineLoader), None
    except Exception as exc:  # noqa: BLE001 - every parse failure is a finding
        return None, str(exc)


class Report:
    def __init__(self, project_dir, capabilities):
        self.project_dir = project_dir
        self.capabilities = capabilities
        self.findings = []

    def add(self, check, level, path, line, summary, mechanism):
        self.findings.append(
            {
                "check": check,
                "level": level,
                "path": self.rel(path) if path else "",
                "line": line or 0,
                "summary": summary,
                "mechanism": mechanism,
            }
        )

    def error(self, check, path, line, summary, mechanism):
        self.add(check, ERROR, path, line, summary, mechanism)

    def warn(self, check, path, line, summary, mechanism):
        self.add(check, WARN, path, line, summary, mechanism)

    def unavailable(self, check, summary):
        self.add(check, INFO, "", 0, summary, "")

    def rel(self, path):
        try:
            return os.path.relpath(path, self.project_dir)
        except ValueError:
            return path

    def counts(self):
        errors = sum(1 for finding in self.findings if finding["level"] == ERROR)
        warnings = sum(1 for finding in self.findings if finding["level"] == WARN)
        return errors, warnings

