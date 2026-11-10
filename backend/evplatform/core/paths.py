"""Resolve operator-owned runtime files without depending on process cwd."""
from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def runtime_data_dir(configured: str | Path | None = None) -> Path:
    """An explicit data directory wins; the installed checkout is the default."""
    if configured is None:
        return PROJECT_ROOT / "data"
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def runtime_data_file(name: str, configured: str | Path | None = None) -> Path:
    if not name or Path(name).name != name:
        raise ValueError("Runtime data file must be a single filename")
    return runtime_data_dir(configured) / name


def runtime_database_path(configured: str | Path) -> Path:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()
