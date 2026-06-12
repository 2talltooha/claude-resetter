# Claude Code — Session Warmer + Task Dispatcher

One script that Windows Task Scheduler runs every weekday morning, a few hours
before you start work. It opens your Claude **5-hour usage window early** so the
reset lands inside your workday — and does real work if you've queued any.

## How it works

On each run the script auto-picks a mode:

| Mode             | When                          | What it does                                                        |
| ---------------- | ----------------------------- | ------------------------------------------------------------------- |
| **Productivity** | `queue/` has `*.json` tasks   | Runs the **oldest** task via `claude -p` (headless) in its `cwd`, then moves it to `archive/`. |
| **Warmup**       | `queue/` empty                | Fires `claude -p "ping"` — just enough to start the usage window.   |

Every run logs one canonical line to a rotating log file.

## Files

```
claude resetter/
├── warm_dispatch.py          ← the script (stdlib only)
├── run_warm_dispatch.bat     ← wrapper Task Scheduler calls
├── README.md                 ← this file
├── queue/                    ← drop task .json files here (oldest runs first)
│   └── example.json.sample   ← copy to a real .json to use
├── archive/                  ← finished tasks land here, timestamped
└── logs/                     ← rotating log (warm_dispatch.log + .1 .. .5)
```

`queue/`, `archive/`, and `logs/` are created automatically on first run.

## Task file format

A task is a small JSON file in `queue/`. Any name ending in `.json`:

```json
{
  "prompt": "Read currentstate.md and write a 5-bullet summary into TODO_NEXT.md.",
  "cwd": "C:\\Users\\awsom\\OneDrive\\Documents\\cooode\\localjobscout"
}
```

- `prompt` — required, non-empty. Passed straight to `claude -p`.
- `cwd`    — optional. Directory Claude runs in. Defaults to this folder.
            Must exist (note the doubled `\\` backslashes in JSON).

Oldest file (by modification time) runs first. Only **one** task runs per
scheduled invocation — keeps each morning short and the window predictable.
The sample file ends in `.json.sample` so it is ignored; copy it to `*.json`
to activate.

## Archiving behavior

After a task finishes it moves to `archive/` with a timestamp + status prefix,
so the queue always advances (a broken task never blocks tomorrow's run):

```
archive/20260605-053001_OK_mytask.json        # claude returned 0
archive/20260605-053001_FAILED_mytask.json    # claude returned nonzero
archive/20260605-053001_TIMEOUT_mytask.json   # claude was killed on timeout
archive/20260605-053001_BADTASK_mytask.json   # malformed JSON / bad cwd
```

## Logging

Rotating log at `logs/warm_dispatch.log` (1 MB × 5 backups). Grep the run lines:

```
2026-06-05 05:30:02 | INFO    | RUN mode=warmup task=ping exit=0
2026-06-05 05:30:14 | INFO    | RUN mode=productivity task=mytask.json exit=0
```

Output is also echoed to stdout, so Task Scheduler's **Last Run Result** is useful.

## Exit codes

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| 0    | success                                   |
| 2    | `claude` CLI not found on PATH            |
| 3    | claude ran but returned nonzero           |
| 4    | task JSON malformed or `cwd` missing      |
| 5    | claude hung and was killed on timeout     |
| 9    | unexpected error                          |

Failures are logged and return nonzero — the script never hangs (every
`claude` call has a hard timeout and stdin is `/dev/null`).

## Timeouts (tune in `warm_dispatch.py`)

```python
TASK_TIMEOUT_SECONDS   = 60 * 30   # productivity task ceiling
WARMUP_TIMEOUT_SECONDS = 60 * 2    # warmup ping ceiling
```

> **Note on tool permissions.** Headless `claude -p` may stall waiting for a
> tool-permission prompt on complex tasks. If a queued task needs file writes
> or commands, add the flag your workflow trusts (e.g. `--permission-mode
> acceptEdits`) to the `cmd` list in `run_claude()`. The timeout guarantees it
> can't hang forever either way.

---

## Windows Task Scheduler setup

Open **Task Scheduler** → **Create Task…** (not "Basic Task" — you want the
full dialog).

### General
- **Name:** `Claude Session Warmer`
- **Run whether user is logged on or not:** your call. Logged-on is simpler
  (avoids storing a password and avoids credential issues with the
  authenticated `claude` CLI). If you pick "whether or not", also check
  **Do not store password** won't work here — Claude's auth lives in your user
  profile, so running as your own user while logged on is recommended.
- Check **Run with highest privileges** only if a task needs it (usually no).
- **Configure for:** Windows 10/11.

### Triggers → New…
- **Begin the task:** On a schedule
- **Weekly**, recur every **1** week
- Days: **Mon Tue Wed Thu Fri**
- **Start:** today's date, time **05:30:00**
- Enable **Stop task if it runs longer than:** `1 hour` (safety net)
- OK.

### Actions → New…
- **Action:** Start a program
- **Program/script:**
  ```
  C:\Users\awsom\OneDrive\Documents\cooode\claudecode\projects\claude resetter\run_warm_dispatch.bat
  ```
- **Start in (working directory):**
  ```
  C:\Users\awsom\OneDrive\Documents\cooode\claudecode\projects\claude resetter
  ```
  (The .bat also self-`cd`s, so this is belt-and-suspenders.)
- Leave **Add arguments** empty.
- OK.

### Conditions
- Uncheck **Start the task only if the computer is on AC power** (so it runs on
  battery too).
- Check **Wake the computer to run this task** if you want it to fire while the
  PC sleeps. (Won't fire if the machine is fully powered off.)

### Settings
- Check **Run task as soon as possible after a scheduled start is missed**
  (catches mornings the PC was asleep/off at 5:30).
- **If the task is already running:** Do not start a new instance.
- OK to save. Enter your Windows password if prompted.

### Test it
Right-click the task → **Run**. Then check:
- `logs/warm_dispatch.log` has a fresh `RUN ...` line.
- Task Scheduler **Last Run Result** shows `0x0` on success.

Run it once from a terminal first to confirm `claude` is on PATH for your user:

```powershell
cd "C:\Users\awsom\OneDrive\Documents\cooode\claudecode\projects\claude resetter"
.\run_warm_dispatch.bat
echo $LASTEXITCODE
```
