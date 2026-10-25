import hashlib
import os
import threading

import pytest

from mcp_server_kit import files


def test_first_write_keeps_the_name(tmp_path):
    p = files.atomic_create(tmp_path / "shared", "report.pdf", b"one")
    assert p == tmp_path / "shared" / "report.pdf" and p.read_bytes() == b"one"


def test_collisions_get_numbered_suffixes_before_the_extension(tmp_path):
    names = [files.atomic_create(tmp_path, "a.tar.gz", bytes([i])).name for i in range(3)]
    assert names == ["a.tar.gz", "a.tar-2.gz", "a.tar-3.gz"]
    assert files.atomic_create(tmp_path, "noext", b"x").name == "noext"
    assert files.atomic_create(tmp_path, "noext", b"y").name == "noext-2"


def test_never_overwrites_under_concurrency(tmp_path):
    out, start = [], threading.Barrier(20)

    def write(i):
        start.wait()
        out.append(files.atomic_create(tmp_path, "same.bin", str(i).encode()))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({p.name for p in out}) == 20
    assert sorted(p.read_bytes() for p in out) == sorted(str(i).encode() for i in range(20))


def test_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    real_fdopen = os.fdopen

    def failing_fdopen(fd, *a, **k):
        fp = real_fdopen(fd, *a, **k)
        fp.write = lambda data: (_ for _ in ()).throw(OSError("disk full"))
        return fp

    monkeypatch.setattr(files.os, "fdopen", failing_fdopen)
    with pytest.raises(OSError, match="disk full"):
        files.atomic_create(tmp_path, "x.txt", b"data")
    assert list(tmp_path.iterdir()) == []


def test_exhausted_names_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_SUFFIX", 2)
    for _ in range(3):
        files.atomic_create(tmp_path, "f.txt", b"")
    with pytest.raises(files.NoFreeNameError, match="tried -2"):
        files.atomic_create(tmp_path, "f.txt", b"")


def test_exclusive_open_streams(tmp_path):
    path, fd = files.exclusive_open(tmp_path, "s.bin")
    with os.fdopen(fd, "wb") as fp:
        fp.write(b"abc")
    assert path.read_bytes() == b"abc"
    assert files.exclusive_open(tmp_path, "s.bin")[0].name == "s-2.bin"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../x", "/etc/passwd"])
def test_require_bare_rejects_paths(bad):
    with pytest.raises(ValueError):
        files.require_bare(bad)


def test_require_bare_and_sha256():
    assert files.require_bare("ok.pdf") == "ok.pdf"
    assert files.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def _old(path, hours):
    t = os.path.getmtime(path) - hours * 3600
    os.utime(path, (t, t))


def test_list_files_shows_size_sha_and_reference(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"abc")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_bytes(b"x")
    out = files.list_files(tmp_path)
    assert "1 file(s)" in out and "a.pdf  (3 bytes" in out and "[shared:a.pdf]" in out
    assert hashlib.sha256(b"abc").hexdigest() in out and "b.txt" not in out
    deep = files.list_files(tmp_path, scheme="local", label="local store", recursive=True)
    assert "2 file(s) in the local store" in deep and "[local:sub/b.txt]" in deep


def test_list_files_filter_empty_and_missing(tmp_path):
    (tmp_path / "Invoice.PDF").write_bytes(b"1")
    assert "Invoice.PDF" in files.list_files(tmp_path, "invoice")
    assert "is empty (filter 'zzz')" in files.list_files(tmp_path, "zzz")
    assert "does not exist" in files.list_files(tmp_path / "nope")


def test_purge_one_deletes_only_bare_names(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"abc")
    assert files.purge_one(tmp_path, "a.pdf") == f"Deleted a.pdf (3 bytes) from {tmp_path}."
    assert not (tmp_path / "a.pdf").exists()
    assert "No such file" in files.purge_one(tmp_path, "a.pdf")
    with pytest.raises(ValueError):
        files.purge_one(tmp_path, "../a.pdf")


def test_purge_older_respects_age_dry_run_and_subdirs(tmp_path):
    for name in ("old.pdf", "new.pdf"):
        (tmp_path / name).write_bytes(b"x")
    _old(tmp_path / "old.pdf", 30)
    (tmp_path / "keepdir").mkdir()
    _old(tmp_path / "keepdir", 30)
    dry = files.purge_older(tmp_path, 24, dry_run=True)
    assert dry.startswith("Would delete 1 file(s)") and (tmp_path / "old.pdf").exists()
    assert files.purge_older(tmp_path, 24).startswith("Deleted 1 file(s)")
    assert not (tmp_path / "old.pdf").exists() and (tmp_path / "new.pdf").exists() and (tmp_path / "keepdir").is_dir()
    assert "No files older" in files.purge_older(tmp_path, 24)
    with pytest.raises(ValueError):
        files.purge_older(tmp_path, -1)
    assert "nothing to purge" in files.purge_older(tmp_path / "nope")
