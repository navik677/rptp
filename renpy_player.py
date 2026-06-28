#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════╗
║                    —  Ren'Py TUI Player —                     ║
║   Run real Ren'Py Visual Novels inside your Terminal TUI      ║
║   Supports dynamic image rendering of backgrounds & sprites   ║
╚═══════════════════════════════════════════════════════════════╝

Usage:
    python3 renpy_player.py <path_to_renpy_game_or_directory>
    python3 renpy_player.py                   # launches built-in mock game

Controls:
    Space / Enter / → : Advance dialogue / skip typewriter
    ↓ / ↑             : Move menu cursor
    Enter             : Choose option / advance
    Q / Ctrl+C        : Quit
    R                 : Restart current VN
    V                 : Toggle Graphics (ANSI blocks / High-Res)
    TAB / Ctrl+F      : Toggle Fast Skip Mode
    S                 : Open Save Menu (slots 1-5)
    L                 : Open Load Menu (slots 1-5)
    ESC               : Close Save/Load menu
"""

from __future__ import annotations

import os
import re
import sys
import asyncio
import io
import shutil
import base64
import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import ClassVar, Union
from datetime import datetime

from PIL import Image, ImageDraw
from rich.text import Text
from rich.console import RenderableType

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Footer, Label, ListItem, ListView, Static


# ─────────────────────────────────────────────────────────────
# SECTION 1 ▸ IMAGE DATABASE & RESOLVER
# ─────────────────────────────────────────────────────────────

ImageTarget = Union[Path, tuple[Path, int, int, bytes]]

def index_images(game_dir: Path) -> dict[tuple[str, ...], ImageTarget]:
    """
    Recursively scan the directory for local image files and files inside RPA
    archives, then map their name tags to ease automatic image matching.
    """
    image_map: dict[tuple[str, ...], ImageTarget] = {}
    valid_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    
    # 1. Index local files
    for path in game_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in valid_exts:
            try:
                rel = path.relative_to(game_dir)
            except ValueError:
                rel = path
            
            stem = rel.with_suffix("").as_posix().lower()
            parts = re.split(r'[/_\-\s]+', stem)
            
            # Remove common container folder names from the start of tokens
            if parts and parts[0] in {"images", "gui", "game", "backgrounds", "characters", "sprites"}:
                parts = parts[1:]
                
            if parts:
                image_map[tuple(parts)] = path

    # 2. Index RPA archives (read files on-the-fly)
    import unrpa
    for rpa_path in game_dir.glob("*.rpa"):
        try:
            with open(rpa_path, "rb") as f:
                un = unrpa.UnRPA(str(rpa_path))
                index = un.get_index(f)
                for name, data in index.items():
                    ext = Path(name).suffix.lower()
                    if ext in valid_exts:
                        stem = Path(name).with_suffix("").as_posix().lower()
                        parts = re.split(r'[/_\-\s]+', stem)
                        if parts and parts[0] in {"images", "gui", "game", "backgrounds", "characters", "sprites"}:
                            parts = parts[1:]
                        if parts:
                            offset, length, prefix = data[0]
                            image_map[tuple(parts)] = (rpa_path, offset, length, prefix)
        except Exception as e:
            print(f"Warning: Failed to index archive {rpa_path.name}: {e}", file=sys.stderr)

    return image_map


def resolve_image(query_tags: list[str], image_map: dict[tuple[str, ...], ImageTarget]) -> ImageTarget | None:
    """
    Resolve tags (e.g. ['bg', 'space']) to the best indexed image path.
    Uses token overlap intersection scoring.
    """
    query_tuple = tuple(t.lower() for t in query_tags if t)
    if not query_tuple:
        return None
        
    # 1. Exact match
    if query_tuple in image_map:
        return image_map[query_tuple]
        
    # 2. Token overlap matching
    best_match = None
    best_score = -1.0
    for key, path in image_map.items():
        overlap = set(query_tuple) & set(key)
        if overlap:
            # Score is the Jaccard-like overlap index
            score = len(overlap) / max(len(key), len(query_tuple))
            if score > best_score:
                best_score = score
                best_match = path
    return best_match


def load_image(target: ImageTarget) -> Image.Image | None:
    """
    Loads a PIL Image from either a local filesystem Path or from an RPA archive offset.
    """
    if isinstance(target, Path):
        try:
            return Image.open(target)
        except Exception:
            return None
    elif isinstance(target, tuple):
        archive_path, offset, length, prefix = target
        try:
            with open(archive_path, "rb") as f:
                f.seek(offset)
                data = f.read(length)
                return Image.open(io.BytesIO(prefix + data))
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────
# AUDIO UTILITIES & ENGINE PLAYBACK HELPERS
# ─────────────────────────────────────────────────────────────

AudioTarget = Union[Path, tuple[Path, int, int, bytes]]

def index_audio(game_dir: Path) -> dict[str, AudioTarget]:
    """
    Builds a map from relative audio paths/names to file targets (Paths or RPA offsets).
    """
    audio_map: dict[str, AudioTarget] = {}
    valid_exts = {".ogg", ".mp3", ".wav", ".m4a", ".opus"}
    
    # 1. Index local files
    for path in game_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in valid_exts:
            try:
                rel = path.relative_to(game_dir)
            except ValueError:
                rel = path
            
            # Normalize path
            posix_path = rel.as_posix().lower()
            audio_map[posix_path] = path
            audio_map[path.name.lower()] = path
            audio_map[path.stem.lower()] = path
            
    # 2. Index RPA archives
    import unrpa
    for rpa_path in game_dir.glob("*.rpa"):
        try:
            with open(rpa_path, "rb") as f:
                un = unrpa.UnRPA(str(rpa_path))
                index = un.get_index(f)
                for name, data in index.items():
                    ext = Path(name).suffix.lower()
                    if ext in valid_exts:
                        posix_path = name.lower()
                        offset, length, prefix = data[0]
                        target = (rpa_path, offset, length, prefix)
                        audio_map[posix_path] = target
                        audio_map[Path(name).name.lower()] = target
                        audio_map[Path(name).stem.lower()] = target
        except Exception:
            pass
            
    return audio_map

def play_audio(channel: str, query: str, audio_map: dict[str, AudioTarget], active_processes: dict[str, subprocess.Popen | None]) -> None:
    """
    Plays audio on a specific channel (music, sound, voice) using mpv in background.
    """
    # 1. Stop active process on this channel
    stop_audio(channel, active_processes)
    
    # 2. Normalize query and resolve target
    normalized = query.lower().replace("\\", "/")
    target = (
        audio_map.get(normalized)
        or audio_map.get(Path(normalized).name)
        or audio_map.get(Path(normalized).stem)
    )
    
    if not target:
        # Fallback: check if the query is a direct file path that exists
        if Path(query).exists():
            target = Path(query)
        else:
            return
            
    # 3. Get play path
    play_path = None
    if isinstance(target, Path):
        play_path = target
    else:
        # Extract from RPA on-the-fly
        try:
            rpa_path, offset, length, prefix = target
            temp_dir = Path("/tmp/renpy_audio")
            temp_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"{offset}_{Path(normalized).name}"
            temp_file = temp_dir / safe_name
            if not temp_file.exists():
                with open(rpa_path, "rb") as f_in:
                    f_in.seek(offset)
                    data = f_in.read(length)
                    temp_file.write_bytes(prefix + data)
            play_path = temp_file
        except Exception:
            pass
            
    if play_path and play_path.exists():
        try:
            cmd = ["mpv", "--no-video", "--no-terminal"]
            if channel == "music":
                cmd.append("--loop-file=inf")
            cmd.append(str(play_path))
            
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            active_processes[channel] = proc
        except Exception:
            pass

def stop_audio(channel: str, active_processes: dict[str, subprocess.Popen | None]) -> None:
    """
    Stops the background mpv process playing on a specific channel.
    """
    proc = active_processes.get(channel)
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=0.2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        active_processes[channel] = None


# ─────────────────────────────────────────────────────────────
# SECTION 2 ▸ REN'PY COMPREHENSIVE PARSER
# ─────────────────────────────────────────────────────────────

class NodeType(Enum):
    LABEL = auto()
    SCENE = auto()
    SHOW = auto()
    HIDE = auto()
    DIALOGUE = auto()
    MENU = auto()
    JUMP = auto()
    PLAY = auto()
    STOP = auto()


@dataclass
class ScriptNode:
    kind: NodeType
    data: dict = field(default_factory=dict)
    filename: str = ""


class RenpyParser:
    """
    Parses Ren'Py (.rpy) files into sequential ScriptNode instructions.
    """
    _RE_LABEL = re.compile(r'^label\s+(\w+)\s*:')
    _RE_SCENE = re.compile(r'^scene\s+([\w\s]+)')
    _RE_SHOW = re.compile(r'^show\s+([\w\s]+?)(?:\s+at\s+(\w+))?\s*(?:$|:)')
    _RE_HIDE = re.compile(r'^hide\s+([\w\s]+)')
    _RE_DIALOGUE_VAR = re.compile(r'^([a-zA-Z0-9_]+)\s+"([^"]+)"')
    _RE_DIALOGUE_QUOTED = re.compile(r'^"([^"]+)"\s+"([^"]+)"')
    _RE_NARRATE = re.compile(r'^"([^"]+)"')
    _RE_MENU = re.compile(r'^menu(?:\s+\w+)?\s*:')
    _RE_CHOICE = re.compile(r'^"([^"]+)"\s*(?:if\s+.*?)?\s*:')
    _RE_JUMP = re.compile(r'^jump\s+(\w+)')
    _RE_PLAY = re.compile(r'^play\s+(\w+)\s+(?:["\']([^"\']+)["\']|(\w+))(?:\s+loop)?(?:\s+fadein\s+\d+\.?\d*)?')
    _RE_STOP = re.compile(r'^stop\s+(\w+)')
    _RE_DEFINE_CHAR = re.compile(
        r'^define\s+([a-zA-Z0-9_]+)\s*=\s*(?:Character|character)\(\s*["\']([^"\']+)["\']\s*'
        r'(?:,\s*(?:color|who_color)\s*=\s*["\']([^"\']+)["\'])?.*?\)'
    )

    def __init__(self) -> None:
        self.characters: dict[str, tuple[str, str | None]] = {}  # var_name -> (display_name, color_hex)

    def parse_content(self, content: str, filename: str = "") -> list[ScriptNode]:
        lines: list[tuple[int, str]] = []
        for raw_line in content.splitlines():
            stripped = raw_line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(stripped)
            lines.append((indent, stripped))
        return self._parse_lines(lines, filename)

    def _parse_lines(self, lines: list[tuple[int, str]], filename: str) -> list[ScriptNode]:
        nodes: list[ScriptNode] = []
        i = 0
        while i < len(lines):
            indent, val = lines[i]

            # Character definitions
            if m := self._RE_DEFINE_CHAR.match(val):
                var = m.group(1)
                name = m.group(2)
                color = m.group(3) if len(m.groups()) >= 3 else None
                self.characters[var] = (name, color)
                i += 1
                continue

            # Labels
            if m := self._RE_LABEL.match(val):
                nodes.append(ScriptNode(NodeType.LABEL, {"name": m.group(1)}, filename))
                i += 1
                continue

            # Scene backgrounds
            if m := self._RE_SCENE.match(val):
                nodes.append(ScriptNode(NodeType.SCENE, {"bg": m.group(1).strip()}, filename))
                i += 1
                continue

            # Show sprites
            if m := self._RE_SHOW.match(val):
                nodes.append(ScriptNode(NodeType.SHOW, {
                    "sprite": m.group(1).strip(),
                    "position": m.group(2) or "center",
                }, filename))
                i += 1
                continue

            # Hide sprites
            if m := self._RE_HIDE.match(val):
                nodes.append(ScriptNode(NodeType.HIDE, {"sprite": m.group(1).strip()}, filename))
                i += 1
                continue

            # Jumps
            if m := self._RE_JUMP.match(val):
                nodes.append(ScriptNode(NodeType.JUMP, {"name": m.group(1)}, filename))
                i += 1
                continue

            # Menus
            if self._RE_MENU.match(val):
                menu_indent = indent
                menu_line_num = i
                i += 1
                prompt = ""
                choices = []

                # Gather all menu lines
                menu_lines = []
                while i < len(lines) and lines[i][0] > menu_indent:
                    menu_lines.append(lines[i])
                    i += 1

                # Parse inside menu block to resolve choices indent
                choice_indent = None
                for mj_indent, mj_val in menu_lines:
                    if self._RE_CHOICE.match(mj_val):
                        choice_indent = mj_indent
                        break

                if choice_indent is None:
                    continue

                menu_end_label = f"_menu_end_{filename.replace('.', '_')}_{menu_line_num}"
                choice_bodies_nodes = []

                j = 0
                while j < len(menu_lines):
                    mj_indent, mj_val = menu_lines[j]

                    if mj_indent < choice_indent:
                        if self._RE_NARRATE.match(mj_val) and not self._RE_CHOICE.match(mj_val):
                            prompt = self._RE_NARRATE.match(mj_val).group(1)
                        j += 1
                        continue

                    if mj_indent == choice_indent and (m_choice := self._RE_CHOICE.match(mj_val)):
                        choice_text = m_choice.group(1)
                        choice_line_num = j
                        j += 1

                        # Gather choice body lines
                        choice_body = []
                        while j < len(menu_lines) and menu_lines[j][0] > choice_indent:
                            choice_body.append(menu_lines[j])
                            j += 1

                        # Generate synthetic label
                        choice_label = f"_choice_{filename.replace('.', '_')}_{menu_line_num}_{choice_line_num}"
                        choices.append((choice_text, choice_label))

                        # Parse recursively
                        body_nodes = self._parse_lines(choice_body, filename)

                        # Flatten nodes into list
                        choice_bodies_nodes.append(ScriptNode(NodeType.LABEL, {"name": choice_label}, filename))
                        choice_bodies_nodes.extend(body_nodes)
                        choice_bodies_nodes.append(ScriptNode(NodeType.JUMP, {"name": menu_end_label}, filename))
                    else:
                        j += 1

                # Append menu structures
                nodes.append(ScriptNode(NodeType.MENU, {
                    "prompt": prompt,
                    "choices": choices,
                }, filename))
                nodes.append(ScriptNode(NodeType.JUMP, {"name": menu_end_label}, filename))
                nodes.extend(choice_bodies_nodes)
                nodes.append(ScriptNode(NodeType.LABEL, {"name": menu_end_label}, filename))
                continue

            # Variable character dialogue
            if m := self._RE_DIALOGUE_VAR.match(val):
                var = m.group(1)
                text = m.group(2)
                name, color = self.characters.get(var, (var, None))
                nodes.append(ScriptNode(NodeType.DIALOGUE, {
                    "character": name,
                    "text": text,
                    "color": color,
                }, filename))
                i += 1
                continue

            # Literal string dialogue
            if m := self._RE_DIALOGUE_QUOTED.match(val):
                nodes.append(ScriptNode(NodeType.DIALOGUE, {
                    "character": m.group(1),
                    "text": m.group(2),
                    "color": None,
                }, filename))
                i += 1
                continue

            # Plain narration
            if m := self._RE_NARRATE.match(val):
                nodes.append(ScriptNode(NodeType.DIALOGUE, {
                    "character": "NARRATOR",
                    "text": m.group(1),
                    "color": None,
                }, filename))
                i += 1
                continue

            # Play audio
            if m := self._RE_PLAY.match(val):
                channel = m.group(1)
                audio_file = m.group(2) or m.group(3)
                nodes.append(ScriptNode(NodeType.PLAY, {
                    "channel": channel,
                    "file": audio_file,
                }, filename))
                i += 1
                continue

            # Stop audio
            if m := self._RE_STOP.match(val):
                channel = m.group(1)
                nodes.append(ScriptNode(NodeType.STOP, {
                    "channel": channel,
                }, filename))
                i += 1
                continue

            # Skip unknown lines
            i += 1
        return nodes


# ─────────────────────────────────────────────────────────────
# SECTION 3 ▸ ENGINE STATE & SCRIPT CONTROLLER
# ─────────────────────────────────────────────────────────────

@dataclass
class EngineState:
    bg: str = ""
    sprites: dict[str, str] = field(default_factory=dict)  # name -> position
    current_index: int = 0
    music_track: str = ""


class ScriptEngine:
    def __init__(self, nodes: list[ScriptNode]) -> None:
        self.nodes = nodes
        self.state = EngineState()
        self.last_executed_index = -1
        self._label_map: dict[str, int] = {}
        self._build_label_map()

    def _build_label_map(self) -> None:
        for idx, node in enumerate(self.nodes):
            if node.kind == NodeType.LABEL:
                self._label_map[node.data["name"]] = idx

    def jump_to(self, label: str) -> bool:
        if label in self._label_map:
            self.state.current_index = self._label_map[label]
            self.last_executed_index = -1
            return True
        return False

    def reset(self) -> None:
        self.state = EngineState()
        self.last_executed_index = -1

    def step(self) -> ScriptNode | None:
        while self.state.current_index < len(self.nodes):
            # Prevent sequential fallthrough between different script files
            if self.last_executed_index != -1 and self.state.current_index == self.last_executed_index + 1:
                prev_node = self.nodes[self.last_executed_index]
                curr_node = self.nodes[self.state.current_index]
                if prev_node.filename != curr_node.filename:
                    # Sequential boundary crossed -> end file flow
                    return None

            node = self.nodes[self.state.current_index]
            self.last_executed_index = self.state.current_index
            self.state.current_index += 1

            if node.kind == NodeType.LABEL:
                continue

            if node.kind == NodeType.SCENE:
                self.state.bg = node.data["bg"]
                self.state.sprites.clear()
                continue

            if node.kind == NodeType.SHOW:
                self.state.sprites[node.data["sprite"]] = node.data["position"]
                continue

            if node.kind == NodeType.HIDE:
                self.state.sprites.pop(node.data["sprite"], None)
                continue

            if node.kind == NodeType.JUMP:
                self.jump_to(node.data["name"])
                continue

            return node
        return None


# ─────────────────────────────────────────────────────────────
# SECTION 4 ▸ TEXTUAL TUI IMPLEMENTATION
# ─────────────────────────────────────────────────────────────

_CHAR_COLORS: dict[str, str] = {
    "ARIA": "cyan",
    "COMMANDER": "yellow",
    "NARRATOR": "bright_black",
    "UNKNOWN": "magenta",
    "SYSTEM": "green",
}
_DEFAULT_CHAR_COLOR = "white"


def clear_terminal_graphics() -> None:
    """Explicitly deletes all graphics images in Kitty to prevent overlapping text."""
    import os
    term = os.environ.get("TERM", "").lower()
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    is_kitty = "kitty" in term or "KITTY_WINDOW_ID" in os.environ or "kitty" in term_program
    if is_kitty:
        sys.stdout.write("\x1b_Ga=d\x1b\\")
        sys.stdout.flush()


class RawAnsi:
    """A custom Rich renderable that streams raw ANSI graphics escape codes directly."""
    def __init__(self, text: str) -> None:
        self.text = text

    def __rich_console__(self, console, options):
        from rich.segment import Segment
        yield Segment(self.text)


class VisualArea(Static):
    """
    Renders background and sprites. Supports two modes:
    1. Pixel mode: rendered using blocky ANSI Unicode half-blocks.
    2. High-res mode: rendered directly as an inline image (iTerm2/WezTerm).
    """
    def __init__(self, engine: ScriptEngine, image_map: dict[tuple[str, ...], ImageTarget], **kwargs) -> None:
        super().__init__(**kwargs)
        self._engine = engine
        self._image_map = image_map
        self._cached_renderable = None
        self._last_state_key = None

    def _get_state_key(self, w: int, h: int) -> tuple:
        st = self._engine.state
        return (
            st.bg,
            tuple(sorted(st.sprites.items())),
            self.app._visual_mode,
            w,
            h
        )

    def render(self) -> RenderableType:
        w = self.size.width or 80
        h = self.size.height or 24
        
        # If menu or choice is active, clear terminal graphics to ensure no overlap
        menu_active = False
        try:
            menu_active = self.app.query_one("#saveload-menu").display
        except Exception:
            pass
            
        if self.app._waiting_for_choice or menu_active:
            import os
            term = os.environ.get("TERM", "").lower()
            term_program = os.environ.get("TERM_PROGRAM", "").lower()
            is_kitty = "kitty" in term or "KITTY_WINDOW_ID" in os.environ or "kitty" in term_program
            if is_kitty:
                # Delete all images from the screen in Kitty
                seq = "\x1b_Ga=d\x1b\\"
                output = seq + "\n" + "\n".join(" " * w for _ in range(h - 1))
                return RawAnsi(output)
            else:
                # Return empty spaces for other terminals
                return Text("\n" * (h - 1))
        
        current_key = self._get_state_key(w, h)
        if self._cached_renderable is not None and self._last_state_key == current_key:
            return self._cached_renderable

        # Adjust canvas resolution based on visual mode (8x16 cell aspect ratio)
        if self.app._visual_mode == "high":
            canvas_w = max(w * 8, 80)
            canvas_h = max(h * 16, 80)
        else:
            canvas_w = max(w, 10)
            canvas_h = max(2 * h, 10)

        st = self._engine.state
        
        # Load background
        bg_image = None
        if st.bg:
            bg_target = resolve_image(st.bg.split(), self._image_map)
            if bg_target:
                bg_image = load_image(bg_target)
                if bg_image and bg_image.mode != "RGBA":
                    bg_image = bg_image.convert("RGBA")

        if bg_image:
            canvas = bg_image.resize((canvas_w, canvas_h), Image.Resampling.BILINEAR)
        else:
            # Fallback black canvas with stars
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (5, 5, 20, 255))
            draw = ImageDraw.Draw(canvas)
            for i in range(40):
                x = (i * 37) % canvas_w
                y = (i * 23) % canvas_h
                draw.point((x, y), fill=(200, 200, 250, 200))

        # Render sprites
        for sprite_name, position in st.sprites.items():
            sprite_target = resolve_image(sprite_name.split(), self._image_map)
            if sprite_target:
                sprite_image = load_image(sprite_target)
                if sprite_image:
                    if sprite_image.mode != "RGBA":
                        sprite_image = sprite_image.convert("RGBA")
                    sh = int(canvas_h * 0.85)
                    aspect = sprite_image.width / sprite_image.height
                    sw = int(sh * aspect)
                    if sw > 0 and sh > 0:
                        sprite_resized = sprite_image.resize((sw, sh), Image.Resampling.BILINEAR)
                        
                        if position == "left":
                            x = int(canvas_w * 0.1)
                        elif position == "right":
                            x = int(canvas_w * 0.9) - sw
                        else:
                            x = (canvas_w - sw) // 2
                        
                        y = canvas_h - sh
                        canvas.paste(sprite_resized, (x, y), sprite_resized)

        # Output format based on graphic mode
        if self.app._visual_mode == "high":
            import os
            term = os.environ.get("TERM", "").lower()
            term_program = os.environ.get("TERM_PROGRAM", "").lower()
            is_kitty = "kitty" in term or "KITTY_WINDOW_ID" in os.environ or "kitty" in term_program
            
            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            
            if is_kitty:
                # Kitty Graphics Protocol - local file transmission (t=f)
                # Extremely fast, uses zero terminal bandwidth, and bypasses command size limits.
                import glob
                import time
                for f in glob.glob("/tmp/kitty_vn_frame_*.png"):
                    try:
                        os.unlink(f)
                    except Exception:
                        pass
                
                frame_path = f"/tmp/kitty_vn_frame_{time.time_ns()}.png"
                canvas.save(frame_path, format="PNG")
                
                b64_path = base64.b64encode(frame_path.encode("utf-8")).decode("ascii")
                # Delete any old images first, then draw the new one
                seq = f"\x1b_Ga=d\x1b\\\x1b_Ga=T,f=100,t=f,c={w},r={h};{b64_path}\x1b\\"
                output = seq + "\n" + "\n".join(" " * w for _ in range(h - 1))
            else:
                # Sixel Graphics Protocol (for WezTerm, Konsole, Gnome Console)
                from sixel.converter import SixelConverter
                buf.seek(0)
                conv = SixelConverter(buf, ncolor=64, fast=True)
                out_buf = io.StringIO()
                conv.write(out_buf)
                sixel_data = out_buf.getvalue()
                output = sixel_data + "\n" + "\n".join(" " * w for _ in range(h - 1))
                
            self._cached_renderable = RawAnsi(output)
            self._last_state_key = current_key
            return self._cached_renderable
        else:
            # Standard ANSI Unicode half-blocks (pixelated)
            lines = []
            for cy in range(0, canvas_h, 2):
                line_parts = []
                for cx in range(canvas_w):
                    p1 = canvas.getpixel((cx, cy))
                    p2 = canvas.getpixel((cx, cy + 1)) if cy + 1 < canvas_h else (0, 0, 0, 255)
                    r1, g1, b1 = p1[:3]
                    r2, g2, b2 = p2[:3]
                    line_parts.append(f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m▄")
                lines.append("".join(line_parts))
            
            ansi_output = "\n".join(lines) + "\x1b[0m"
            self._cached_renderable = Text.from_ansi(ansi_output)
            self._last_state_key = current_key
            return self._cached_renderable


class TypewriterLabel(Static):
    _full_text: str = ""
    _displayed: str = ""
    _worker = None

    def set_text(self, text: str, speed: float = 0.02) -> None:
        self._full_text = text
        self._displayed = ""
        self.update("")
        if self._worker:
            self._worker.cancel()
        
        self._worker = self.run_worker(
            self._run_typewriter(text, speed),
            group="typewriter",
            exclusive=True
        )

    async def _run_typewriter(self, text: str, speed: float) -> None:
        try:
            self._displayed = ""
            for ch in text:
                self._displayed += ch
                self.update(self._displayed)
                await asyncio.sleep(speed)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._displayed = text
            self.update(text)

    def skip(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._displayed = self._full_text
        self.update(self._displayed)

    def is_complete(self) -> bool:
        return self._displayed == self._full_text


class DialogueBox(Container):
    DEFAULT_CSS = """
    DialogueBox {
        height: auto;
        border: tall #00e5ff 40%;
        background: #0a0a1a;
        padding: 1 2;
    }

    #char-name {
        text-style: bold;
        padding: 0 1;
        margin-bottom: 1;
    }

    #dialogue-text {
        padding: 0 1;
        color: white;
    }

    #hint-line {
        color: gray;
        padding: 0 1;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="char-name")
        yield TypewriterLabel("", id="dialogue-text")
        yield Label(
            "[dim]  SPACE / ENTER to continue  |  Q to quit  |  R to restart[/]",
            id="hint-line",
            markup=True,
        )

    def on_mount(self) -> None:
        self._char_label = self.query_one("#char-name", Label)
        self._text_label = self.query_one("#dialogue-text", TypewriterLabel)
        self.current_character = ""
        self.current_text = ""

    def display_dialogue(self, character: str, text: str, char_color: str | None = None) -> None:
        self.current_character = character
        self.current_text = text
        color = char_color or _CHAR_COLORS.get(character, _DEFAULT_CHAR_COLOR)
        self._char_label.update(f"[bold {color}]{character}[/]")
        self._text_label.set_text(text)

    def skip_animation(self) -> bool:
        was_running = not self._text_label.is_complete()
        self._text_label.skip()
        return was_running

    def clear(self) -> None:
        self.current_character = ""
        self.current_text = ""
        self._char_label.update("")
        if self._text_label._worker:
            self._text_label._worker.cancel()
        self._text_label._full_text = ""
        self._text_label._displayed = ""
        self._text_label.update("")

    def show_menu_prompt(self, prompt: str) -> None:
        self.current_character = "CHOICE"
        self.current_text = prompt if prompt else "Choose your destiny:"
        self._char_label.update("[bold #c77dff]--- CHOICE ---[/]")
        self._text_label.set_text(self.current_text, speed=0.01)


