from __future__ import annotations

from datetime import UTC, datetime
import errno
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import httpx
import pytest

import web_translator.pdf_acquire as acquire_module
from web_translator.pdf_acquire import MAX_PDF_BYTES, PdfAcquireError, acquire_pdf


FIXED_TIME = datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC)
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_acquire_local_pdf_copies_to_fresh_inode_and_records_private_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "입력 자료" / "보고서.pdf"
    source.parent.mkdir()
    source.write_bytes(PDF)
    run_dir = tmp_path / "실행 공간"

    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    copied = run_dir / "source.pdf"
    assert copied.read_bytes() == PDF
    assert not os.path.samefile(copied, source)
    assert record.input_kind == "local"
    assert record.requested_source == "보고서.pdf"
    assert record.final_source == "보고서.pdf"
    assert record.content_type == "application/pdf"
    assert record.byte_length == len(PDF)
    assert record.acquired_at == "2026-08-21T01:02:03Z"
    assert record.redirects == []
    assert record.warnings == []


@pytest.mark.parametrize(
    "source",
    [r"C:\입력 자료\보고서.pdf", r"\\server\공유 폴더\보고서.pdf"],
)
def test_windows_paths_are_classified_as_local_before_url_dispatch(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def local(
        path: Path,
        run_dir: Path,
        *,
        now: datetime | None,
        metadata_writer: object,
    ) -> object:
        seen.append(path)
        raise PdfAcquireError("local classifier reached")

    monkeypatch.setattr(acquire_module, "_acquire_local_pdf", local)

    with pytest.raises(PdfAcquireError, match="local classifier reached"):
        acquire_pdf(source, tmp_path / "run", now=FIXED_TIME)

    assert seen == [Path(source)]


@pytest.mark.parametrize("kind", ["link", "directory"])
def test_acquire_local_pdf_rejects_links_and_non_regular_files(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "source.pdf"
    if kind == "link":
        target = tmp_path / "target.pdf"
        target.write_bytes(PDF)
        source.symlink_to(target)
    else:
        source.mkdir()

    with pytest.raises(PdfAcquireError, match="regular file|link or reparse"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)


def test_acquire_local_pdf_rejects_non_pdf_signature_without_publishing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"not a PDF")
    run_dir = tmp_path / "run"

    with pytest.raises(PdfAcquireError, match="PDF signature"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (run_dir / "source.pdf").exists()


def test_acquire_pdf_rejects_nonempty_run_directory_without_publishing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(PdfAcquireError, match="must be empty"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert (run_dir / "existing.txt").read_text(encoding="utf-8") == "keep"
    assert not (run_dir / "source.pdf").exists()


def test_acquire_pdf_rejects_symlinked_run_directory_without_publishing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    target = tmp_path / "target"
    target.mkdir()
    run_dir = tmp_path / "run"
    run_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(PdfAcquireError, match="link or reparse"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (target / "source.pdf").exists()


def test_acquire_pdf_rejects_reparse_run_directory_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original_lstat = acquire_module.os.lstat

    def reparse_lstat(path: str | Path) -> os.stat_result | SimpleNamespace:
        if Path(path) == run_dir:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=acquire_module._REPARSE_POINT,
            )
        return original_lstat(path)

    monkeypatch.setattr(acquire_module.os, "lstat", reparse_lstat)

    with pytest.raises(PdfAcquireError, match="link or reparse"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (run_dir / "source.pdf").exists()


def test_acquire_local_pdf_rejects_file_when_identity_changes_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(PDF)
    original_copy = acquire_module._copy_and_hash_pdf

    def replace_during_copy(*args: object, **kwargs: object) -> tuple[str, int]:
        replacement.replace(source)
        return original_copy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acquire_module, "_copy_and_hash_pdf", replace_during_copy)

    with pytest.raises(PdfAcquireError, match="changed identity"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)

    assert not (tmp_path / "run" / "source.pdf").exists()


def test_acquire_local_pdf_stops_streaming_above_fifty_mebibytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    with source.open("wb") as stream:
        stream.write(PDF)
        stream.truncate(MAX_PDF_BYTES + 1)

    with pytest.raises(PdfAcquireError, match="size limit"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)

    assert not (tmp_path / "run" / "source.pdf").exists()


def test_acquire_pdf_stages_with_only_run_directory_write_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original_mkdir = acquire_module.os.mkdir

    def deny_parent_creates(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        candidate = Path(path)
        if (
            dir_fd is None
            and candidate.parent == tmp_path
            and candidate.name.startswith(".pdf-acquiring-")
        ):
            raise PermissionError("run-directory parent is not writable")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(acquire_module.os, "mkdir", deny_parent_creates)

    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert record.input_kind == "local"
    assert (run_dir / "source.pdf").read_bytes() == PDF


def test_acquire_pdf_links_from_the_pinned_run_filesystem_to_avoid_exdev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    original_link = acquire_module.os.link

    def reject_unanchored_source(
        source_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if src_dir_fd is None:
            raise OSError(errno.EXDEV, "simulated cross-device publication")
        original_link(
            source_path,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(acquire_module.os, "link", reject_unanchored_source)

    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert record.input_kind == "local"
    assert (run_dir / "source.pdf").read_bytes() == PDF


def test_acquire_pdf_rejects_destination_race_without_overwriting_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    destination = tmp_path / "run" / "source.pdf"
    original_link = acquire_module.os.link

    def race(source_path: Path, destination_name: str, **kwargs: object) -> None:
        destination.write_bytes(b"racer")
        original_link(source_path, destination_name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(acquire_module.os, "link", race)

    with pytest.raises(PdfAcquireError, match="already exists"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)

    assert destination.read_bytes() == b"racer"


def test_acquire_pdf_rejects_run_directory_swap_after_source_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    replacement_dir = tmp_path / "replacement"
    original_publish = acquire_module._publish_staged_file

    def swap_after_source(
        staging: object, source_name: str, destination: Path, run: object
    ) -> tuple[int, int]:
        identity = original_publish(  # type: ignore[arg-type]
            staging, source_name, destination, run
        )
        if destination.name == "source.pdf":
            run_dir.replace(replacement_dir)
            run_dir.symlink_to(tmp_path / "attacker", target_is_directory=True)
        return identity

    monkeypatch.setattr(acquire_module, "_publish_staged_file", swap_after_source)

    with pytest.raises(PdfAcquireError, match="link or reparse|changed identity"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (replacement_dir / "source.pdf").exists()
    assert list(replacement_dir.glob(".pdf-acquiring-*")) == []
    assert run_dir.is_symlink()


def test_acquire_pdf_rejects_destination_replaced_before_identity_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"racer")
    original_link = acquire_module.os.link

    def replace_after_publication(source: Path, destination: str, **kwargs: object) -> None:
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]
        if destination == "source.pdf":
            replacement.replace(run_dir / destination)

    monkeypatch.setattr(acquire_module.os, "link", replace_after_publication)

    with pytest.raises(PdfAcquireError, match="changed identity during publication"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert (run_dir / "source.pdf").read_bytes() == b"racer"


def test_acquire_pdf_rolls_back_link_when_interrupted_before_post_link_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    original_link = acquire_module.os.link

    def interrupt_after_link(source: Path, destination: str, **kwargs: object) -> None:
        original_link(source, destination, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(acquire_module.os, "link", interrupt_after_link)

    with pytest.raises(KeyboardInterrupt):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (run_dir / "source.pdf").exists()
    assert not (run_dir / "source.json").exists()


@pytest.mark.parametrize(
    "force_fallback", [False, True], ids=["posix", "windows-fallback"]
)
def test_metadata_writer_exception_cleans_unregistered_stage_without_touching_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_fallback: bool,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")

    if force_fallback:
        monkeypatch.setattr(
            acquire_module, "_supports_descriptor_relative_operations", lambda: False
        )

    def write_then_fail(record: object, path: Path) -> None:
        path.write_text("staged metadata", encoding="utf-8")
        raise RuntimeError("metadata writer failed")

    with pytest.raises(RuntimeError, match="metadata writer failed"):
        acquire_pdf(
            str(source),
            run_dir,
            now=FIXED_TIME,
            metadata_writer=write_then_fail,
        )

    assert not (run_dir / "source.pdf").exists()
    assert not (run_dir / "source.json").exists()
    assert list(run_dir.glob(".pdf-acquiring-*")) == []
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize(
    "force_fallback", [False, True], ids=["posix", "windows-fallback"]
)
def test_metadata_writer_base_exception_cleans_unregistered_stage_without_touching_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_fallback: bool,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")

    if force_fallback:
        monkeypatch.setattr(
            acquire_module, "_supports_descriptor_relative_operations", lambda: False
        )

    def write_then_interrupt(record: object, path: Path) -> None:
        path.write_text("staged metadata", encoding="utf-8")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        acquire_pdf(
            str(source),
            run_dir,
            now=FIXED_TIME,
            metadata_writer=write_then_interrupt,
        )

    assert not (run_dir / "source.pdf").exists()
    assert not (run_dir / "source.json").exists()
    assert list(run_dir.glob(".pdf-acquiring-*")) == []
    assert outside.read_text(encoding="utf-8") == "outside"


def test_acquire_pdf_uses_windows_fallback_with_korean_space_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "입력 자료" / "보고서.pdf"
    source.parent.mkdir()
    source.write_bytes(PDF)
    run_dir = tmp_path / "실행 공간"

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )

    record = acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert record.input_kind == "local"
    assert (run_dir / "source.pdf").read_bytes() == PDF


def test_windows_fallback_maps_unsupported_link_to_pdf_acquire_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )

    def unsupported_link(*args: object, **kwargs: object) -> None:
        raise NotImplementedError("link unavailable")

    monkeypatch.setattr(acquire_module.os, "link", unsupported_link)

    with pytest.raises(PdfAcquireError, match="safe PDF publication unavailable"):
        acquire_pdf(str(source), tmp_path / "run", now=FIXED_TIME)


def test_windows_fallback_rolls_back_owned_source_after_run_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    run_dir = tmp_path / "run"
    moved = tmp_path / "moved-run"
    original_link = acquire_module.os.link

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )

    def swap_after_link(source_path: Path, destination: Path, **kwargs: object) -> None:
        original_link(source_path, destination, **kwargs)  # type: ignore[arg-type]
        run_dir.replace(moved)
        run_dir.symlink_to(tmp_path / "attacker", target_is_directory=True)

    monkeypatch.setattr(acquire_module.os, "link", swap_after_link)

    with pytest.raises(PdfAcquireError, match="link or reparse|changed identity"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (moved / "source.pdf").exists()


def test_windows_fallback_rolls_back_after_non_sibling_nested_run_directory_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "入力 資料" / "報告書.pdf"
    source.parent.mkdir()
    source.write_bytes(PDF)
    run_dir = tmp_path / "實行 空間"
    moved_parent = tmp_path / "別の親" / "더 깊은 곳"
    moved_parent.mkdir(parents=True)
    moved = moved_parent / "옮겨진 실행 공간"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    original_link = acquire_module.os.link

    monkeypatch.setattr(
        acquire_module, "_supports_descriptor_relative_operations", lambda: False
    )

    def move_after_link(
        source_path: Path, destination: Path, **kwargs: object
    ) -> None:
        original_link(source_path, destination, **kwargs)  # type: ignore[arg-type]
        run_dir.replace(moved)
        run_dir.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(acquire_module.os, "link", move_after_link)

    with pytest.raises(PdfAcquireError, match="link or reparse|changed identity"):
        acquire_pdf(str(source), run_dir, now=FIXED_TIME)

    assert not (moved / "source.pdf").exists()
    assert list(moved.glob(".pdf-acquiring-*")) == []
    assert list(attacker.iterdir()) == []


def test_acquire_public_pdf_records_redirect_chain_and_accepts_generic_binary_type(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "start.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/report.pdf"})
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-type": "application/octet-stream"},
        )

    record = acquire_pdf(
        "https://start.example/report.pdf",
        tmp_path / "run",
        transport=httpx.MockTransport(handler),
        now=FIXED_TIME,
    )

    assert calls == ["https://start.example/report.pdf", "https://cdn.example/report.pdf"]
    assert record.input_kind == "public"
    assert record.requested_source == "https://start.example/report.pdf"
    assert record.final_source == "https://cdn.example/report.pdf"
    assert record.content_type == "application/octet-stream"
    assert record.redirects == ["https://start.example/report.pdf"]
    assert record.warnings == ["generic-content-type: application/octet-stream"]
    assert (tmp_path / "run" / "source.pdf").read_bytes() == PDF


def test_acquire_public_pdf_rejects_private_redirect_target_before_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def resolve(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        address = "10.0.0.7" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://private.example/report.pdf"})

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    with pytest.raises(PdfAcquireError, match="non-public DNS"):
        acquire_pdf(
            "https://start.example/report.pdf",
            tmp_path / "run",
            transport=httpx.MockTransport(handler),
            now=FIXED_TIME,
        )

    assert requested == ["https://start.example/report.pdf"]


def test_acquire_public_pdf_measures_streamed_content_not_lie_in_content_length(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PDF,
            headers={"content-type": "application/pdf", "content-length": "1"},
        )

    record = acquire_pdf(
        "https://example.com/report.pdf",
        tmp_path / "run",
        transport=httpx.MockTransport(handler),
        now=FIXED_TIME,
    )

    assert record.byte_length == len(PDF)
    assert record.warnings == []
    assert (tmp_path / "run" / "source.pdf").read_bytes() == PDF


def test_acquire_public_pdf_stops_streaming_above_fifty_mebibytes(
    tmp_path: Path,
) -> None:
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield PDF
            yield b"x" * (50 * 1024 * 1024)

        def close(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=Stream(),
            headers={"content-type": "application/pdf"},
        )

    with pytest.raises(PdfAcquireError, match="size limit|downloaded bytes"):
        acquire_pdf(
            "https://example.com/report.pdf",
            tmp_path / "run",
            transport=httpx.MockTransport(handler),
            now=FIXED_TIME,
        )

    assert not (tmp_path / "run" / "source.pdf").exists()
