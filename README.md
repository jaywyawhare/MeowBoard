# MeowBoard

> Every keystroke deserves a meow. Global keypress sounds for your terminal.

## Quick Start

```sh
chmod +x keysound
./keysound
# or
python3 keysound.py
```

Stop with `Ctrl+C`.

## Options

| Flag | Description |
|------|-------------|
| `--min-interval SEC` | Minimum seconds between sounds (default: every key) |
| `--device PATH` | Linux: specify evdev device (repeatable) |
| `--test-sound` | Linux: verify audio playback and exit |

## Custom Sound

Replace `assets/voices/meow.wav` with any WAV file, or keep the meow, it's perfect.

## Platform Notes

**Linux:** Requires evdev access: run with `sudo` or add yourself to the `input` group:
```sh
sudo usermod -aG input $USER  # log out and back in
```
Audio backends tried in order: `mpv`, `ffplay`, `pw-play`, `paplay`, `aplay`.

**macOS:** Needs Accessibility permission for your terminal in *System Settings -> Privacy & Security*. Playback via `afplay`.

**Windows:** Uses `GetAsyncKeyState` + built-in `winsound`. Keep the asset as WAV.

## Requirements

Python 3, standard library only. No dependencies to install.
