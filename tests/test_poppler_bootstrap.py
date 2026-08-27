from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
import stat
import sys
from types import ModuleType
import warnings
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


def _zip_members(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, payload in entries:
                archive.writestr(name, payload)
    return output.getvalue()


def _replace_raw_member_name(payload: bytes, old: str, new: str) -> bytes:
    old_bytes = old.encode("utf-8")
    new_bytes = new.encode("utf-8")
    assert len(old_bytes) == len(new_bytes)
    assert payload.count(old_bytes) == 2, "expected local and central ZIP names"
    return payload.replace(old_bytes, new_bytes)


def _mark_zip_members_encrypted(payload: bytes) -> bytes:
    patched = bytearray(payload)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        found = 0
        while True:
            offset = patched.find(signature, start)
            if offset < 0:
                break
            flags = int.from_bytes(
                patched[offset + flag_offset : offset + flag_offset + 2],
                "little",
            )
            patched[offset + flag_offset : offset + flag_offset + 2] = (
                flags | 0x1
            ).to_bytes(2, "little")
            start = offset + 4
            found += 1
        assert found > 0
    return bytes(patched)


def _valid_entries() -> list[tuple[str | zipfile.ZipInfo, bytes]]:
    return [
        ("poppler-test/Library/bin/pdfinfo.exe", b"pdfinfo"),
        ("poppler-test/Library/bin/pdftoppm.exe", b"pdftoppm"),
    ]


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


def test_validated_members_rejects_raw_backslash_after_runtime_normalization() -> None:
    module = _load_script()
    safe_name = "poppler-test/Library/bin/escape.dll"
    raw_name = "poppler-test\\Library\\bin\\escape.dll"
    payload = _replace_raw_member_name(
        _zip_members([*_valid_entries(), (safe_name, b"escape")]),
        safe_name,
        raw_name,
    )
    contract = _contract(module, payload)

    with zipfile.ZipFile(BytesIO(payload), "r") as archive:
        member = archive.infolist()[-1]
        assert member.orig_filename == raw_name
        # Reproduce ZipInfo's Windows host normalization while retaining the raw name.
        member.filename = member.filename.replace("\\", "/")
        with pytest.raises(module.PopplerBootstrapError, match="unsafe path"):
            module._validated_members(archive, contract)


def test_install_rejects_raw_backslash_member_independent_of_host_writer(
    tmp_path: Path,
) -> None:
    module = _load_script()
    safe_name = "poppler-test/Library/bin/escape.dll"
    raw_name = "poppler-test\\Library\\bin\\escape.dll"
    payload = _replace_raw_member_name(
        _zip_members([*_valid_entries(), (safe_name, b"escape")]),
        safe_name,
        raw_name,
    )

    with pytest.raises(module.PopplerBootstrapError, match="unsafe path"):
        module.install_pinned_poppler(
            tmp_path / "installed",
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )


@pytest.mark.parametrize("name", ["/rooted/escape.dll", "C:/drive/escape.dll"])
def test_install_rejects_rooted_and_drive_zip_paths(
    tmp_path: Path,
    name: str,
) -> None:
    module = _load_script()
    payload = _zip_members([*_valid_entries(), (name, b"escape")])

    with pytest.raises(module.PopplerBootstrapError, match="unsafe path"):
        module.install_pinned_poppler(
            tmp_path / "installed",
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )


def test_install_rejects_duplicate_zip_member(tmp_path: Path) -> None:
    module = _load_script()
    duplicate = "poppler-test/Library/bin/dependency.dll"
    payload = _zip_members(
        [*_valid_entries(), (duplicate, b"first"), (duplicate, b"second")]
    )

    with pytest.raises(module.PopplerBootstrapError, match="duplicate member"):
        module.install_pinned_poppler(
            tmp_path / "installed",
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )


def test_install_rejects_symbolic_link_zip_member(tmp_path: Path) -> None:
    module = _load_script()
    link = zipfile.ZipInfo("poppler-test/Library/bin/link.dll")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    payload = _zip_members([*_valid_entries(), (link, b"dependency.dll")])

    with pytest.raises(module.PopplerBootstrapError, match="symbolic link"):
        module.install_pinned_poppler(
            tmp_path / "installed",
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )


def test_install_rejects_encrypted_zip_member(tmp_path: Path) -> None:
    module = _load_script()
    payload = _mark_zip_members_encrypted(_zip_members(_valid_entries()))

    with pytest.raises(module.PopplerBootstrapError, match="encrypted member"):
        module.install_pinned_poppler(
            tmp_path / "installed",
            contract=_contract(module, payload),
            opener=lambda *_args, **_kwargs: _Response(payload),
        )


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
