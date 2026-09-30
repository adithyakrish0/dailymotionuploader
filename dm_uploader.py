"""
Dailymotion Cloud Uploader — Desktop GUI Queue Manager
===================================================
CustomTkinter GUI application that scans local source directory for .mp4/.json
video pairs, copies them into the repository `uploaded/` queue folder, and executes
Git sync (`git add`, `git commit`, `git push origin main`) so GitHub Actions
headless runners can publish them on schedule.
"""

import os
import sys
import json
import time
import shutil
import threading
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# Reconfigure stdout/stderr encoding for Windows console safety
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import customtkinter as ctk
from dotenv import load_dotenv

# Load local environment variables
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# ─────────────────────────────────────────────
# CONSTANTS & PATHS
# ─────────────────────────────────────────────

SOURCE_UPLOADED_DIR_STR = os.getenv(
    "SOURCE_UPLOADED_DIR",
    r"C:\Users\Adithyakrishnan\Desktop\python\ytvidshorts\output"
)
SOURCE_UPLOADED_DIR = Path(SOURCE_UPLOADED_DIR_STR)
REPO_QUEUE_DIR = PROJECT_ROOT / "uploaded"

STATE_FILE = PROJECT_ROOT / "schedule_state.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"
CLIENT_SECRET_FILE = PROJECT_ROOT / "client_secret.json"

IST = ZoneInfo("Asia/Kolkata")
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

TARGET_SLOTS_IST = [
    (17, 0),    # 5:00 PM IST (7:30 AM EST - US Morning Wake-up)
    (22, 0),    # 10:00 PM IST (12:30 PM EST - US Lunch Peak)
    (3, 0),     # 3:00 AM IST (5:30 PM EST - US Evening Commute/Gym)
    (6, 30),    # 6:30 AM IST (9:00 PM EST / 6:00 PM PST - US Prime-Time Couch)
]

FILE_STABILITY_WAIT = 3  # seconds
MAX_DAILY_UPLOADS = 100


# ─────────────────────────────────────────────
# SCHEDULE MANAGER
# ─────────────────────────────────────────────

class ScheduleManager:
    """Manages schedule_state.json ledger for display & quota tracking."""

    def __init__(self, state_path: Path = STATE_FILE):
        self.state_path = state_path
        self.state = self._load_state()

    def _load_state(self) -> dict:
        """Load state from disk or return fresh default."""
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                pass
        return {
            "last_scheduled_timestamp": None,
            "booked_slots": [],
            "uploads_today": 0,
            "current_pt_date": datetime.now(PT).strftime("%Y-%m-%d"),
        }

    def check_and_reset_quota(self):
        """Reset uploads_today if the PT date has changed."""
        current_pt = datetime.now(PT).strftime("%Y-%m-%d")
        if self.state.get("current_pt_date") != current_pt:
            self.state["uploads_today"] = 0
            self.state["current_pt_date"] = current_pt

    def get_uploads_today(self) -> int:
        """Return number of uploads recorded today."""
        self.check_and_reset_quota()
        return self.state.get("uploads_today", 0)

    def get_upcoming_slots(self, n: int = 4) -> list[dict]:
        """Return the last N booked slots for display."""
        slots = self.state.get("booked_slots", [])
        return slots[-n:] if len(slots) >= n else slots

    @staticmethod
    def time_until_quota_reset() -> timedelta:
        """Calculate time remaining until midnight Pacific Time."""
        now_pt = datetime.now(PT)
        midnight_pt = datetime(
            now_pt.year, now_pt.month, now_pt.day,
            0, 0, 0, tzinfo=PT
        ) + timedelta(days=1)
        return midnight_pt - now_pt


# ─────────────────────────────────────────────
# FILE SCANNER & STABILITY GUARD
# ─────────────────────────────────────────────