class ChoiceMenu(Container):
    DEFAULT_CSS = """
    ChoiceMenu {
        display: none;
        layer: overlay;
        position: absolute;
        offset: 0 75%;
        height: 25%;
        width: 100%;
        background: #07071a;
        border: tall #c77dff;
        padding: 1 4;
    }

    ChoiceMenu ListView {
        border: none;
        height: auto;
        background: transparent;
    }

    ChoiceMenu ListItem {
        padding: 0 2;
        color: #d4e8ff;
        background: transparent;
    }

    ChoiceMenu ListItem:hover,
    ChoiceMenu ListItem.-highlighted {
        background: #1a1a4a;
        color: #00e5ff;
    }

    #menu-title {
        text-align: center;
        color: #c77dff;
        text-style: bold;
        padding-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="menu-title")
        yield ListView(id="choice-list")

    def show_choices(self, prompt: str, choices: list[tuple[str, str]]) -> None:
        clear_terminal_graphics()
        self.query_one("#menu-title").update(f"[bold #c77dff]* {prompt or 'Choose an option:'} *[/]")
        lv = self.query_one("#choice-list", ListView)
        lv.clear()
        for label, _ in choices:
            lv.append(ListItem(Label(f">  {label}")))
        self.display = True
        self.focus()
        lv.focus()
        lv.index = 0
        if self.app:
            try:
                self.app._refresh_visual()
            except Exception:
                pass

    def hide_menu(self) -> None:
        clear_terminal_graphics()
        self.display = False
        if self.app:
            try:
                self.app._refresh_visual()
                self.app.query_one("#dialogue-panel").focus()
            except Exception:
                pass



# ─────────────────────────────────────────────────────────────
# SECTION 5 ▸ SAVE / LOAD MENU OVERLAY
# ─────────────────────────────────────────────────────────────

class SaveLoadMenu(Container):
    DEFAULT_CSS = """
    SaveLoadMenu {
        display: none;
        layer: overlay;
        position: absolute;
        offset: 0 75%;
        height: 25%;
        width: 100%;
        background: #051a05;
        border: tall #69ff47;
        padding: 1 4;
    }

    SaveLoadMenu ListView {
        border: none;
        height: auto;
        background: transparent;
    }

    SaveLoadMenu ListItem {
        padding: 0 2;
        color: #d4e8ff;
        background: transparent;
    }

    SaveLoadMenu ListItem:hover,
    SaveLoadMenu ListItem.-highlighted {
        background: #1a4a1a;
        color: #69ff47;
    }

    #saveload-title {
        text-align: center;
        color: #69ff47;
        text-style: bold;
        padding-bottom: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mode = "save"
        self._saves_dir: Path | None = None
        self._app_ref: RenpyPlayerApp | None = None

    def compose(self) -> ComposeResult:
        yield Label("", id="saveload-title")
        yield ListView(id="save-list")

    def show_menu(self, mode: str, saves_dir: Path, app_ref: RenpyPlayerApp) -> None:
        clear_terminal_graphics()
        self.mode = mode
        self._saves_dir = saves_dir
        self._app_ref = app_ref
        
        self.query_one("#saveload-title").update(
            f"[bold #69ff47]* {mode.upper()} GAME *[/]"
        )
        
        # Load slots info
        lv = self.query_one("#save-list", ListView)
        lv.clear()
        
        for i in range(1, 6):
            save_path = self._saves_dir / f"slot_{i}.json"
            if save_path.exists():
                try:
                    data = json.loads(save_path.read_text(encoding="utf-8"))
                    time_str = data.get("timestamp", "Unknown date")
                    preview = data.get("text", "")[:35]
                    char = data.get("character", "Narrator")
                    slot_desc = f"Slot {i}: {time_str} - {char}: \"{preview}...\""
                except Exception:
                    slot_desc = f"Slot {i}: [Corrupted save file]"
            else:
                slot_desc = f"Slot {i}: [Empty]"
            lv.append(ListItem(Label(f">  {slot_desc}")))
            
        self.display = True
        self.focus()
        lv.focus()
        lv.index = 0
        if self.app:
            try:
                self.app._refresh_visual()
            except Exception:
                pass

    def hide_menu(self) -> None:
        clear_terminal_graphics()
        self.display = False
        if self.app:
            try:
                self.app._refresh_visual()
                self.app.query_one("#dialogue-panel").focus()
            except Exception:
                pass



