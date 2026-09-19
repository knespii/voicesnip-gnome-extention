#!/usr/bin/env python3
"""
VoiceSnip - Push-to-Talk Speech-to-Text

Simple GUI for selecting microphone, language, provider, and hotkey.
Press and hold your configured hotkey (default: Ctrl+Space) to record audio.
Release to transcribe and insert text into the active application.

Speech-to-Text Providers:
- Whisper Local CPU: Local processing using Faster Whisper on the CPU (no GPU)
- Whisper Local GPU (CUDA): Local processing using Faster Whisper with NVIDIA GPU
- Whisper Local GPU (ROCm): Local processing using Whisper with AMD GPU

Features:
- Configurable hotkey (e.g., Ctrl+Space, Alt+R, Ctrl+Shift+V)
- Local-only STT with CPU, CUDA and ROCm backends
- Automatic terminal detection for better paste support
- Settings persistence

Copyright (c) Stefan Schmidbauer
License: MIT License
GitHub: https://github.com/Stefan-Schmidbauer/voicesnip
"""

import argparse
import os
import sys
import customtkinter as ctk
from tkinter import messagebox

from dotenv import load_dotenv

from voicesnip.gui.config_manager import load_installation_config
from voicesnip.gui.main_window import VoiceSnipGUI


def load_config_file():
    """Load configuration from .env file.

    Returns the path of the loaded config file, or None if not found.
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.join(base_dir, '.env')
    if os.path.exists(config_path):
        load_dotenv(config_path)
        return config_path

    # Fallback: try default load_dotenv behavior (searches parent dirs)
    load_dotenv()
    return None


# Load environment variables from config file
load_config_file()


def arm_hotkey(root, app):
    """Press Start on the user's behalf when launched with --background.

    On success the window is withdrawn: unmapped entirely, so it leaves no
    dock entry and nothing in the overview - the top-bar extension is how
    VoiceSnip is reached from then on. On failure the window stays up,
    because the reason is on it.

    The outcome also goes to stdout: run unattended, a hotkey that failed to
    arm otherwise looks exactly like one that worked.
    """
    app.start()
    if app.core is not None:
        print("Background: hotkey armed, window hidden.", flush=True)
        root.withdraw()
    else:
        print("Background: hotkey NOT armed - see the window for the reason.",
              flush=True)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="VoiceSnip - push-to-talk speech-to-text")
    parser.add_argument(
        "--background", action="store_true",
        help="arm the hotkey immediately and hide the window; used by the "
             "top-bar extension")
    args = parser.parse_args()

    # Load installation config
    config = load_installation_config()

    if config is None:
        # Show error dialog
        root = ctk.CTk()
        root.withdraw()
        messagebox.showerror(
            "Installation Required",
            "VoiceSnip is not installed.\n\n"
            "Please run the installer first:\n"
            "  ./install.py\n\n"
            "This will configure VoiceSnip for your system."
        )
        sys.exit(1)

    # Start GUI with installation config
    root = ctk.CTk()
    app = VoiceSnipGUI(root, installation_config=config)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)

    if args.background:
        # The widgets are filled from the saved config while the window is
        # built, so let Tk settle before start() reads them back.
        root.after(1500, lambda: arm_hotkey(root, app))

    try:
        root.mainloop()
    except KeyboardInterrupt:
        # Handle Ctrl+C gracefully (same as clicking Quit)
        app.on_closing()
        sys.exit(0)


if __name__ == "__main__":
    main()