class FileScanner:
    """Scans source folder for valid .mp4 / .json pairs."""

    def __init__(self, source_dir: Path = SOURCE_UPLOADED_DIR):
        self.source_dir = source_dir

    def scan(self) -> list[tuple[Path, Path]]:
        """Find matching .mp4 and .json pairs in source directory."""
        candidate_dirs = [self.source_dir]
        
        # Add output fallback if primary directory is different
        alt_output = Path(r"C:\Users\Adithyakrishnan\Desktop\python\ytvidshorts\output")
        if alt_output not in candidate_dirs:
            candidate_dirs.append(alt_output)

        # Build set of stems already in the repo queue to skip re-processing
        already_queued = set()
        if REPO_QUEUE_DIR.exists():
            already_queued = {f.stem for f in REPO_QUEUE_DIR.glob("*.mp4")}

        pairs = []
        seen_stems = set()

        for c_dir in candidate_dirs:
            if not c_dir.exists():
                continue
            mp4_files = {f.stem: f for f in c_dir.glob("*.mp4")}
            json_files = {f.stem: f for f in c_dir.glob("*.json")}
            for stem, mp4_path in mp4_files.items():
                if stem in json_files and stem not in seen_stems:
                    pairs.append((mp4_path, json_files[stem]))
                    seen_stems.add(stem)

        def get_video_timestamp(json_file: Path, mp4_file: Path) -> tuple[str, float]:
            created_str = ""
            try:
                with open(json_file, "r", encoding="utf-8") as jf:
                    d = json.load(jf)
                    created_str = str(d.get("created_at", "")).strip()
            except Exception:
                pass
            mtime = 0.0
            try:
                mtime = mp4_file.stat().st_mtime
            except Exception:
                pass
            return (created_str, mtime)

        # Sort newest first
        pairs.sort(key=lambda p: get_video_timestamp(p[1], p[0]), reverse=True)
        return pairs

    @staticmethod
    def check_file_stability(filepath: Path, wait_seconds: int = FILE_STABILITY_WAIT) -> bool:
        """Check if file size is static (not still being generated/written)."""
        try:
            size1 = filepath.stat().st_size
            if size1 == 0:
                return False
            time.sleep(wait_seconds)
            size2 = filepath.stat().st_size
            return size1 == size2
        except OSError:
            return False


# ─────────────────────────────────────────────
# GIT CONTROLLER
# ─────────────────────────────────────────────

class GitController:
    """Handles git execution for staging, committing, and pushing queue items."""

    def __init__(self, repo_dir: Path = PROJECT_ROOT, log_callback=None):
        self.repo_dir = repo_dir
        self.log = log_callback or print

    def _run_git(self, args: list[str]) -> tuple[int, str]:
        """Execute a git command in the repository directory."""
        cmd = ["git"] + args
        try:
            res = subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return res.returncode, (res.stdout + res.stderr).strip()
        except Exception as e:
            return -1, str(e)

    def is_git_repo(self) -> bool:
        """Check if current directory is a git repository."""
        code, _ = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return code == 0

    def sync_to_remote(self, commit_msg: str) -> bool:
        """Add queue files, commit, pull remote changes, and push to origin main."""
        if not self.is_git_repo():
            self.log("Initializing local git repository...", "warning")
            code, out = self._run_git(["init"])
            if code != 0:
                self.log(f"Git init failed: {out}", "error")
                return False

        self.log("Fetching latest changes from GitHub...", "info")
        self._run_git(["pull", "--rebase", "--autostash", "origin", "main"])

        self.log("Staging repository changes...", "info")
        self._run_git(["add", "-A"])

        self.log("Committing changes...", "info")
        code, out = self._run_git(["commit", "-m", commit_msg])
        if code != 0 and "nothing to commit" not in out.lower():
            self.log(f"Git commit message: {out}", "info")

        self.log("Pushing queue to GitHub (origin main)...", "info")
        code, out = self._run_git(["push", "origin", "main"])
        if code != 0:
            self.log("Syncing remote updates with rebase...", "info")
            code_pull, out_pull = self._run_git(["pull", "--rebase", "--autostash", "origin", "main"])
            if code_pull != 0:
                self.log(f"Git pull rebase failed: {out_pull}", "error")
                return False

            code_retry, out_retry = self._run_git(["push", "origin", "main"])
            if code_retry != 0:
                code_up, out_up = self._run_git(["push", "-u", "origin", "main"])
                if code_up != 0:
                    self.log(f"Git push failed: {out_up}", "error")
                    return False

        self.log("✅ Git push to remote origin main succeeded!", "success")
        return True


