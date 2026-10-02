"""Configuration loading with dotted-path overrides and global seed control.

Nothing in ``src/`` hardcodes a tunable value; every parameter is read from
``config/default.yaml`` through this module. That constraint is what makes
``scripts/ablations.py`` a loop over config variants instead of a series of code
edits, and it is why each row of the ablation table in README.md corresponds to
an override string rather than a branch.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Config", "load_config", "set_global_seed"]


def _find_project_root(start: Path | None = None) -> Path:
    """Walk upward looking for the directory containing ``config/``.

    Lets scripts be invoked from anywhere without relative-path breakage.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "config").is_dir() and (parent / "data").is_dir():
            return parent
    return Path.cwd()


PROJECT_ROOT = _find_project_root()


def _coerce(value: str) -> Any:
    """Parse a CLI override value into the narrowest sensible type.

    ``--set rerank.alpha=0.5`` must yield a float, not the string ``"0.5"``,
    or arithmetic downstream silently concatenates.
    """
    lowered = value.strip().lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if "," in value:
        return [_coerce(v) for v in value.split(",")]
    return value


class Config:
    """Dict-backed config with dotted-path access.

    Deliberately not a nested dataclass hierarchy: the ablation harness needs to
    set arbitrary leaves by string path, which a dataclass tree makes awkward.
    Validation happens at the point of use rather than up front.
    """

    def __init__(self, data: dict[str, Any], source_path: Path | None = None) -> None:
        self._data = data
        self.source_path = source_path

    # -- access --------------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        """Read a value by dotted path, e.g. ``config.get("rerank.alpha")``."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        """Read a value, raising if absent. Use for parameters with no safe default."""
        sentinel = object()
        value = self.get(path, sentinel)
        if value is sentinel:
            raise KeyError(f"required config key missing: {path!r}")
        return value

    def set(self, path: str, value: Any) -> None:
        """Write a value by dotted path, creating intermediate dicts as needed."""
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise TypeError(f"cannot set {path!r}: {part!r} is not a mapping")
        node[parts[-1]] = value

    def __getitem__(self, path: str) -> Any:
        return self.require(path)

    def __contains__(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

    # -- paths ---------------------------------------------------------------

    def path(self, key: str) -> Path:
        """Resolve a ``paths.*`` entry against the project root."""
        raw = self.require(f"paths.{key}")
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p

    # -- optional LLM stages -------------------------------------------------

    def llm_model(self, stage_key: str | None = None) -> str:
        """Resolve the model id for an optional LLM stage.

        No provider or model id is hardcoded anywhere in ``src/``. The id is
        looked up in this order:

        1. the stage's own key (e.g. ``generation.llm.model``),
        2. the shared ``llm.model`` default in the config,
        3. the ``LLM_MODEL`` environment variable.

        The LLM stages are off by default, so the common case never reaches
        here; when one is switched on without an id configured, failing with an
        explicit message beats sending a request naming a model the account may
        not have.
        """
        candidates = [self.get(stage_key) if stage_key else None,
                      self.get("llm.model"),
                      os.environ.get("LLM_MODEL")]
        for value in candidates:
            if value:
                return str(value)
        raise ValueError(
            "no LLM model configured -- set LLM_MODEL in the environment, "
            f"or llm.model{f' / {stage_key}' if stage_key else ''} in the config. "
            "The optional LLM stages are provider-agnostic; supply the model id "
            "your SDK expects."
        )

    # -- derivation ----------------------------------------------------------

    def variant(self, name: str, overrides: dict[str, Any]) -> "Config":
        """Return a deep copy with ``overrides`` applied.

        The ablation harness builds each table row this way, so a row is fully
        described by its override dict and is reproducible from the log.
        """
        import copy

        clone = Config(copy.deepcopy(self._data), self.source_path)
        clone.set("experiment.name", name)
        for path, value in overrides.items():
            clone.set(path, value)
        return clone

    def apply_overrides(self, pairs: list[str]) -> "Config":
        """Apply ``["key.path=value", ...]`` strings from the command line."""
        for pair in pairs:
            if "=" not in pair:
                raise ValueError(f"--set expects key.path=value, got {pair!r}")
            key, _, raw = pair.partition("=")
            self.set(key.strip(), _coerce(raw))
        return self

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def __repr__(self) -> str:
        return f"Config(experiment={self.get('experiment.name')!r}, path={self.source_path})"


def set_global_seed(seed: int) -> None:
    """Seed every RNG the pipeline can reach.

    Called automatically by :func:`load_config`. numpy is seeded only if it is
    installed, since the core pipeline does not require it.
    """
    random.seed(seed)
    try:  # optional dependency
        import numpy as np

        np.random.seed(seed)
    except ImportError:
        pass


def load_config(
    path: str | Path | None = None,
    overrides: list[str] | None = None,
) -> Config:
    """Load a YAML config, apply CLI overrides, and set the global seed.

    Args:
        path: Config file. Defaults to ``config/default.yaml``.
        overrides: ``["rerank.alpha=0.5", ...]`` from ``--set``.

    Returns:
        A :class:`Config`. The global seed is applied as a side effect, so this
        must be called before anything that samples.
    """
    cfg_path = Path(path) if path else PROJECT_ROOT / "config" / "default.yaml"
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path
    if not cfg_path.exists():
        raise FileNotFoundError(f"config not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    config = Config(data, cfg_path)
    if overrides:
        config.apply_overrides(overrides)

    set_global_seed(int(config.get("experiment.seed", 42)))
    return config
