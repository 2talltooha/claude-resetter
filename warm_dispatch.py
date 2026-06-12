#!/usr/bin/env python3
"""
Claude Code session warmer + task dispatcher.

Run by Windows Task Scheduler on weekday mornings. It starts the Claude
5-hour usage window early (so the reset lands inside the workday) and does
real work if any is queued.

Modes (auto-selected):
  * productivity - queue/ has *.json task files: run the oldest via
                   `claude -p` (headless) in the task's cwd, then archive it.
  * warmup       - queue empty: fire a trivial `claude -p "ping"` just to
                   open the usage window.

Task file format (queue/<name>.json):
  { "prompt": "do the thing", "cwd": "C:\\path\\to\\project" }

Standard library only. Designed to fail fast and never hang.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --- Configuration ----------------------------------------------------------

# Everything lives next to this script so the scheduler only needs one path.
BASE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BASE_DIR / "queue"
ARCHIVE_DIR = BASE_DIR / "archive"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "warm_dispatch.log"

# Hard ceiling so a stuck `claude` call can never hang the scheduled task.
# A real coding task can take a while; bump if your queued jobs are large.
TASK_TIMEOUT_SECONDS = 60 * 30        # 30 min for productivity tasks
WARMUP_TIMEOUT_SECONDS = 60 * 2       # 2 min is plenty for "ping"

# Rotating log: 1 MB per file, keep 5 backups.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5

# Exit codes
EXIT_OK = 0
EXIT_NO_CLAUDE = 2        # CLI missing / not on PATH
EXIT_TASK_FAILED = 3      # claude ran but returned nonzero
EXIT_BAD_TASK = 4         # task JSON malformed / bad cwd
EXIT_TIMEOUT = 5          # claude hung and was killed
EXIT_UNEXPECTED = 9       # anything else


# --- Logging ----------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("warm_dispatch")
    logger.setLevel(logging.INFO)
    if logger.handlers:                       # avoid double handlers on reimport
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fileh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fileh.setFormatter(fmt)
    logger.addHandler(fileh)

    # Also echo to stdout so the .bat / Task Scheduler "Last Run Result" is useful.
    streamh = logging.StreamHandler(sys.stdout)
    streamh.setFormatter(fmt)
    logger.addHandler(streamh)
    return logger


def log_run(logger: logging.Logger, mode: str, task: str, exit_code: int) -> None:
    """One canonical line per run, easy to grep."""
    logger.info("RUN mode=%s task=%s exit=%d", mode, task, exit_code)


# --- Claude discovery -------------------------------------------------------

def find_claude() -> str | None:
    """Locate the claude CLI. On Windows it is usually claude.cmd."""
    for name in ("claude", "claude.cmd", "claude.exe"):
        path = shutil.which(name)
        if path:
            return path
    return None


# --- Queue handling ---------------------------------------------------------

def oldest_task() -> Path | None:
    """Oldest *.json in queue/ by modification time (FIFO-ish)."""
    if not QUEUE_DIR.is_dir():
        return None
    tasks = sorted(
        (p for p in QUEUE_DIR.glob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    return tasks[0] if tasks else None


def load_task(path: Path) -> tuple[str, str]:
    """Parse and validate a task file. Returns (prompt, cwd). Raises ValueError."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read/parse task JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("task JSON must be an object")

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task missing non-empty 'prompt'")

    cwd = data.get("cwd") or str(BASE_DIR)
    if not isinstance(cwd, str):
        raise ValueError("'cwd' must be a string")
    if not Path(cwd).is_dir():
        raise ValueError(f"'cwd' is not an existing directory: {cwd}")

    return prompt, cwd


