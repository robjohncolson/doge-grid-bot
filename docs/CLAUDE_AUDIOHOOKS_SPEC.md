# Claude Code Audio Hooks — Warcraft Peon Pack

**Version**: 1.0
**Platform**: Windows 11 (PowerShell + .NET SoundPlayer)
**Purpose**: Play Warcraft Peon voice lines on Claude Code lifecycle events.

---

## Overview

Claude Code supports [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) — shell commands that fire on lifecycle events. This spec wires 6 hook events to Warcraft Peon `.wav` files via a PowerShell playback script.

## File Structure

```
~/.claude/
├── settings.json              ← hook configuration (global)
└── sounds/
    ├── play.ps1               ← playback script
    └── peon/                  ← audio assets
        ├── ready_to_work.wav
        ├── zug_zug.wav
        ├── work_work.wav
        ├── me_not_that_kind.wav
        ├── what.wav
        ├── jobs_done.wav
        └── something_need_doing.wav   (spare / unmapped)
```

On Windows, `~/.claude/` resolves to `C:\Users\<username>\.claude\`.

## Hook → Sound Mapping

| Hook Event | Sound File | When It Fires |
|---|---|---|
| `SessionStart` | `ready_to_work.wav` | Claude Code session begins |
| `UserPromptSubmit` | `zug_zug.wav` | User presses Enter on a prompt |
| `PostToolUse` | `work_work.wav` | A tool call completes successfully |
| `PostToolUseFailure` | `me_not_that_kind.wav` | A tool call errors |
| `Notification` | `what.wav` | Claude sends a notification |
| `Stop` | `jobs_done.wav` | Claude finishes responding / session ends |

## Playback Script — `play.ps1`

```powershell
param([string]$Sound)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$file = Join-Path $dir "peon\$Sound"
if (Test-Path $file) {
    $player = New-Object System.Media.SoundPlayer $file
    $player.PlaySync()
}
```

### Critical: `PlaySync()` not `Play()`

`Play()` is async — the PowerShell process exits before audio finishes, killing playback. `PlaySync()` blocks until the WAV completes. All peon clips are under 1.5 seconds, well within the 5-second hook timeout.

## Configuration — `settings.json`

Add the `hooks` key to `~/.claude/settings.json` (create if it doesn't exist). Replace `<USERNAME>` with the actual Windows username.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 ready_to_work.wav",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 zug_zug.wav",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 work_work.wav",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 me_not_that_kind.wav",
            "timeout": 5
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 what.wav",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\<USERNAME>\\.claude\\sounds\\play.ps1 jobs_done.wav",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## Audio Assets

WAV files must be PCM format (`.wav`). The Peon voice lines from Warcraft II / Warcraft III are:

| File | Quote | Duration |
|---|---|---|
| `ready_to_work.wav` | "Ready to work!" | ~1.3s |
| `zug_zug.wav` | "Zug zug." | ~1.0s |
| `work_work.wav` | "Work, work." | ~0.9s |
| `me_not_that_kind.wav` | "Me not that kind of orc!" | ~1.4s |
| `what.wav` | "What?" | ~0.7s |
| `jobs_done.wav` | "Job's done!" | ~0.9s |
| `something_need_doing.wav` | "Something need doing?" | ~1.4s |

Source: Extract from game files or download from community sound archives. All files should be 22050 Hz or 44100 Hz, mono or stereo, 16-bit PCM WAV.

## Setup Steps

1. **Create directories**:
   ```bash
   mkdir -p ~/.claude/sounds/peon
   ```

2. **Place WAV files** into `~/.claude/sounds/peon/`

3. **Create `play.ps1`** at `~/.claude/sounds/play.ps1` with the script above

4. **Add hooks** to `~/.claude/settings.json` (merge with existing config if present)

5. **Restart Claude Code** — you should hear "Ready to work!" on launch

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No sound at all | `Play()` instead of `PlaySync()` | Change to `PlaySync()` in play.ps1 |
| No sound at all | Wrong file path in settings.json | Verify `<USERNAME>` substitution, use `\\` not `\` |
| No sound at all | Execution policy blocks script | The `-ExecutionPolicy Bypass` flag should handle this |
| Sound cuts off early | WAV file too long for timeout | Increase `"timeout"` above 5, or trim the WAV |
| "Work work" is annoying | Fires on every single tool call | Remove or comment out the `PostToolUse` hook |
| Want different mapping | Personal preference | Swap filenames in settings.json |

## Adaptation for Other Agents

This pattern works for any CLI agent that supports lifecycle hooks and shell command execution. To adapt:

1. **Map events**: Find the agent's equivalent lifecycle events (start, prompt, tool use, error, stop)
2. **Playback method**: On macOS use `afplay`, on Linux use `aplay` or `paplay` instead of PowerShell SoundPlayer
3. **Timeout**: Ensure the hook timeout exceeds the longest WAV duration
4. **Blocking playback**: Always use synchronous/blocking playback — async will get killed when the process exits

### macOS equivalent (`play.sh`):
```bash
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
FILE="$DIR/peon/$1"
[ -f "$FILE" ] && afplay "$FILE"
```

### Linux equivalent (`play.sh`):
```bash
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
FILE="$DIR/peon/$1"
[ -f "$FILE" ] && aplay -q "$FILE"
```
