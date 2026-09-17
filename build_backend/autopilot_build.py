"""A build backend inside the tree itself, without a single external dependency.

Codex Autopilot is pure Python on the standard library. Pulling setuptools
from the network to build it means requiring the network where there is
no network code: a fresh offline install of the sources failed before it
got to the tests, and a new user could not install the product at all.

The backend lives in the tree (PEP 517 ``backend-path``) and requires
nothing, so ``pip install .`` works in an empty venv without network and
without a pre-installed setuptools. Exactly what the package has is built:
the packages under ``src`` and one console command.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WHEEL_TAG = "py3-none-any"
_SDIST_INCLUDE = ("pyproject.toml", "LICENSE", "README.md")
_SDIST_TREES = ("src", "build_backend")


def _metadata() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project


def _distribution(project: dict[str, object]) -> tuple[str, str]:
    name = str(project["name"]).replace("-", "_").replace(".", "_")
    return name, str(project["version"])


def _package_files() -> list[tuple[str, Path]]:
    """Collect (path in the wheel, path on disk) for every module of the package."""

    members: list[tuple[str, Path]] = []
    source_root = ROOT / "src"
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        members.append((path.relative_to(source_root).as_posix(), path))
    if not members:
        raise RuntimeError("src contains no module - nothing to build")
    return members


def _record_line(arcname: str, payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode('ascii')},{len(payload)}"


def _core_metadata(project: dict[str, object]) -> bytes:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
    ]
    summary = project.get("description")
    if summary:
        lines.append(f"Summary: {summary}")
    requires_python = project.get("requires-python")
    if requires_python:
        lines.append(f"Requires-Python: {requires_python}")
    license_field = project.get("license")
    if isinstance(license_field, dict) and license_field.get("text"):
        lines.append(f"License: {license_field['text']}")
    elif isinstance(license_field, str):
        lines.append(f"License: {license_field}")
    for requirement in project.get("dependencies", ()) or ():
        lines.append(f"Requires-Dist: {requirement}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _wheel_metadata() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: codex-autopilot in-tree backend\n"
        "Root-Is-Purelib: true\n"
        f"Tag: {WHEEL_TAG}\n"
    ).encode("utf-8")


def _entry_points(project: dict[str, object]) -> bytes | None:
    scripts = project.get("scripts") or {}
    if not scripts:
        return None
    lines = ["[console_scripts]"]
    lines.extend(f"{name} = {target}" for name, target in sorted(scripts.items()))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_wheel(wheel_directory: str, members: list[tuple[str, bytes]]) -> str:
    project = _metadata()
    name, version = _distribution(project)
    dist_info = f"{name}-{version}.dist-info"
    payload: list[tuple[str, bytes]] = list(members)
    payload.append((f"{dist_info}/METADATA", _core_metadata(project)))
    payload.append((f"{dist_info}/WHEEL", _wheel_metadata()))
    entry_points = _entry_points(project)
    if entry_points is not None:
        payload.append((f"{dist_info}/entry_points.txt", entry_points))
    license_path = ROOT / "LICENSE"
    if license_path.is_file():
        payload.append((f"{dist_info}/licenses/LICENSE", license_path.read_bytes()))

    record = [_record_line(arcname, data) for arcname, data in payload]
    record.append(f"{dist_info}/RECORD,,")
    payload.append((f"{dist_info}/RECORD", ("\n".join(record) + "\n").encode("utf-8")))

    filename = f"{name}-{version}-{WHEEL_TAG}.whl"
    target = Path(wheel_directory) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, data in payload:
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return filename


# --- PEP 517 -----------------------------------------------------------


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    project = _metadata()
    name, version = _distribution(project)
    dist_info = Path(metadata_directory) / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_bytes(_core_metadata(project))
    (dist_info / "WHEEL").write_bytes(_wheel_metadata())
    entry_points = _entry_points(project)
    if entry_points is not None:
        (dist_info / "entry_points.txt").write_bytes(entry_points)
    return dist_info.name


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    members = [(arcname, path.read_bytes()) for arcname, path in _package_files()]
    return _write_wheel(wheel_directory, members)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    project = _metadata()
    name, _ = _distribution(project)
    pth = f"{ROOT / 'src'}\n".encode("utf-8")
    return _write_wheel(wheel_directory, [(f"_{name}_editable.pth", pth)])


def build_sdist(sdist_directory, config_settings=None):
    project = _metadata()
    name, version = _distribution(project)
    base = f"{name}-{version}"
    target = Path(sdist_directory) / f"{base}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)

    def _add(archive: tarfile.TarFile, path: Path, arcname: str) -> None:
        info = archive.gettarinfo(str(path), arcname=arcname)
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        with path.open("rb") as handle:
            archive.addfile(info, handle)

    with tarfile.open(target, "w:gz") as archive:
        for relative in _SDIST_INCLUDE:
            path = ROOT / relative
            if path.is_file():
                _add(archive, path, f"{base}/{relative}")
        for tree in _SDIST_TREES:
            root = ROOT / tree
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                _add(archive, path, f"{base}/{path.relative_to(ROOT).as_posix()}")
        pkg_info = io.BytesIO(_core_metadata(project))
        info = tarfile.TarInfo(f"{base}/PKG-INFO")
        info.size = len(pkg_info.getvalue())
        info.mtime = 0
        archive.addfile(info, pkg_info)
    return target.name