# ─────────────────────────────────────────────
# GUI APPLICATION
# ─────────────────────────────────────────────

class QueueApp(ctk.CTk):
    """Desktop GUI application for queueing Dailymotion & pushing to cloud."""

    BG_DARK = "#0d1117"
    PANEL_BG = "#161b22"
    CARD_BG = "#1c2333"
    ACCENT_CYAN = "#00d4ff"
    ACCENT_PURPLE = "#7c3aed"
    ACCENT_GREEN = "#22c55e"
    ACCENT_RED = "#ef4444"
    ACCENT_AMBER = "#f59e0b"
    TEXT_PRIMARY = "#e6edf3"
    TEXT_SECONDARY = "#8b949e"
    TEXT_DIM = "#484f58"
    BORDER_COLOR = "#30363d"

    def __init__(self):
        super().__init__()

        self.title("🎬 Dailymotion Queue Dashboard & Cloud Sync")
        self.geometry("960x680")
        self.minsize(900, 620)
        self.configure(fg_color=self.BG_DARK)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self._is_running = False
        self.schedule_mgr = ScheduleManager()
        self.schedule_mgr.check_and_reset_quota()

        self._build_header()
        self._build_main_area()
        self._build_action_button()

        self._update_countdown()
        self._refresh_side_panel()

        # Start auto-watcher: checks immediately on launch and repeats every 60s
        self._auto_watch_interval_ms = 60000
        self._empty_folder_ticks = 0
        self._max_empty_ticks_before_close = 3  # 3 minutes of empty folder -> auto close
        self.after(2000, self._auto_watch_tick)

    def _build_header(self):
        """Build top header frame."""
        header = ctk.CTkFrame(self, fg_color=self.PANEL_BG, corner_radius=0, height=56)
        header.pack(fill="x", padx=0, pady=0)
        header.pack_propagate(False)

        title_label = ctk.CTkLabel(
            header,
            text="🎬  Dailymotion Queue Dashboard",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=self.TEXT_PRIMARY,
        )
        title_label.pack(side="left", padx=20, pady=12)

        version_label = ctk.CTkLabel(
            header,
            text="v2.0 (Cloud Sync)",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.TEXT_DIM,
        )
        version_label.pack(side="right", padx=20)

    def _build_main_area(self):
        """Build main console (left) and side status panel (right)."""
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=16, pady=(10, 6))
        main_frame.grid_columnconfigure(0, weight=3)
        main_frame.grid_columnconfigure(1, weight=1)
        main_frame.grid_rowconfigure(0, weight=1)

        # ── Console ──
        console_frame = ctk.CTkFrame(main_frame, fg_color=self.CARD_BG, corner_radius=12, border_width=1, border_color=self.BORDER_COLOR)
        console_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        console_header = ctk.CTkLabel(
            console_frame,
            text="📋  Live Activity Console",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self.ACCENT_CYAN,
            anchor="w",
        )
        console_header.pack(fill="x", padx=16, pady=(12, 4))

        self.console_text = ctk.CTkTextbox(
            console_frame,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=self.BG_DARK,
            text_color=self.TEXT_PRIMARY,
            corner_radius=8,
            border_width=1,
            border_color=self.BORDER_COLOR,
            state="disabled",
            wrap="word",
        )
        self.console_text.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        # ── Side Panel ──
        side_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        side_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # Quota Card
        quota_card = ctk.CTkFrame(side_frame, fg_color=self.CARD_BG, corner_radius=12, border_width=1, border_color=self.BORDER_COLOR)
        quota_card.pack(fill="x", pady=(0, 8))

        quota_header = ctk.CTkLabel(
            quota_card,
            text="📊  Daily Quota Status",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self.ACCENT_PURPLE,
            anchor="w",
        )
        quota_header.pack(fill="x", padx=14, pady=(12, 2))

        self.quota_label = ctk.CTkLabel(
            quota_card,
            text="Uploads Today: 0 / 100",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=14),
            text_color=self.TEXT_PRIMARY,
            anchor="w",
        )
        self.quota_label.pack(fill="x", padx=14, pady=(2, 2))

        self.quota_bar = ctk.CTkProgressBar(
            quota_card,
            progress_color=self.ACCENT_GREEN,
            fg_color=self.BG_DARK,
            height=8,
            corner_radius=4,
        )
        self.quota_bar.pack(fill="x", padx=14, pady=(2, 6))
        self.quota_bar.set(0)

        self.countdown_label = ctk.CTkLabel(
            quota_card,
            text="Resets in: --h --m --s",
            font=ctk.CTkFont(family="Consolas", size=12),
            text_color=self.ACCENT_AMBER,
            anchor="w",
        )
        self.countdown_label.pack(fill="x", padx=14, pady=(0, 12))

        # Queue Depth Card
        queue_card = ctk.CTkFrame(side_frame, fg_color=self.CARD_BG, corner_radius=12, border_width=1, border_color=self.BORDER_COLOR)
        queue_card.pack(fill="x", pady=(0, 8))

        queue_header = ctk.CTkLabel(
            queue_card,
            text="📦  Repository Queue Depth",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self.ACCENT_CYAN,
            anchor="w",
        )
        queue_header.pack(fill="x", padx=14, pady=(12, 4))

        self.queue_depth_label = ctk.CTkLabel(
            queue_card,
            text="0 Videos in Queue",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=15),
            text_color=self.TEXT_PRIMARY,
            anchor="w",
        )
        self.queue_depth_label.pack(fill="x", padx=14, pady=(0, 12))

        # Recent Booked Slots Card
        slots_card = ctk.CTkFrame(side_frame, fg_color=self.CARD_BG, corner_radius=12, border_width=1, border_color=self.BORDER_COLOR)
        slots_card.pack(fill="both", expand=True)

        slots_header = ctk.CTkLabel(
            slots_card,
            text="📅  Recent Upload Ledger",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self.ACCENT_CYAN,
            anchor="w",
        )
        slots_header.pack(fill="x", padx=14, pady=(12, 6))

        slots_container = ctk.CTkFrame(slots_card, fg_color="transparent")
        slots_container.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        self.slot_labels = []
        for i in range(4):
            slot_frame = ctk.CTkFrame(
                slots_container,
                fg_color=self.BG_DARK,
                corner_radius=8,
                height=38,
                border_width=1,
                border_color=self.BORDER_COLOR,
            )
            slot_frame.pack(fill="x", pady=2)
            slot_frame.pack_propagate(False)

            slot_lbl = ctk.CTkLabel(
                slot_frame,
                text="  —  No record",
                font=ctk.CTkFont(family="Consolas", size=11),
                text_color=self.TEXT_DIM,
                anchor="w",
            )
            slot_lbl.pack(fill="x", padx=8, pady=6)
            self.slot_labels.append(slot_lbl)

    def _build_action_button(self):
        """Build bottom action button."""
        button_frame = ctk.CTkFrame(self, fg_color="transparent", height=60)
        button_frame.pack(fill="x", padx=16, pady=(4, 16))

        self.sync_button = ctk.CTkButton(
            button_frame,
            text="🚀  Queue & Push Videos to GitHub Cloud",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            fg_color=self.ACCENT_PURPLE,
            hover_color="#6d28d9",
            text_color="#ffffff",
            corner_radius=12,
            height=48,
            command=self._on_sync_click,
        )
        self.sync_button.pack(fill="x")

    def log(self, message: str, level: str = "info"):
        """Append timestamped message to live log console."""
        timestamp = datetime.now(IST).strftime("%H:%M:%S")
        icons = {
            "info": "ℹ️",
            "success": "✅",
            "warning": "⚠️",
            "error": "❌",
            "sync": "🔄",
        }
        icon = icons.get(level, "•")
        formatted = f"[{timestamp}] {icon}  {message}\n"

        def _append():
            self.console_text.configure(state="normal")
            self.console_text.insert("end", formatted)
            self.console_text.see("end")
            self.console_text.configure(state="disabled")

        self.after(0, _append)

    def _refresh_side_panel(self):
        """Refresh queue depth, quota, and ledger side panel."""
        uploads = self.schedule_mgr.get_uploads_today()
        self.quota_label.configure(text=f"Uploads Today: {uploads} / {MAX_DAILY_UPLOADS}")
        self.quota_bar.set(uploads / MAX_DAILY_UPLOADS)

        # Count repo queue depth
        if REPO_QUEUE_DIR.exists():
            mp4_count = len(list(REPO_QUEUE_DIR.glob("*.mp4")))
            self.queue_depth_label.configure(text=f"{mp4_count} Video(s) in Queue")
        else:
            self.queue_depth_label.configure(text="0 Videos in Queue")

        slots = self.schedule_mgr.get_upcoming_slots(4)
        for i, label in enumerate(self.slot_labels):
            if i < len(slots):
                slot = slots[i]
                name = slot.get("filename", "?")
                if len(name) > 22:
                    name = name[:19] + "..."
                ist_str = slot.get("ist_time", "?")
                label.configure(
                    text=f"  🕐 {ist_str} • {name}",
                    text_color=self.TEXT_PRIMARY,
                )
            else:
                label.configure(text="  —  No record", text_color=self.TEXT_DIM)

    def _update_countdown(self):
        """Update quota reset countdown timer."""
        remaining = ScheduleManager.time_until_quota_reset()
        secs = max(0, int(remaining.total_seconds()))
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        self.countdown_label.configure(text=f"Resets in: {h:02d}h {m:02d}m {s:02d}s")
        self.after(1000, self._update_countdown)

    def _auto_watch_tick(self):
        """Periodic 1-minute interval checker to auto-push any new videos or auto-close after 3 mins idle."""
        try:
            if not self._is_running:
                scanner = FileScanner(SOURCE_UPLOADED_DIR)
                pairs = scanner.scan()
                if pairs:
                    self._empty_folder_ticks = 0  # Reset idle counter
                    self.log(f"⚡ Auto-Watcher: Found {len(pairs)} video pair(s) in source directory! Auto-pushing to cloud queue...", "sync")
                    self._on_sync_click()
                else:
                    self._empty_folder_ticks += 1
                    mins_empty = self._empty_folder_ticks
                    remaining_mins = max(0, self._max_empty_ticks_before_close - self._empty_folder_ticks)
                    
                    if self._empty_folder_ticks >= self._max_empty_ticks_before_close:
                        self.log(f"🚪 Auto-Watcher: No videos detected for {mins_empty} minutes. Auto-closing GUI in 3 seconds to save resources...", "warning")
                        self.after(3000, self.destroy)
                        return
                    else:
                        self.log(f"⏳ Auto-Watcher: No pending videos in folder ({mins_empty}/3 mins empty). Auto-closing in {remaining_mins} min(s) if still empty...", "info")
                    
                    self._refresh_side_panel()
            else:
                self._empty_folder_ticks = 0  # Reset while actively running
        except Exception as e:
            self.log(f"⚠️ Auto-Watcher check error: {e}", "warning")
        finally:
            if self._empty_folder_ticks < self._max_empty_ticks_before_close:
                self.after(self._auto_watch_interval_ms, self._auto_watch_tick)

    def _on_sync_click(self):
        """Handle Queue & Sync button click."""
        if self._is_running:
            return

        self._is_running = True
        self.sync_button.configure(state="disabled", text="⏳  Syncing to GitHub...", fg_color=self.TEXT_DIM)

        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", "end")
        self.console_text.configure(state="disabled")

        threading.Thread(target=self._run_queue_and_sync, daemon=True).start()

    def _run_queue_and_sync(self):
        """Background thread executing file scanning, queue copying, and Git push."""
        try:
            self.log(f"Scanning source directory: {SOURCE_UPLOADED_DIR}", "info")
            scanner = FileScanner(SOURCE_UPLOADED_DIR)
            pairs = scanner.scan()

            REPO_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

            if not pairs:
                self.log("No new video pairs found in source directory.", "warning")
            else:
                self.log(f"Found {len(pairs)} new video pair(s) to process.", "info")

            copied_pairs = []
            for mp4_path, json_path in pairs:
                filename = mp4_path.name
                self.log(f"Checking stability for: {filename}", "info")

                if not scanner.check_file_stability(mp4_path):
                    self.log(f"File still being written or empty: {filename}. Skipping.", "warning")
                    continue

                dest_mp4 = REPO_QUEUE_DIR / mp4_path.name
                dest_json = REPO_QUEUE_DIR / json_path.name

                shutil.copy2(str(mp4_path), str(dest_mp4))
                shutil.copy2(str(json_path), str(dest_json))
                thumb_path = mp4_path.with_name(mp4_path.stem + '_thumb.jpg')
                if thumb_path.exists():
                    shutil.copy2(str(thumb_path), str(REPO_QUEUE_DIR / thumb_path.name))
                copied_pairs.append((mp4_path, json_path))
                self.log(f"Copied {filename} to repository queue.", "success")

            # Git push sync
            git_ctrl = GitController(log_callback=lambda msg, lvl: self.log(msg, lvl))
            commit_msg = f"Queue {len(copied_pairs)} new Short(s) for cloud scheduling"

            push_success = git_ctrl.sync_to_remote(commit_msg)

            if push_success and copied_pairs:
                DEST_UPLOADED_DIR = Path(r"C:\Users\Adithyakrishnan\Desktop\python\ytvidshorts\uploaded")
                DEST_UPLOADED_DIR.mkdir(parents=True, exist_ok=True)
                self.log("Moving original source files to ytvidshorts/uploaded...", "sync")
                moved_count = 0
                for src_mp4, src_json in copied_pairs:
                    # Clean up companion thumbnail if present in output/
                    thumb_candidate = src_mp4.parent / f"{src_mp4.stem}_thumb.jpg"
                    if thumb_candidate.exists():
                        try:
                            os.remove(thumb_candidate)
                        except OSError:
                            pass

                    for src_file in (src_mp4, src_json):
                        if not src_file.exists():
                            self.log(f"⚠️ Source file already gone: {src_file.name}", "warning")
                            continue
                        target_file = DEST_UPLOADED_DIR / src_file.name
                        if src_file.resolve() == target_file.resolve():
                            self.log(f"⏭️ Skipping {src_file.name} (already in uploaded/)", "info")
                            continue
                        if target_file.exists():
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            target_file = DEST_UPLOADED_DIR / f"{src_file.stem}_{ts}{src_file.suffix}"
                        # Retry up to 3 times for Windows file locks
                        for attempt in range(1, 4):
                            try:
                                shutil.move(str(src_file), str(target_file))
                                self.log(f"✅ Moved {src_file.name} ➔ ytvidshorts/uploaded", "success")
                                moved_count += 1
                                break
                            except PermissionError:
                                if attempt < 3:
                                    self.log(f"🔒 File locked: {src_file.name} (retry {attempt}/3)...", "warning")
                                    time.sleep(2)
                                else:
                                    self.log(f"❌ Failed to move {src_file.name} after 3 retries (file locked)", "error")
                            except Exception as e:
                                self.log(f"❌ Error moving {src_file.name}: {e}", "error")
                                break
                self.log(f"Move complete: {moved_count} file(s) relocated to uploaded/", "info")

            self.log("Batch queue and sync workflow finished.", "success")

        except Exception as e:
            self.log(f"Error during queue sync: {e}", "error")

        finally:
            def _cleanup():
                self._is_running = False
                self.sync_button.configure(
                    state="normal",
                    text="🚀  Queue & Push Videos to GitHub Cloud",
                    fg_color=self.ACCENT_PURPLE,
                )
                self._refresh_side_panel()

            self.after(0, _cleanup)


def main():
    REPO_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    app = QueueApp()
    app.mainloop()


if __name__ == "__main__":
    main()
