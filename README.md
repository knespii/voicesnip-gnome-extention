# VoiceSnip - Push-to-Talk Speech-to-Text

Local push-to-talk speech-to-text for Linux. Hold a hotkey, speak, release - text appears at your cursor.

All processing happens locally on your machine - on the CPU or a GPU. No cloud, no server, no data leaves your machine.

![VoiceSnip GUI](voicesnip.png)

## Features

- **Push-to-Talk**: Hold hotkey to record, release to transcribe
- **Local Processing**: Whisper runs directly on your CPU or on your NVIDIA (CUDA) / AMD (ROCm) GPU
- **Privacy-First**: All data stays on your device
- **Configurable Hotkeys**: Any key combination (Ctrl+Space, Alt+R, etc.)
- **Multi-Language**: 11 languages (German, English, French, Spanish, Czech, etc.) + Auto-Detection
- **On-Screen Overlay**: A capsule shows what it is hearing and what it transcribed, without stealing focus
- **Dark/Light Mode**: Switch between dark and light themes
- **Adjustable Font Size**: A-/A+ buttons to customize text size

## Requirements

- Linux (X11 or Wayland)
- Python 3.8+
- Any CPU, **or** NVIDIA GPU with CUDA, **or** AMD GPU with ROCm
- Debian/Ubuntu for automated system package install

## Installation

```bash
git clone https://github.com/Stefan-Schmidbauer/voicesnip.git
cd voicesnip
./install.py
```

The installer will ask you to choose a profile:

| Profile  | Description                                              |
| -------- | -------------------------------------------------------- |
| **cuda** | Whisper with NVIDIA GPU acceleration (requires CUDA)     |
| **rocm** | Whisper with AMD GPU acceleration (requires ROCm, Linux) |
| **cpu**  | Whisper on the CPU - no GPU required, works anywhere     |

### GPU Setup

**NVIDIA (CUDA):** Install [NVIDIA drivers](https://developer.nvidia.com/cuda-downloads). CUDA libraries are installed automatically by the installer.

**AMD (ROCm):** Install [ROCm drivers](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/) first. PyTorch ROCm is installed automatically by the installer.

**CPU:** No extra setup - the `cpu` profile runs on any machine without GPU drivers.

## Usage

```bash
./start.sh
```

1. Select your microphone
2. Click "Start"
3. Hold your hotkey (default: Ctrl+Space) and speak
4. Release - text appears at your cursor

## On-Screen Overlay

While the hotkey is held, a capsule appears near the top of the screen: a
waveform that follows your voice, the status, and then the finished transcript
before it is typed at your cursor. It never takes keyboard focus, so whatever
you were typing in stays focused the whole time, and it is click-through
everywhere except the capsule itself.

![Listening](assets/overlay/overlay-listening.png)

The waveform's height follows the voice directly, but its shape follows a slow
envelope - speech changes how tall it is without reshuffling its pattern on
every syllable. While Whisper works, the capsule says so and the waveform runs
on its own:

![Transcribing](assets/overlay/overlay-transcribing.png)

The transcript is shown before it is inserted. A long one grows the capsule
downwards to at most 40% of the screen height and then scrolls:

![Result](assets/overlay/overlay-result.png)

It draws itself on rather than appearing: the lit outline is traced from the
top-left corner while the capsule widens out of a narrow token, and the
material follows just behind the tip. Leaving is the same gesture reversed.

![Opening](assets/overlay/overlay-opening.png)

A second click-through window lights the border of the display for as long as
the overlay is up, so the machine listening to you is visible from the corner
of your eye:

![On screen](assets/overlay/overlay-screen.png)

Set `VOICESNIP_OVERLAY=0` to turn the whole thing off. It needs GTK 3 from the
distribution (`python3-gi`, `gir1.2-gtk-3.0`); if that is missing, VoiceSnip
runs exactly as before without it.

> The images are rendered by the overlay's own painting code over a neutral
> background - they are not screenshots of anyone's desktop. The status labels
> are Czech on this branch.

## Wayland Support

VoiceSnip provides the same workflow on both X11 and Wayland — hold your hotkey
anywhere, speak, release, and the text appears at your cursor. The display
server is detected at runtime; the difference is purely internal.

### X11

- **Global hotkey** via pynput
- Text is typed at your cursor via `xdotool`

### Wayland

Wayland's compositor blocks the usual global keyboard hook and synthetic typing,
so VoiceSnip uses two lower-level mechanisms instead:

- **Global hotkey** by reading `/dev/input/event*` directly (`python-evdev`).
  This requires your user to be in the **`input`** group.
- Text is inserted by copying it to the clipboard and pasting with **Ctrl+V**
  via `ydotool` (which needs access to `/dev/uinput`).

The installer sets both up for you: it adds you to the `input` group and
installs a udev rule for `/dev/uinput`. **Group changes only take effect after
the next login**, so log out and back in (or reboot) once after installing,
then the hotkey works system-wide just like on X11.

> **Clipboard note:** On Wayland, text is inserted by placing it on the
> clipboard, pasting it, and then restoring your previous clipboard contents.
> Only the **primary clipboard format** is restored — content that advertises
> multiple formats at once (e.g. rich text with `text/html` + `text/plain`, or
> images offering several types) keeps just the primary type; the other formats
> are dropped. This is a limitation of restoring a clipboard offer with
> `wl-copy`, which handles one MIME type per entry.

## Whisper Models

| Model    | Size   | Memory\* | Notes                   |
| -------- | ------ | -------- | ----------------------- |
| tiny     | 78 MB  | 230 MB   | Fastest, lowest quality |
| base     | 149 MB | 330 MB   |                         |
| small    | 488 MB | 745 MB   | Good balance            |
| medium   | 1.5 GB | 2 GB     |                         |
| turbo    | 1.6 GB | 2.2 GB   | **Recommended for GPU** |
| large-v3 | 3.1 GB | 3.9 GB   | Best quality            |

\* VRAM on the GPU profiles, system RAM on the CPU profile.

On the **CPU** profile the smaller models (`small`, `base`) give the best
responsiveness; `turbo` and `large-v3` also work but are noticeably slower
than on a GPU.

Models download automatically on first use to `~/.cache/huggingface/`.

## Configuration

Settings are saved automatically in the project directory:

| File                    | Purpose              |
| ----------------------- | -------------------- |
| `.env`                  | Environment config   |
| `voicesnip_config.json` | GUI settings         |
| `voicesnip_profile.ini` | Installation profile |

## License

MIT License - see LICENSE file.

**Third-Party:** OpenAI Whisper (MIT), NVIDIA CUDA, AMD ROCm

## Author

Stefan Schmidbauer

---

_Developed with AI assistance (Claude/Anthropic)_
