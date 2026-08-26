from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import sys
from types import ModuleType
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_pinned_poppler.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_pinned_poppler", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: bytes, content_length: int | None = None) -> None:
        self._stream = BytesIO(payload)
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.read_sizes: list[int] = []

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._stream.read(size)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _contract(
    module: ModuleType,
    payload: bytes,
    *,
    max_entries: int = 8,
    max_uncompressed_bytes: int = 1024,
) -> object:
    return module.ArchiveContract(
        url="https://example.invalid/pinned.zip",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        root_prefix="poppler-test/",
        max_entries=max_entries,
        max_uncompressed_bytes=max_uncompressed_bytes,
        required_relative_files=(
            "Library/bin/pdfinfo.exe",
            "Library/bin/pdftoppm.exe",
        ),
    )


@pytest.mark.parametrize(
    ("payload", "expected_size", "message"),
    [
        (b"12345", 4, "exceeds pinned size"),
        (b"1234", 5, "truncated"),
    ],
)
def test_download_rejects_overflow_and_truncation_without_leaving_archive(
    tmp_path: Path,
    payload: bytes,
    expected_size: int,
    message: str,
) -> None:
    module = _load_script()
    destination = tmp_path / "pinned.zip"
    response = _Response(payload)

    with pytest.raises(module.PopplerBootstrapError, match=message):
        module.download_verified_archive(
            destination,
            url="https://example.invalid/pinned.zip",
            expected_size=expected_size,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            opener=lambda *_args, **_kwargs: response,
        )

    assert not destination.exists()
    assert max(response.read_sizes) <= expected_size


def test_install_rejects_bad_hash_before_creating_extraction_destination(
    tmp_path: Path,
) -> None:
    module = _load_script()
    payload = _zip_bytes(
        {
            "poppler-test/Library/bin/pdfinfo.exe": b"pdfinfo",
            "poppler-test/Library/bin/pdftoppm.exe": b"pdftoppm",
        }
    )
    contract = _contract(module, payload)
    contract = module.ArchiveContract(
        url=contract.url,
        expected_size=contract.expected_size,
        expected_sha256="0" * 64,
        root_prefix=contract.root_prefix,
        max_entries=contract.max_entries,
        max_uncompressed_bytes=contract.max_uncompressed_bytes,
        required_relative_files=contract.required_relative_files,
    )
    destination = tmp_path / "installed"

    with pytest.raises(module.PopplerBootstrapError, match="SHA-256 mismatch"):
        module.install_pinned_poppler(
            destination,
            contract=contract,
            opener=lambda *_args, **_kwargs: _Response(payload, len(payload)),
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("extra_name", "message"),
    [
        ("../escape.exe", "unsafe path"),
        ("other-root/file.dll", "outside pinned root"),
        ("poppler-test\\Library\\bin\\escape.dll", "unsafe path"),
    ],
)
def test_install_rejects_unsafe_zip_member_paths_before_extraction(
    tmp_path: Path,
    extra_name: str,
    message: str,
) -> None:
    module = _load_script()
    payload = _zip_bytes(
        {
            "poppler-test/Library/bin/pdfinfo.exe": b"pdfinfo",
            "poppler-test/Library/bin/pdftoppm.exe": b"pdftoppm",
            extra_name: b"escape",
        }
    )
    destination = tmp_path / "installed"

    with pytest.raises(module.PopplerBootstrapError, match=message):
        module.install_pinned_poppler(
            destination,
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )

    assert not destination.exists()
    assert not (tmp_path / "escape.exe").exists()


@pytest.mark.parametrize(
    ("max_entries", "max_uncompressed_bytes", "message"),
    [
        (2, 1024, "entry count"),
        (8, 15, "uncompressed size"),
    ],
)
def test_install_rejects_zip_resource_bounds_before_extraction(
    tmp_path: Path,
    max_entries: int,
    max_uncompressed_bytes: int,
    message: str,
) -> None:
    module = _load_script()
    payload = _zip_bytes(
        {
            "poppler-test/Library/bin/pdfinfo.exe": b"pdfinfo",
            "poppler-test/Library/bin/pdftoppm.exe": b"pdftoppm",
            "poppler-test/Library/bin/dependency.dll": b"dependency",
        }
    )
    destination = tmp_path / "installed"

    with pytest.raises(module.PopplerBootstrapError, match=message):
        module.install_pinned_poppler(
            destination,
            contract=_contract(
                module,
                payload,
                max_entries=max_entries,
                max_uncompressed_bytes=max_uncompressed_bytes,
            ),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )

    assert not destination.exists()


def test_install_extracts_only_the_validated_pinned_tree(
    tmp_path: Path,
) -> None:
    module = _load_script()
    entries = {
        "poppler-test/Library/bin/pdfinfo.exe": b"pdfinfo",
        "poppler-test/Library/bin/pdftoppm.exe": b"pdftoppm",
        "poppler-test/Library/bin/dependency.dll": b"dependency",
    }
    payload = _zip_bytes(entries)
    destination = tmp_path / "installed"

    poppler_bin = module.install_pinned_poppler(
        destination,
        contract=_contract(module, payload),
        opener=lambda *_args, **_kwargs: _Response(payload, len(payload)),
    )

    assert poppler_bin == destination / "poppler-test" / "Library" / "bin"
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == entries
