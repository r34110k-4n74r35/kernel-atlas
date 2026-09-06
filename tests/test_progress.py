"""Progress output contracts and integration with atomic index builds."""

import io
import os
import re
import threading

import pytest

from kernel_atlas import build_output, cli, db, indexer, progress
from kernel_atlas.progress import Progress


class Terminal(io.StringIO):
    def isatty(self):
        return True


def plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


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
    unstyled = re.sub(r"\x1b\[[0-9]*[AK]", "", plain(output)).replace("\r", "\n")
    assert all(len(line) <= max(1, width - 1) for line in unstyled.splitlines())
    if width >= 40:
        unwrapped = " ".join(unstyled.split())
        assert "50/100 files" in unwrapped and "ETA 00:10" in unwrapped
        assert "8 workers; 123 symbols" in unwrapped
    assert output.endswith("\n")


def test_terminal_groups_phase_counts_timing_and_detail(clock, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    stream = Terminal()
    with Progress("Parsing sources", total=100, unit="files", stream=stream,
                  refresh=False) as bar:
        clock[0] += 10
        bar.update(50, detail="8 workers; 123 symbols")
        lines = [plain(line) for line in bar._terminal_lines(clock[0], None, 79)]
        assert "running Parsing sources" in lines[0]
        assert "[#########---------]  50%  50/100 files" in lines[1]
        assert "elapsed 00:10   5 files/s   ETA 00:10" in lines[2]
        assert lines[3] == "    8 workers; 123 symbols"
        done = [plain(line) for line in bar._terminal_lines(clock[0], "done", 79)]
        assert done[0] == "  done Parsing sources"
        assert "50/100 files (50%)   elapsed 00:10" in done[1]
        assert "ETA" not in " ".join(done) and "[" not in " ".join(done)


@pytest.mark.parametrize("width", [1, 2, 4, 5, 7, 39, 79])
def test_unicode_progress_wraps_by_terminal_cells(clock, monkeypatch, width):
    monkeypatch.setenv("TERM", "xterm")
    detail = "资料/" * 20 + "cafe\u0301"
    with build_output.color_mode("always"):
        bar = Progress("扫描 sources", total=100, unit="文件", stream=Terminal(),
                       detail=detail, refresh=False)
    bar.started = clock[0]
    lines = [plain(line) for line in bar._terminal_lines(clock[0], None, width)]
    assert all(build_output.display_width(line) <= width for line in lines)
    joined = "".join("".join(lines).split())
    if width > 1:
        assert "扫描sources" in joined and detail in joined
        assert "文件" in joined and "?" not in joined
    else:
        assert "?" in joined and "资料" not in joined
    # Combining accents stay attached without consuming an extra terminal cell.
    assert "cafe\u0301" in joined


def test_display_width_counts_wide_characters_and_combining_marks():
    assert build_output.display_width("A资料e\u0301") == 6
    assert build_output.wrap_text("e\u0301x", 1) == ["e\u0301", "x"]
    assert build_output.wrap_text("资料", 1) == ["?", "?"]
    assert build_output.wrap_text("    资料", 2, subsequent_indent="    ") == ["资", "料"]
    assert build_output.wrap_text("\u3000 资料", 3) == ["资", "料"]


@pytest.mark.parametrize("mode,terminal,no_color,colored", [
    ("auto", True, False, True),
    ("auto", False, False, False),
    ("auto", True, True, False),
    ("always", False, False, True),
    ("always", True, True, True),
    ("never", True, False, False),
])
def test_progress_honors_color_mode_and_no_color(
        monkeypatch, mode, terminal, no_color, colored):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "")
    stream = Terminal() if terminal else io.StringIO()
    with build_output.color_mode(mode):
        with Progress("Parsing sources", stream=stream, refresh=False):
            pass
    output = stream.getvalue()
    assert ("\x1b[1;36m" in output) == colored
    assert ("\x1b[1;32m" in output) == colored
    assert "done Parsing sources" in plain(output)
    assert ("\r" in output) == terminal


@pytest.mark.parametrize("stderr_terminal", [False, True])
def test_progress_auto_color_uses_stderr_independently_of_stdout(
        monkeypatch, stderr_terminal):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stderr = Terminal() if stderr_terminal else io.StringIO()
    stdout = io.StringIO() if stderr_terminal else Terminal()
    monkeypatch.setattr(progress.sys, "stderr", stderr)
    monkeypatch.setattr(progress.sys, "stdout", stdout)
    with Progress("Parsing sources", refresh=False):
        pass
    assert ("\x1b[1;32m" in stderr.getvalue()) == stderr_terminal
    assert stdout.getvalue() == ""


def test_color_preference_is_preserved_in_refresh_thread(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("NO_COLOR", "1")
    rendered = threading.Event()
    colors = []
    original = progress.paint

    def observe(text, code, on):
        if threading.current_thread().name == "kernel-atlas-progress" and code:
            colors.append(on)
            rendered.set()
        return original(text, code, on)

    monkeypatch.setattr(progress, "paint", observe)
    with build_output.color_mode("always"):
        bar = Progress("Validating index", stream=Terminal())
    # Construction captures the explicit preference; the ContextVar has reset
    # before either the main thread or the renderer starts writing.
    with bar:
        assert rendered.wait(2), "progress thread did not render"
    assert colors and all(colors)
    assert "\x1b[1;32m" in bar.stream.getvalue()


@pytest.mark.parametrize("error,status,code", [
    (RuntimeError, "failed", "1;31"),
    (KeyboardInterrupt, "interrupted", "1;33"),
])
def test_failed_and_interrupted_phases_have_color_and_status_words(error, status, code):
    stream = io.StringIO()
    with build_output.color_mode("always"):
        with pytest.raises(error):
            with Progress("Parsing sources", stream=stream):
                raise error("stopped")
    assert f"\x1b[{code}m  {status} Parsing sources\x1b[0m" in stream.getvalue()
    assert "done Parsing sources" not in plain(stream.getvalue())


def test_progress_sanitizes_all_user_controlled_fields():
    stream = io.StringIO()
    with build_output.color_mode("never"):
        with Progress("Parsing\x1b[2J sources\n", unit="files\t", stream=stream,
                      detail="source  with spaces\r\nnext") as bar:
            bar.update(1, detail="other  path\x1b[31m\n")
    output = stream.getvalue()
    assert "\x1b" not in output and "\r" not in output and "\t" not in output
    assert len(output.splitlines()) == 2
    assert "source  with spaces" in output and "other  path" in output


def test_counted_progress_without_units_keeps_measurements(clock):
    stream = io.StringIO()
    with Progress("Checking", total=4, stream=stream) as bar:
        clock[0] += 30
        bar.update(2)
    assert "2/4 (50%)" in stream.getvalue()
    assert "100%" not in stream.getvalue()


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
        assert "1 worker" in output.err and "symbols" in output.err
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
