"""Progress output contracts and integration with atomic index builds."""

import io
import os
import re
import threading

import pytest

from kernel_atlas import cli, db, indexer, progress
from kernel_atlas.progress import Progress


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: now[0])
    return now


def test_redirected_progress_is_throttled_and_has_no_terminal_controls(clock):
    stream = io.StringIO()
    with Progress("Parsing sources", total=100, unit="files", stream=stream) as bar:
        clock[0] += 10
        bar.update(10)
        assert len(stream.getvalue().splitlines()) == 1
        clock[0] += 20
        bar.update(50, detail="8 workers; 123 symbols")
        assert len(stream.getvalue().splitlines()) == 2
        assert "ETA 00:30" in stream.getvalue()
        bar.update(100)
    output = stream.getvalue()
    assert len(output.splitlines()) == 3
    assert "done Parsing sources" in output and "100/100 files" in output
    assert "8 workers; 123 symbols" in output
    assert "\r" not in output and "\x1b" not in output


@pytest.mark.parametrize("width", [1, 8, 40, 80, 120])
def test_terminal_wraps_counters_and_cleans_up(clock, monkeypatch, width):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(progress.shutil, "get_terminal_size",
                        lambda **kwargs: os.terminal_size((width, 80)))
    stream = Terminal()
    with Progress("Parsing sources", total=100, unit="files", stream=stream,
                  refresh=False) as bar:
        clock[0] += 10
        bar.update(50, detail="8 workers; 123 symbols")
    output = stream.getvalue()
    assert "\r" in output and "\x1b[K" in output
    plain = re.sub(r"\x1b\[[0-9]*[AK]", "", output).replace("\r", "\n")
    assert all(len(line) <= max(1, width - 1) for line in plain.splitlines())
    if width >= 40:
        unwrapped = " ".join(plain.split())
        assert "50/100 files" in unwrapped and "ETA 00:10" in unwrapped
        assert "8 workers; 123 symbols" in unwrapped
    assert output.endswith("\n")


@pytest.mark.parametrize("total", [None, 0])
def test_unknown_and_empty_totals_do_not_invent_percentages(clock, total):
    stream = io.StringIO()
    with Progress("Scanning", total=total, unit="files", stream=stream) as bar:
        clock[0] += 30
        bar.update(0)
    assert "%" not in stream.getvalue() and "ETA" not in stream.getvalue()
    assert "done Scanning" in stream.getvalue()


def test_resumed_transfer_rate_counts_only_new_bytes(clock):
    stream = io.StringIO()
    with Progress("Downloading", total=100 * 1024, initial=40 * 1024,
                  unit="bytes", stream=stream) as bar:
        clock[0] += 30
        bar.update(70 * 1024)
        assert "1.0 KiB/s" in stream.getvalue()
        assert "ETA 00:30" in stream.getvalue()
        bar.update(100 * 1024)
    assert "100.0 KiB/100.0 KiB" in stream.getvalue()


@pytest.mark.parametrize("error,marker", [(RuntimeError, "failed"),
                                         (KeyboardInterrupt, "interrupted")])
def test_errors_propagate_and_are_never_reported_as_done(error, marker):
    stream = io.StringIO()
    with pytest.raises(error):
        with Progress("Parsing", total=100, unit="files", stream=stream) as bar:
            bar.update(25)
            raise error("stopped")
    output = stream.getvalue()
    assert f"{marker} Parsing" in output and "25/100 files" in output
    assert "done" not in output and "100%" not in output


def test_spinner_refreshes_without_updates_and_stops_on_exit(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    rendered = threading.Event()

    class ObservedTerminal(Terminal):
        def flush(self):
            if self.getvalue().count("elapsed") >= 2:
                rendered.set()

    stream = ObservedTerminal()
    with Progress("Validating index", stream=stream) as bar:
        assert rendered.wait(2), "spinner did not refresh while work was blocked"
    assert not bar._thread.is_alive()
    assert "done Validating index" in stream.getvalue()


def test_quiet_mode_never_starts_a_thread_or_writes(monkeypatch):
    monkeypatch.setattr(progress.threading, "Thread",
                        lambda **kwargs: pytest.fail("quiet mode started a thread"))
    stream = Terminal()
    with Progress("Parsing", total=5, unit="files", quiet=True, stream=stream) as bar:
        bar.update(5, detail="complete")
    assert stream.getvalue() == ""


def test_dumb_terminal_uses_plain_logs_and_sanitizes_details(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    stream = Terminal()
    with Progress("Scanning", detail="file\x1b[2J\nname", stream=stream):
        pass
    assert "\x1b" not in stream.getvalue() and "\r" not in stream.getvalue()
    assert len(stream.getvalue().splitlines()) == 2


def test_parser_workers_start_before_the_progress_refresh_thread(
        mini_tree, tmp_path, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    terminal = Terminal()
    monkeypatch.setattr(progress.sys, "stderr", terminal)
    executor = indexer.ProcessPoolExecutor

    class ObservedExecutor(executor):
        def map(self, *args, **kwargs):
            assert not any(thread.name == "kernel-atlas-progress"
                           for thread in threading.enumerate())
            return super().map(*args, **kwargs)

    monkeypatch.setattr(indexer, "ProcessPoolExecutor", ObservedExecutor)
    indexer.build(mini_tree, tmp_path / "index.db", "9.9", jobs=2)
    assert "done Parsing sources" in terminal.getvalue()
    assert not any(thread.name == "kernel-atlas-progress"
                   for thread in threading.enumerate())


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("with_calls", [False, True])
def test_build_progress_is_on_stderr_and_quiet_keeps_summary(
        mini_tree, tmp_path, capsys, quiet, with_calls):
    args = ["build", "--src", str(mini_tree), "--output", str(tmp_path / "index.db"),
            "--jobs", "1"]
    if quiet:
        args.append("--quiet")
    if with_calls:
        args.append("--with-calls")
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert "Built index" in output.out
    assert "Parsing sources" not in output.out
    if quiet:
        assert output.err == ""
    else:
        for phase in ("Scanning source tree", "Parsing sources", "Mapping ownership",
                      "Creating database indexes", "Validating index", "Publishing index"):
            assert f"done {phase}" in output.err
        assert ("done Resolving calls" in output.err) == with_calls
        assert "workers" in output.err and "symbols" in output.err
        assert "\r" not in output.err and "\x1b" not in output.err


def test_failed_validation_finishes_progress_without_publishing(
        mini_tree, tmp_path, capsys, monkeypatch):
    output = tmp_path / "index.db"
    output.write_bytes(b"previous index")

    def fail(*args, **kwargs):
        raise db.SchemaError("test audit failure")

    monkeypatch.setattr(db, "validate_schema", fail)
    with pytest.raises(db.SchemaError, match="test audit failure"):
        indexer.build(mini_tree, output, "9.9", jobs=1)
    report = capsys.readouterr().err
    assert "failed Validating index" in report
    assert "Publishing index" not in report
    assert output.read_bytes() == b"previous index"
    assert not list(tmp_path.glob("*.building"))