def archive_task(path: Path, status: str) -> Path:
    """Move a finished task into archive/ with a timestamp + status prefix."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ARCHIVE_DIR / f"{stamp}_{status}_{path.name}"
    # If a name somehow collides, suffix a counter.
    counter = 1
    while dest.exists():
        dest = ARCHIVE_DIR / f"{stamp}_{status}_{counter}_{path.name}"
        counter += 1
    shutil.move(str(path), str(dest))
    return dest


# --- Claude invocation ------------------------------------------------------

def run_claude(claude: str, prompt: str, cwd: str, timeout: int,
               logger: logging.Logger) -> int:
    """
    Run `claude -p <prompt>` headless in cwd. Returns exit code.

    Never hangs: a timeout kills the process and returns EXIT_TIMEOUT.
    """
    cmd = [claude, "-p", prompt]
    logger.info("exec: claude -p <prompt len=%d> cwd=%s timeout=%ds",
                len(prompt), cwd, timeout)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            stdin=subprocess.DEVNULL,         # never block waiting on input
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        logger.error("claude binary vanished mid-run: %s", claude)
        return EXIT_NO_CLAUDE
    except subprocess.TimeoutExpired:
        logger.error("claude timed out after %ds - killed", timeout)
        return EXIT_TIMEOUT

    # Trim noisy output but keep enough to debug auth/usage errors.
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        logger.info("claude stdout (tail): %s", out[-800:])
    if err:
        logger.warning("claude stderr (tail): %s", err[-800:])

    if proc.returncode != 0:
        logger.error("claude exited nonzero: %d", proc.returncode)
    return proc.returncode


# --- Main -------------------------------------------------------------------

def main() -> int:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("warm_dispatch start | base=%s", BASE_DIR)

    claude = find_claude()
    if not claude:
        logger.error("claude CLI not found on PATH. Install / re-auth Claude Code.")
        log_run(logger, mode="none", task="-", exit_code=EXIT_NO_CLAUDE)
        return EXIT_NO_CLAUDE
    logger.info("claude: %s", claude)

    task_path = oldest_task()

    # --- Warmup mode --------------------------------------------------------
    if task_path is None:
        logger.info("queue empty -> warmup mode")
        rc = run_claude(claude, "ping", str(BASE_DIR),
                        WARMUP_TIMEOUT_SECONDS, logger)
        if rc == 0:
            log_run(logger, mode="warmup", task="ping", exit_code=EXIT_OK)
            return EXIT_OK
        # Map claude failure to a meaningful code.
        code = EXIT_TIMEOUT if rc == EXIT_TIMEOUT else EXIT_TASK_FAILED
        log_run(logger, mode="warmup", task="ping", exit_code=code)
        return code

    # --- Productivity mode --------------------------------------------------
    logger.info("queue has work -> productivity mode | task=%s", task_path.name)
    try:
        prompt, cwd = load_task(task_path)
    except ValueError as exc:
        logger.error("bad task %s: %s", task_path.name, exc)
        archive_task(task_path, status="BADTASK")   # move it so it can't block queue
        log_run(logger, mode="productivity", task=task_path.name,
                exit_code=EXIT_BAD_TASK)
        return EXIT_BAD_TASK

    rc = run_claude(claude, prompt, cwd, TASK_TIMEOUT_SECONDS, logger)

    if rc == 0:
        archive_task(task_path, status="OK")
        log_run(logger, mode="productivity", task=task_path.name, exit_code=EXIT_OK)
        return EXIT_OK

    # Failed or timed out: archive so the queue advances instead of looping
    # on the same broken task every morning.
    status = "TIMEOUT" if rc == EXIT_TIMEOUT else "FAILED"
    archive_task(task_path, status=status)
    code = EXIT_TIMEOUT if rc == EXIT_TIMEOUT else EXIT_TASK_FAILED
    log_run(logger, mode="productivity", task=task_path.name, exit_code=code)
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_UNEXPECTED)
    except Exception as exc:                  # last-resort guard: never hang/crash silently
        try:
            logging.getLogger("warm_dispatch").exception("unexpected: %s", exc)
        finally:
            sys.exit(EXIT_UNEXPECTED)