# ─────────────────────────────────────────────────────────────
# SECTION 6 ▸ MAIN APPLICATION CLASS
# ─────────────────────────────────────────────────────────────

class RenpyPlayerApp(App[None]):
    TITLE = "◈ Ren'Py TUI Player"

    
    CSS = """
    Screen {
        background: #050514;
        layers: base overlay;
    }

    #root-layout {
        height: 1fr;
        layout: vertical;
    }

    #visual-panel {
        height: 75%;
        layer: base;
    }

    #dialogue-panel {
        height: 25%;
        layer: base;
    }

    #title-bar {
        height: 1;
        background: #00102a;
        color: #00e5ff;
        text-align: center;
        text-style: bold;
    }

    Footer {
        background: #00102a;
        color: #4fc3f7;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "advance", "Advance"),
        Binding("right", "advance", "Advance"),
        Binding("q", "quit", "Quit"),
        Binding("r", "restart", "Restart"),
        Binding("v", "toggle_visuals", "Quality"),
        Binding("ctrl+f", "toggle_skip", "Skip"),
        Binding("s", "open_save", "Save"),
        Binding("l", "open_load", "Load"),
        Binding("escape", "close_menu", "Back", show=False),
    ]

    _visual_mode = reactive("high")
    _skip_mode = reactive(False)

    def __init__(self, nodes: list[ScriptNode], image_map: dict[tuple[str, ...], ImageTarget], original_game_dir: Path, game_name: str = "default_game") -> None:
        super().__init__()
        self._engine = ScriptEngine(nodes)
        self._image_map = image_map
        self._game_name = game_name
        self._original_game_dir = original_game_dir
        self._saves_dir = Path.home() / ".config" / "renpy_player" / game_name / "saves"

        self._waiting_for_choice = False
        self._pending_menu_node: ScriptNode | None = None
        
        # Audio map index and active playback track processes
        self._audio_map = index_audio(original_game_dir)
        self._audio_processes: dict[str, subprocess.Popen | None] = {
            "music": None,
            "sound": None,
            "voice": None
        }

    def on_unmount(self) -> None:
        for channel in list(self._audio_processes.keys()):
            stop_audio(channel, self._audio_processes)
        import shutil
        try:
            shutil.rmtree("/tmp/renpy_audio", ignore_errors=True)
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Label("  ◈  Ren'Py Terminal Visual Novel Player  ◈", id="title-bar")
        with Vertical(id="root-layout"):
            yield VisualArea(self._engine, self._image_map, id="visual-panel")
            yield DialogueBox(id="dialogue-panel")
        yield ChoiceMenu(id="choice-menu")
        yield SaveLoadMenu(id="saveload-menu")
        yield Footer()

    def on_mount(self) -> None:
        self._engine.jump_to("start")
        self._refresh_visual()
        self._advance_engine()

    def _refresh_visual(self) -> None:
        try:
            self.query_one("#visual-panel", VisualArea).refresh()
        except NoMatches:
            pass

    def watch__visual_mode(self, mode: str) -> None:
        self._refresh_visual()

    def watch__skip_mode(self, active: bool) -> None:
        try:
            db = self.query_one("#dialogue-panel", DialogueBox)
            hint = db.query_one("#hint-line", Label)
            if active:
                hint.update("[bold #69ff47]>>> SKIPPING TEXT (Press TAB to stop) <<<[/]")
            else:
                hint.update("[dim]  SPACE / ENTER to continue  |  Q to quit  |  R to restart[/]")
        except NoMatches:
            pass

    def _advance_engine(self) -> None:
        while True:
            node = self._engine.step()
            if node is None:
                self.query_one("#dialogue-panel", DialogueBox).display_dialogue(
                    "SYSTEM", "[End of script. Press R to restart or Q to quit.]"
                )
                return

            if node.kind == NodeType.PLAY:
                channel = node.data["channel"]
                audio_file = node.data["file"]
                if channel == "music":
                    self._engine.state.music_track = audio_file
                if not (self._skip_mode and channel in {"sound", "voice"}):
                    play_audio(channel, audio_file, self._audio_map, self._audio_processes)
                continue

            if node.kind == NodeType.STOP:
                channel = node.data["channel"]
                if channel == "music":
                    self._engine.state.music_track = ""
                stop_audio(channel, self._audio_processes)
                continue

            if node.kind == NodeType.DIALOGUE:
                self._refresh_visual()
                char = node.data["character"]
                text = node.data["text"]
                color = node.data["color"]
                self.query_one("#dialogue-panel", DialogueBox).display_dialogue(char, text, color)
                return

            if node.kind == NodeType.MENU:
                self._refresh_visual()
                self._show_menu(node)
                return

    def _show_menu(self, node: ScriptNode) -> None:
        self._waiting_for_choice = True
        self._pending_menu_node = node
        prompt = node.data.get("prompt", "")
        choices = node.data["choices"]

        # Automatically stop Fast Skip Mode when choice menu is hit
        if self._skip_mode:
            self._skip_mode = False

        self.query_one("#dialogue-panel", DialogueBox).show_menu_prompt(prompt)
        self.query_one("#choice-menu", ChoiceMenu).show_choices(prompt, choices)

    @on(ListView.Selected, "#choice-list")
    def on_choice_selected(self, event: ListView.Selected) -> None:
        if not self._waiting_for_choice or self._pending_menu_node is None:
            return

        idx = event.list_view.index
        choices = self._pending_menu_node.data["choices"]
        if idx is not None and 0 <= idx < len(choices):
            _, jump_target = choices[idx]
            if jump_target:
                self._engine.jump_to(jump_target)

        self.query_one("#choice-menu", ChoiceMenu).hide_menu()
        self._waiting_for_choice = False
        self._pending_menu_node = None
        self._refresh_visual()
        self._advance_engine()

    @on(ListView.Selected, "#save-list")
    def on_save_selected(self, event: ListView.Selected) -> None:
        menu = self.query_one("#saveload-menu", SaveLoadMenu)
        idx = event.list_view.index
        if idx is None or not (0 <= idx < 5):
            return
            
        slot_num = idx + 1
        save_path = menu._saves_dir / f"slot_{slot_num}.json"
        
        if menu.mode == "save":
            st = self._engine.state
            dialogue_box = self.query_one("#dialogue-panel", DialogueBox)
            
            clean_speaker = dialogue_box.current_character or "NARRATOR"
            save_text = dialogue_box.current_text or ""
                
            save_data = {
                "index": self._engine.state.current_index,
                "bg": st.bg,
                "sprites": st.sprites,
                "music_track": st.music_track,
                "character": clean_speaker,
                "text": save_text,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            try:
                menu._saves_dir.mkdir(parents=True, exist_ok=True)
                save_path.write_text(json.dumps(save_data, indent=2), encoding="utf-8")
                self.notify(f"Game saved to Slot {slot_num}")
            except Exception as e:
                self.notify(f"Save failed: {e}", severity="error")
                
        elif menu.mode == "load":
            if not save_path.exists():
                self.notify(f"Slot {slot_num} is empty!", severity="warning")
                menu.hide_menu()
                return
                
            try:
                save_data = json.loads(save_path.read_text(encoding="utf-8"))
                self._engine.state.current_index = save_data["index"]
                self._engine.state.bg = save_data["bg"]
                self._engine.state.sprites = save_data["sprites"]
                
                loaded_track = save_data.get("music_track", "")
                self._engine.state.music_track = loaded_track
                if loaded_track:
                    play_audio("music", loaded_track, self._audio_map, self._audio_processes)
                else:
                    stop_audio("music", self._audio_processes)
                
                self._skip_mode = False
                
                menu.hide_menu()
                self._refresh_visual()
                
                char = save_data["character"]
                text = save_data["text"]
                self.query_one("#dialogue-panel", DialogueBox).display_dialogue(char, text)
                self.notify(f"Game loaded from Slot {slot_num}")
            except Exception as e:
                self.notify(f"Load failed: {e}", severity="error")
                
        menu.hide_menu()

    async def _skip_loop(self) -> None:
        """Coroutine that advances the game extremely fast while Skip Mode is active."""
        while self._skip_mode:
            if self._waiting_for_choice:
                self._skip_mode = False
                break
            
            # Instantly display the current typewriter animation
            dialogue_box = self.query_one("#dialogue-panel", DialogueBox)
            dialogue_box.skip_animation()
            
            self._advance_engine()
            # 0.05 seconds delay between skip steps
            await asyncio.sleep(0.05)

    def action_toggle_visuals(self) -> None:
        if self._visual_mode == "pixel":
            self._visual_mode = "high"
            self.notify("Graphics Quality: High Resolution (Sixel)")
        else:
            self._visual_mode = "pixel"
            self.notify("Graphics Quality: Pixelated (ANSI blocks)")

    def action_toggle_skip(self) -> None:
        if self._waiting_for_choice:
            return
        self._skip_mode = not self._skip_mode
        if self._skip_mode:
            self.notify("Skip Mode: Active (Press TAB to stop)")
            self.run_worker(self._skip_loop(), group="skip_loop", exclusive=True)
        else:
            self.notify("Skip Mode: Disabled")

    def action_open_save(self) -> None:
        if self._waiting_for_choice:
            return
        menu = self.query_one("#saveload-menu", SaveLoadMenu)
        menu.show_menu("save", self._saves_dir, self)

    def action_open_load(self) -> None:
        menu = self.query_one("#saveload-menu", SaveLoadMenu)
        menu.show_menu("load", self._saves_dir, self)

    def action_close_menu(self) -> None:
        try:
            menu = self.query_one("#saveload-menu", SaveLoadMenu)
            if menu.display:
                menu.hide_menu()
        except NoMatches:
            pass

    def action_advance(self) -> None:
        if self._skip_mode:
            self._skip_mode = False
            return

        if self._waiting_for_choice:
            return

        dialogue_box = self.query_one("#dialogue-panel", DialogueBox)
        if dialogue_box.skip_animation():
            return

        self._advance_engine()

    def action_restart(self) -> None:
        self._engine.reset()
        self._engine.jump_to("start")
        self._waiting_for_choice = False
        self._pending_menu_node = None
        self._skip_mode = False
        for channel in list(self._audio_processes.keys()):
            stop_audio(channel, self._audio_processes)
        try:
            self.query_one("#choice-menu", ChoiceMenu).hide_menu()
            self.query_one("#saveload-menu", SaveLoadMenu).hide_menu()
        except NoMatches:
            pass
        self.query_one("#dialogue-panel", DialogueBox).clear()
        self._refresh_visual()
        self._advance_engine()

    def action_quit(self) -> None:
        self.exit()


# ─────────────────────────────────────────────────────────────
# SECTION 7 ▸ LAUNCHER ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    # Resolve game directory
    if len(sys.argv) < 2:
        print("Error: No Ren'Py game path provided.", file=sys.stderr)
        print("Usage: run.sh <path_to_renpy_game_folder>", file=sys.stderr)
        sys.exit(1)
        
    target_path = Path(sys.argv[1]).resolve()
    if not target_path.exists():
        print(f"Error: Path '{target_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Determine game base path
    game_dir = target_path / "game" if (target_path / "game").is_dir() else target_path

    # Check if there are .rpy files
    rpy_files = sorted(list(game_dir.rglob("*.rpy")))
    
    # Cache setup if no .rpy files are found in game directory
    cache_dir = Path.home() / ".cache" / "renpy_player" / target_path.name / "game"

    
    if not rpy_files:
        rpa_files = list(game_dir.glob("*.rpa"))
        rpyc_files = list(game_dir.rglob("*.rpyc"))
        
        if rpa_files or rpyc_files:
            print("No source .rpy files found. Checking cache...", file=sys.stderr)
            cache_rpy = list(cache_dir.rglob("*.rpy"))
            if cache_rpy:
                print("Using previously cached extracted/decompiled scripts.", file=sys.stderr)
                game_dir = cache_dir
                rpy_files = sorted(cache_rpy)
            else:
                print("Extracting and decompiling scripts to cache (this happens once)...", file=sys.stderr)
                cache_dir.mkdir(parents=True, exist_ok=True)
                
                # 1. Extract only scripts (.rpy, .rpyc) from .rpa files
                import unrpa
                for rpa_path in rpa_files:
                    print(f"Scanning archive {rpa_path.name}...", file=sys.stderr)
                    try:
                        with open(rpa_path, "rb") as f:
                            un = unrpa.UnRPA(str(rpa_path))
                            index = un.get_index(f)
                            for name, data in index.items():
                                if name.endswith((".rpy", ".rpyc")):
                                    dest = cache_dir / name
                                    dest.parent.mkdir(parents=True, exist_ok=True)
                                    offset, length, prefix = data[0]
                                    f.seek(offset)
                                    file_bytes = prefix + f.read(length)
                                    dest.write_bytes(file_bytes)
                    except Exception as e:
                        print(f"Warning: Failed to extract from {rpa_path.name}: {e}", file=sys.stderr)
                
                # 2. Copy any direct .rpyc files
                for rpyc in rpyc_files:
                    try:
                        rel = rpyc.relative_to(game_dir)
                        dest = cache_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(rpyc, dest)
                    except Exception:
                        pass
                
                # 3. Decompile all .rpyc files in cache
                print("Decompiling scripts...", file=sys.stderr)
                import rpycdec
                from rpycdec.decompile import decompile
                try:
                    decompile(str(cache_dir), str(cache_dir))
                except Exception as e:
                    print(f"Decompilation warning: {e}", file=sys.stderr)
                
                # 4. Reload files from cache
                rpy_files = sorted(list(cache_dir.rglob("*.rpy")))
                game_dir = cache_dir
                
        if not rpy_files:
            print(f"Error: No '.rpy' files found in '{game_dir}'.", file=sys.stderr)
            sys.exit(1)

    # Index assets (scan original game path for image files & RPA archives)
    print("Indexing assets...", file=sys.stderr)
    original_game_dir = target_path / "game" if (target_path / "game").is_dir() else target_path
    image_map = index_images(original_game_dir)
    print(f"Indexed {len(image_map)} images.", file=sys.stderr)

    # Parse all script files
    parser = RenpyParser()
    
    # Pass 1: Scan for character definitions first
    print("Pre-scanning character definitions...", file=sys.stderr)
    for rpy in rpy_files:
        try:
            content = rpy.read_text(encoding="utf-8")
            for line in content.splitlines():
                stripped = line.strip()
                if m := parser._RE_DEFINE_CHAR.match(stripped):
                    var = m.group(1)
                    name = m.group(2)
                    color = m.group(3) if len(m.groups()) >= 3 else None
                    parser.characters[var] = (name, color)
        except Exception:
            pass

    # Pass 2: Parse actual script nodes
    all_nodes = []
    for rpy in rpy_files:
        print(f"Parsing '{rpy.name}'...", file=sys.stderr)
        try:
            content = rpy.read_text(encoding="utf-8")
            all_nodes.extend(parser.parse_content(content, filename=rpy.name))
        except Exception as e:
            print(f"Warning: Failed to parse '{rpy.name}': {e}", file=sys.stderr)

    if not all_nodes:
        print("Error: Parsed script holds no executable nodes.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(all_nodes)} total execution nodes.", file=sys.stderr)
    print("Launching TUI Player...", file=sys.stderr)

    # Launch app
    app = RenpyPlayerApp(all_nodes, image_map, original_game_dir=original_game_dir, game_name=target_path.name)
    app.run()


if __name__ == "__main__":
    main()
