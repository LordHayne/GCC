#!/usr/bin/env python3
#
# Gaming Command Center — Linux gaming system optimisation
# Copyright (C) 2026 Thomas
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. See the LICENSE file, or <https://www.gnu.org/licenses/>.
#
"""
Gaming Command Center — Kommandozentrale für AMD Ryzen + NVIDIA GPU
Sidebar-based layout with Dashboard, Game Doctor, Benchmark, and Settings pages.
Dark themed GUI with CCD-Parking, GPU-OC, Live Monitoring, and System Scanner.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk, GObject, Gio
import subprocess, os, re, shutil, threading, time
from system_scanner import scan_system
from topology import CPUTopology, format_cpu_list, save_config
import game_db
import steam_scanner
import app_update
import report_stats

# Directory this script lives in — used to load bundled assets (the logo, etc.)
# by an absolute path that works for any user, whether run from source or after
# install.sh (which launches the app straight from this folder).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# Global text-size multiplier. The v2 stylesheet is dense (lots of 9–11px
# labels) which is hard to read on high-DPI/large screens; scaling every
# font-size together lifts legibility while preserving the type hierarchy.
# One knob — easy to expose as a Settings slider later.
UI_FONT_SCALE = 1.2


def scale_font_sizes(css, factor=UI_FONT_SCALE):
    """Multiply every `font-size: Npx` in a stylesheet by `factor` (rounded).
    Layout values (padding, widths) are left alone; only type scales."""
    if factor == 1.0:
        return css
    return re.sub(r"font-size:\s*(\d+)px",
                  lambda m: f"font-size: {max(1, round(int(m.group(1)) * factor))}px",
                  css)


def load_css(provider, css):
    """Feed CSS into a Gtk.CssProvider across GTK versions. load_from_string()
    only exists on GTK 4.12+; older GTK (e.g. Ubuntu 22.04's 4.6, Debian 12's
    4.8) needs load_from_data(bytes). Keeps the app working on the AppImage's
    bundled GTK and on any distro shipping GTK < 4.12."""
    try:
        provider.load_from_string(css)
    except (AttributeError, TypeError):
        provider.load_from_data(css.encode())


# ── System integration (first-run setup) ────────────────────────────────────
# The app runs from source, but Game Mode, the /etc fixes and the app-menu
# launcher need a few root-owned files in place. gaming-cc-setup installs them;
# these helpers detect what's missing and drive it through pkexec, so a user who
# just launched the app never has to open a terminal.
CCD_HELPER_PATH = "/usr/local/bin/gaming-ccd-helper"
ETC_HELPER_PATH = "/usr/local/bin/gaming-cc-etc-helper"
POLKIT_PATH     = "/usr/share/polkit-1/actions/com.gaming.commandcenter.policy"
DESKTOP_PATH    = "/usr/share/applications/com.gaming.commandcenter.desktop"


def integration_status():
    """Which pieces of system integration are present right now."""
    return {
        "helpers":  os.path.exists(CCD_HELPER_PATH) and os.path.exists(ETC_HELPER_PATH),
        "polkit":   os.path.exists(POLKIT_PATH),
        "launcher": os.path.exists(DESKTOP_PATH),
    }


def needs_setup():
    """True if core integration is missing — the helpers + polkit rule that Game
    Mode and the fixes actually need. A missing launcher alone is cosmetic and
    does not force the setup screen."""
    s = integration_status()
    return not (s["helpers"] and s["polkit"])


def run_privileged_setup():
    """Run gaming-cc-setup as root via pkexec. Returns (ok, reason). Mirrors the
    helper convention: success prints SETUP_DONE, failures print 'ERR: ...'."""
    import tempfile
    if not os.path.exists(os.path.join(BASE_DIR, "gaming-cc-setup")):
        return False, "gaming-cc-setup not found next to the app"

    bash = shutil.which("bash") or "/bin/bash"
    appimage = os.environ.get("APPIMAGE")  # set by the AppImage runtime
    src = BASE_DIR
    exec_override = None
    staged = None

    if appimage:
        # Inside an AppImage, BASE_DIR lives in a FUSE mount that root (pkexec)
        # can't read, so copy the files the setup needs into a world-readable
        # temp dir. Point the launcher at the .AppImage itself, since the
        # command-center.py in the mount vanishes when the app closes.
        try:
            staged = tempfile.mkdtemp(prefix="gcc-setup-")
            os.chmod(staged, 0o755)
            for name in ("gaming-cc-setup", "gaming-ccd-helper", "gaming-cc-etc-helper",
                         "com.gaming.commandcenter.policy", "GCC_logo.png",
                         "command-center.py"):
                s = os.path.join(BASE_DIR, name)
                if os.path.exists(s):
                    d = os.path.join(staged, name)
                    shutil.copy(s, d)
                    os.chmod(d, 0o755 if name.startswith("gaming-") else 0o644)
            src = staged
            exec_override = appimage
        except OSError as e:
            return False, f"Could not stage setup files: {e}"

    try:
        cmd = ["pkexec", bash, os.path.join(src, "gaming-cc-setup"), src]
        if exec_override:
            cmd.append(exec_override)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "Setup timed out"
    except OSError as e:
        return False, f"Could not run setup: {e}"
    finally:
        if staged:
            shutil.rmtree(staged, ignore_errors=True)

    if "SETUP_DONE" in (r.stdout or ""):
        return True, ""
    if r.returncode == 126:
        return False, "Authorisation denied"
    err = (r.stderr or "").strip().splitlines()
    reason = err[-1].replace("ERR: ", "") if err else ""
    return False, reason or f"Setup failed (exit {r.returncode})"


# ============================================================
# GPU Info (NVIDIA)
# ============================================================
class GPUInfo:
    def __init__(self):
        self.gr_offset = self.mem_offset = 0
        self.powermizer = 0
        self.update()

    def update(self):
        """Live telemetry — cheap enough to poll every tick (~25 ms)."""
        self.name = ""
        self.vram_total = self.vram_used = 0
        self.power_draw = self.power_limit = self.temp = 0.0
        self.clock_gr = self.clock_mem = self.max_clock_gr = self.max_clock_mem = 0
        self.pstate = ""
        self.util = 0
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,power.draw,power.limit,temperature.gpu,clocks.gr,clocks.mem,pstate,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3)
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            if len(parts) >= 10:
                self.name = parts[0]
                self.vram_total = int(parts[1])
                self.vram_used = int(parts[2])
                self.power_draw = float(parts[3])
                self.power_limit = float(parts[4])
                self.temp = float(parts[5])
                self.clock_gr = int(float(parts[6]))
                self.clock_mem = int(float(parts[7]))
                self.pstate = parts[8]
                self.util = int(float(parts[9]))
        except: pass
        # Query max graphics clock for progress bar
        try:
            r2 = subprocess.run(["nvidia-smi", "--query-gpu=clocks.max.gr", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=2)
            val = r2.stdout.strip()
            if val and val != "[N/A]":
                self.max_clock_gr = int(float(val))
        except: pass

    def update_oc(self):
        """Overclock offsets and PowerMizer mode.

        Three nvidia-settings calls, ~200 ms — by far the most expensive thing
        we poll, and pointless to poll at all: these values only change when
        this app changes them. Read once at startup and after Apply OC.
        """
        for attr, query, pattern in (
            ("gr_offset", "GPUGraphicsClockOffsetAllPerformanceLevels", r'\): (-?\d+)'),
            ("mem_offset", "GPUMemoryTransferRateOffsetAllPerformanceLevels", r'\): (-?\d+)'),
            ("powermizer", "GPUPowerMizerMode", r'\): (\d+)'),
        ):
            try:
                r = subprocess.run(["nvidia-settings", "-q", query],
                                   capture_output=True, text=True, timeout=2)
                m = re.search(pattern, r.stdout)
                if m:
                    setattr(self, attr, int(m.group(1)))
            except (OSError, subprocess.SubprocessError):
                pass


# ============================================================
# Controllers
# ============================================================
class CCDController:
    """Drives the root helper. CPU numbers always come from CPUTopology —
    nothing here assumes a core layout."""

    HELPER = "/usr/local/bin/gaming-ccd-helper"          # runtime, no password
    ETC_HELPER = "/usr/local/bin/gaming-cc-etc-helper"   # persistent, asks for auth

    @staticmethod
    def _run(args, expect, timeout=60, binary=None):
        """Returns (ok, message). The helper prints DONE_* on success and
        'ERR: reason' on stderr, so a cancelled pkexec dialog reads as failure.

        A helper may also print 'NOTE: ...' lines (e.g. where it put a backup);
        those are folded into the success message.
        """
        try:
            r = subprocess.run(["pkexec", binary or CCDController.HELPER] + args,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "Helper timed out"
        except OSError as e:
            return False, f"Could not run helper: {e}"
        out = r.stdout or ""
        if expect in out:
            notes = [ln[6:].strip() for ln in out.splitlines() if ln.startswith("NOTE:")]
            return True, "; ".join(notes)
        err = (r.stderr or "").strip().splitlines()
        reason = err[-1].replace("ERR: ", "") if err else ""
        if r.returncode == 126:
            reason = "Authorisation denied"
        return False, reason or f"Helper failed (exit {r.returncode})"

    @staticmethod
    def park(cpus):
        if not cpus:
            return False, "Nothing to park"
        return CCDController._run(
            ["park", ",".join(str(c) for c in sorted(cpus))], "DONE_PARK")

    @staticmethod
    def unpark(cpus):
        if not cpus:
            return False, "Nothing to unpark"
        return CCDController._run(
            ["unpark", ",".join(str(c) for c in sorted(cpus))], "DONE_UNPARK")

    @staticmethod
    def unpark_all():
        return CCDController._run(["unpark-all"], "DONE_UNPARK")

    @staticmethod
    def helper(action, expect, success_msg):
        """Run a one-word runtime action and turn it into (ok, message)."""
        ok, note = CCDController._run([action], expect, timeout=30)
        return (True, success_msg) if ok else (False, note)

    @staticmethod
    def set_governor(name):
        """Force a cpufreq governor (benchmark uses this). Password-free."""
        ok, _ = CCDController._run(["set-governor", name], "DONE_SETGOVERNOR", timeout=30)
        return ok

    @staticmethod
    def gpu_lock(min_mhz, max_mhz):
        """Lock the NVIDIA graphics clock (Wayland-safe, via nvidia-smi)."""
        return CCDController._run(["gpu-lock", str(min_mhz), str(max_mhz)],
                                  "DONE_GPULOCK", timeout=15)

    @staticmethod
    def gpu_unlock():
        return CCDController._run(["gpu-unlock"], "DONE_GPUUNLOCK", timeout=15)

    @staticmethod
    def etc_helper(action, expect, success_msg):
        """Run a persistent /etc action. Prompts for admin authentication, and
        the helper reports where it put the backup — surface that to the user."""
        ok, note = CCDController._run([action], expect, timeout=120,
                                      binary=CCDController.ETC_HELPER)
        if not ok:
            return False, note
        return True, f"{success_msg} ({note})" if note else success_msg


class GPUController:
    # nvidia-settings ALWAYS exits 0, even when it prints "ERROR: ... permission
    # for operation" and changes nothing (common for OC over XWayland / without
    # the right permission). So the exit code is useless — parse stderr instead.
    @staticmethod
    def _apply(attr, value):
        """Returns (ok, message). Never trusts nvidia-settings' exit code."""
        try:
            r = subprocess.run(["nvidia-settings", "-a", f"{attr}={value}"],
                               capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            return False, "nvidia-settings not installed"
        except (OSError, subprocess.SubprocessError):
            return False, "Could not run nvidia-settings"
        err = (r.stderr or "")
        if "ERROR" in err or "does not have permission" in err:
            if "permission" in err:
                return False, ("Permission denied — GPU overclocking needs X11; "
                               "nvidia-settings can't set it over Wayland/XWayland")
            line = next((l.strip() for l in err.splitlines()
                         if l.strip().startswith("ERROR")), "nvidia-settings error")
            return False, line.replace("ERROR: ", "")
        return True, ""

    @staticmethod
    def set_gr_offset(offset):
        return GPUController._apply("GPUGraphicsClockOffsetAllPerformanceLevels", offset)

    @staticmethod
    def set_mem_offset(offset):
        return GPUController._apply("GPUMemoryTransferRateOffsetAllPerformanceLevels", offset)

    @staticmethod
    def set_powermizer(mode):
        return GPUController._apply("GPUPowerMizerMode", mode)


# ============================================================
# System Doctor Page — system-level checks and fixes (formerly Game Doctor)
# ============================================================
class GameDoctorPage(Gtk.Box):
    """System Doctor page — runs system_scanner.scan_system() and displays results."""

    # What the scanner looks at, grouped for the empty-state preview.
    CHECK_GROUPS = [
        ("System", ["Reboot / Kernel", "Vulkan", "vm.max_map_count", "Open Files"]),
        ("GPU", ["NVIDIA Driver", "GPU P-State", "ReBAR", "Coolbits", "Modeset"]),
        ("CPU", ["CPU Governor", "CPU Boost", "CCD / Game Mode"]),
        ("Gaming tools", ["GameMode", "gamescope", "GE-Proton"]),
        ("Power & I/O", ["SATA Link Power", "Audio Power Save"]),
        ("Session", ["Wayland / X11", "NVIDIA Modprobe", "Monitor"]),
    ]

    def __init__(self, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)

        self.scanning = False
        self.last_checks = []          # what the current rows reflect
        self._rows = {}                # check.name -> (msg_lbl, fix_btn or None)

        # Single source of truth for which checks have a one-click fix. Used both
        # to decide whether a row shows a FIX button and to run it.
        self._fixes = {
            "CCD / Game Mode":   self._fix_game_mode,
            "Audio Power Save":  self._fix_audio,
            "GameMode Config":   self._fix_gamemode_ini,
            "NVIDIA Modprobe":   self._fix_modprobe,
            "Coolbits / GPU-OC": self._fix_coolbits,
            "vm.max_map_count":  self._fix_max_map_count,
            "GameMode":          lambda: self._install_package("gamemode"),
            "gamescope":         lambda: self._install_package("gamescope"),
        }

        # Info line + action buttons
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.set_margin_start(24); top_bar.set_margin_end(24)
        top_bar.set_margin_top(16); top_bar.set_margin_bottom(8)
        self.scan_status_lbl = Gtk.Label(label="not scanned yet")
        self.scan_status_lbl.add_css_class("game-meta"); self.scan_status_lbl.set_xalign(0)
        top_bar.append(self.scan_status_lbl)
        spacer = Gtk.Box(); spacer.set_hexpand(True); top_bar.append(spacer)

        self.copy_btn = Gtk.Button(label="COPY REPORT")
        self.copy_btn.add_css_class("btn-apply-sm")
        self.copy_btn.connect("clicked", self.on_copy_clicked)
        self.copy_btn.set_visible(False)
        top_bar.append(self.copy_btn)

        self.fix_all_btn = Gtk.Button(label="FIX ALL")
        self.fix_all_btn.add_css_class("btn-fix")
        self.fix_all_btn.connect("clicked", self.on_fix_all_clicked)
        self.fix_all_btn.set_visible(False)
        top_bar.append(self.fix_all_btn)

        self.scan_btn = Gtk.Button(label="RUN FULL SCAN")
        self.scan_btn.add_css_class("btn-apply-sm")
        self.scan_btn.connect("clicked", self.on_scan_clicked)
        top_bar.append(self.scan_btn)
        self.append(top_bar)

        # Summary banner (hidden until a scan completes)
        self.summary_lbl = Gtk.Label(label="")
        self.summary_lbl.set_xalign(0); self.summary_lbl.set_wrap(True)
        self.summary_lbl.add_css_class("doctor-summary")
        self.summary_lbl.set_margin_start(24); self.summary_lbl.set_margin_end(24)
        self.summary_lbl.set_visible(False)
        self.append(self.summary_lbl)

        # Scrolled area for results
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_vexpand(True)
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results_box.set_margin_start(24)
        self.results_box.set_margin_end(24)
        self.results_box.set_margin_top(8)
        self.results_box.set_margin_bottom(16)
        self.results_box.append(self._build_empty_state())

        self.scroll.set_child(self.results_box)
        self.append(self.scroll)

    def _build_empty_state(self):
        """Inviting pre-scan state: icon, one line of what it does, and a preview
        of the check categories so the page isn't just a lone button."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(36)
        box.set_margin_bottom(24)

        icon = Gtk.Label()
        icon.set_markup("<span size='46000'>🩺</span>")
        box.append(icon)

        headline = Gtk.Label()
        headline.set_markup("<span size='15000' weight='bold' color='#c0caf5'>Ready to check your system</span>")
        box.append(headline)

        desc = Gtk.Label(label="20+ checks across your GPU, CPU, gaming tools, Proton "
                               "and power settings — most with a one-click fix.")
        desc.add_css_class("page-subtitle")
        desc.set_wrap(True)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_max_width_chars(48)
        box.append(desc)

        # Category chips
        grid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        grid.set_margin_top(8)
        for group, checks in self.CHECK_GROUPS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.set_halign(Gtk.Align.CENTER)
            gl = Gtk.Label(label=group)
            gl.add_css_class("doctor-group-label")
            gl.set_size_request(90, -1)
            gl.set_xalign(1.0)
            row.append(gl)
            for name in checks:
                chip = Gtk.Label(label=name)
                chip.add_css_class("doctor-chip")
                row.append(chip)
            grid.append(row)
        box.append(grid)

        hint = Gtk.Label()
        hint.set_markup("<span color='#565f89' size='11000'>Click “Run Full Scan” above to begin</span>")
        hint.set_margin_top(10)
        box.append(hint)
        return box

    def on_scan_clicked(self, btn):
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label("SCANNING…")
        self.scan_status_lbl.set_label("scanning system…")

        child = self.results_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child
        spinner = Gtk.Spinner(); spinner.set_margin_top(40); spinner.start()
        self.results_box.append(spinner)

        start = time.monotonic()

        def run_scan():
            try:
                checks = scan_system()
            except Exception as e:
                checks = []
                GLib.idle_add(lambda: self.scan_status_lbl.set_label(f"Error: {e}"))
            took = time.monotonic() - start
            GLib.idle_add(lambda: self.display_results(checks, took))

        threading.Thread(target=run_scan, daemon=True).start()

    def display_results(self, checks, took=0.0):
        self.scanning = False
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("RUN FULL SCAN")

        child = self.results_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child

        self.last_checks = checks
        self._rows = {}

        crit = sum(1 for c in checks if c.status == "critical")
        ok = sum(1 for c in checks if c.status == "ok")
        warn = sum(1 for c in checks if c.status == "warning")
        info = sum(1 for c in checks if c.status == "info")

        # Critical first, then warnings, info, ok — most urgent at the top.
        order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
        for check in sorted(checks, key=lambda c: order.get(c.status, 4)):
            self.results_box.append(self._build_check_row(check))

        self.scan_status_lbl.set_label(f"{len(checks)} checks · scan took {took:.1f} s")
        self.copy_btn.set_visible(True)
        # FIX ALL only when there is something we can actually auto-fix.
        fixable = [c for c in checks
                   if c.status in ("warning", "critical") and c.name in self._fixes]
        self.fix_all_btn.set_visible(bool(fixable))
        self.fix_all_btn.set_sensitive(True)
        self.fix_all_btn.set_label(f"FIX ALL ({len(fixable)})")

        self.summary_lbl.set_visible(True)
        if crit:
            self.summary_lbl.set_css_classes(["doctor-summary", "summary-critical"])
            self.summary_lbl.set_markup(
                f"<b>{crit} critical issue{'s' if crit != 1 else ''}</b> — "
                f"{warn} warning{'s' if warn != 1 else ''}, {ok} passed")
        elif warn:
            self.summary_lbl.set_css_classes(["doctor-summary", "summary-warn"])
            self.summary_lbl.set_markup(
                f"<b>{warn} warning{'s' if warn != 1 else ''} found</b> — "
                f"{ok} checks passed")
        else:
            self.summary_lbl.set_css_classes(["doctor-summary", "summary-ok"])
            self.summary_lbl.set_markup(f"<b>All good</b> — {ok} checks passed, {info} info")

    def _build_check_row(self, check):
        """A v2 check row: coloured status dot + name + message + optional FIX."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("doctor-row")
        row.add_css_class(f"doctor-row-{check.status}")
        row.set_margin_top(3)

        dot = Gtk.Box(); dot.set_size_request(8, 8)
        dot.set_valign(Gtk.Align.CENTER)
        dot.add_css_class("doctor-dot"); dot.add_css_class(f"dot-{check.status}")
        row.append(dot)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True); text_box.set_valign(Gtk.Align.CENTER)
        name_lbl = Gtk.Label(label=check.name); name_lbl.set_xalign(0)
        name_lbl.add_css_class("doctor-name")
        text_box.append(name_lbl)
        msg = check.message
        if check.fix_message and check.status in ("warning", "critical"):
            msg = f"{check.message} — {check.fix_message}"
        msg_lbl = Gtk.Label(label=msg); msg_lbl.set_xalign(0); msg_lbl.set_wrap(True)
        msg_lbl.add_css_class("doctor-msg")
        text_box.append(msg_lbl)
        row.append(text_box)

        # A FIX button appears only when we genuinely have a one-click fix. A
        # critical item like a pending reboot has no software fix, so it shows
        # its instruction inline instead of a button that does nothing.
        fix_btn = None
        if check.status in ("warning", "critical") and check.name in self._fixes:
            fix_btn = Gtk.Button(label="FIX")
            fix_btn.add_css_class("btn-fix")
            fix_btn.set_valign(Gtk.Align.CENTER)
            fix_btn._msg_lbl = msg_lbl
            fix_btn.connect("clicked", lambda b, c=check: self._apply_fix(c, b))
            row.append(fix_btn)

        self._rows[check.name] = (msg_lbl, fix_btn)
        return row

    # --- individual fixes: each returns (ok, message) and never lies ---

    def _fix_audio(self):
        # Gaming: turn the codec power-save OFF so it never sleeps and pops
        # mid-game. (The power-saving direction lives under the Power Saving
        # toggle, not here.)
        return CCDController.helper("audio-off", "DONE_AUDIO",
                                    "Audio codec power-save turned off (no wake-up pops)")

    def _fix_modprobe(self):
        return CCDController.etc_helper("modprobe", "DONE_MODPROBE",
                                        "NVIDIA modprobe config written — reboot to apply")

    def _fix_coolbits(self):
        return CCDController.etc_helper("coolbits", "DONE_COOLBITS",
                                        "Coolbits enabled — restart your session to apply")

    def _fix_game_mode(self):
        topo = CPUTopology()
        if not topo.complete:
            return False, "CPU layout unknown while cores are parked — restore all cores first"
        keep = topo.keep_ccd()
        plan = topo.park_plan(keep)
        if not plan:
            return False, "Nothing to park — this CPU has only one CCD"
        ok, err = CCDController.park(plan)
        if not ok:
            return False, err
        parked = ", ".join(f"CCD{c}" for c in topo.get_all_ccd_ids() if c != keep)
        return True, f"Game Mode on — kept CCD{keep}, parked {parked}"

    def _fix_gamemode_ini(self):
        topo = CPUTopology()
        if not topo.complete:
            return False, "CPU layout unknown while cores are parked — restore all cores first"
        keep = topo.keep_ccd()
        park = topo.park_plan(keep)
        if topo.ccd_count() < 2 or not park:
            return False, "Single-CCD CPU — no core parking to configure"
        path = os.path.expanduser("~/.config/gamemode.ini")
        config = f"""[general]
desiredgov=performance
renice=0
ioprio=0

[cpu]
park_cores={format_cpu_list(park)}
pin_cores={format_cpu_list(topo.get_ccd_cpus(keep))}

[gpu]
apply_gpu_optimisations=accept-responsibility
nv_powermizer_mode=1
"""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(config)
        except OSError as e:
            return False, f"Could not write {path}: {e}"
        return True, f"gamemode.ini written — pins CCD{keep}, parks {format_cpu_list(park)}"

    @staticmethod
    def _install_package(pkg):
        """pacman needs root, so it goes through pkexec — the old code ran it as
        the user, which always failed while the button still said 'installed'."""
        if not shutil.which("pacman"):
            return False, f"Not an Arch-based distro — install '{pkg}' with your package manager"
        try:
            r = subprocess.run(["pkexec", "pacman", "-S", "--needed", "--noconfirm", pkg],
                               capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return False, f"Install of '{pkg}' timed out"
        except OSError as e:
            return False, f"Could not run pacman: {e}"
        if r.returncode == 0:
            return True, f"{pkg} installed"
        if r.returncode == 126:
            return False, "Authorisation denied"
        err = (r.stderr or "").strip().splitlines()
        return False, err[-1] if err else f"pacman failed (exit {r.returncode})"

    def _fix_max_map_count(self):
        return CCDController.etc_helper("maxmapcount", "DONE_MAXMAPCOUNT",
                                        "vm.max_map_count raised to 2147483642")

    def _apply_fix(self, check, btn, on_done=None):
        """Run one check's fix in a worker thread and report what happened.
        on_done(ok) fires on the main thread once finished — used by FIX ALL to
        chain the next fix."""
        fix = self._fixes.get(check.name)
        if fix is None:
            btn._msg_lbl.set_label("No automatic fix for this check yet")
            if on_done:
                on_done(False)
            return

        btn.set_label("Applying...")
        btn.set_sensitive(False)

        def apply_in_thread():
            try:
                ok, msg = fix()
            except Exception as e:
                ok, msg = False, f"Fix crashed: {e}"

            def update_ui():
                btn._msg_lbl.set_label(msg)
                btn.set_label("Done" if ok else "Retry")
                btn.set_sensitive(not ok)
                if ok:
                    btn.remove_css_class("btn-apply")
                    btn.add_css_class("btn-game-off")
                    # Re-run this one check so the row reflects reality rather
                    # than our claim about it.
                    check.run()
                if on_done:
                    on_done(ok)
                return False

            GLib.idle_add(update_ui)

        threading.Thread(target=apply_in_thread, daemon=True).start()

    def on_fix_all_clicked(self, btn):
        """Apply every auto-fixable warning/critical in turn. Runs them one at a
        time so pkexec prompts don't stack, and stops cleanly at the end."""
        queue = [c for c in self.last_checks
                 if c.status in ("warning", "critical") and c.name in self._fixes]
        if not queue:
            return
        btn.set_sensitive(False)
        btn.set_label("Fixing…")
        self.scan_btn.set_sensitive(False)

        def run_next(index):
            if index >= len(queue):
                btn.set_label("FIX ALL — done")
                self.scan_btn.set_sensitive(True)
                return
            check = queue[index]
            row = self._rows.get(check.name)
            if not row or row[1] is None:      # no button for this row
                run_next(index + 1)
                return
            _msg_lbl, fix_btn = row
            self._apply_fix(check, fix_btn,
                            on_done=lambda _ok, i=index: run_next(i + 1))

        run_next(0)

    def on_copy_clicked(self, btn):
        """Copy a plain-text report to the clipboard — handy for support threads
        and Reddit help posts."""
        icon = {"critical": "[CRIT]", "warning": "[WARN]", "info": "[INFO]", "ok": "[ OK ]"}
        order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
        lines = ["Gaming Command Center — System Doctor report", ""]
        for c in sorted(self.last_checks, key=lambda c: order.get(c.status, 4)):
            lines.append(f"{icon.get(c.status, '[ ?? ]')} {c.name}: {c.message}")
            if c.fix_message and c.status in ("warning", "critical"):
                lines.append(f"        fix: {c.fix_message}")
        text = "\n".join(lines)
        try:
            self.get_clipboard().set(text)
            btn.set_label("COPIED ✓")
            GLib.timeout_add(1800, lambda: (btn.set_label("COPY REPORT"), False)[1])
        except Exception:
            btn.set_label("COPY FAILED")


# ============================================================
# Games Page — per-game fixes from games.yaml
# ============================================================
class GamesPage(Gtk.Box):
    """Detects the user's Steam games, matches them against games.yaml, and
    offers one-click fixes. The database is the trust boundary (game_db only
    ever loads whitelisted fix types); this page just applies them."""

    # --- Community report: no-account channel (Google Form) ---
    # The account-free way to report a result: the app opens a pre-filled Google
    # Form, the user just hits Submit, and the response lands in a sheet the
    # nightly aggregator reads. Filled in once the Form exists:
    #   FORM_PREFILL_URL — the "Get pre-filled link" URL (…/viewform).
    #   FORM_FIELDS      — maps our data keys to the form's entry.NNN field ids.
    # Until BOTH are set, the share dialog falls back to Copy report, so the
    # feature is never broken while the Form is being created.
    FORM_PREFILL_URL = ("https://docs.google.com/forms/d/e/"
                        "1FAIpQLSfrH4faZ5eQ1T4VP1WmwS1w0afjTKfQ_V4bc4NO3oYAitRUZw/"
                        "viewform?usp=pp_url")
    FORM_FIELDS = {
        "game":       "entry.1117300195",
        "appid":      "entry.871360158",
        "fix":        "entry.1999642819",
        "result":     "entry.711869093",
        "gpu":        "entry.742209682",
        "cpu":        "entry.1717487472",
        "session":    "entry.954762000",
        "distro":     "entry.723557649",
        "appversion": "entry.1883040327",
    }

    def __init__(self, win, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self.win = win
        self.db, self.db_err = game_db.load_games()

        # Title lives in the shared header now — this page starts with the
        # info line + rescan button.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(16)
        header.set_margin_bottom(8)
        self.subtitle = Gtk.Label(label="")
        self.subtitle.add_css_class("game-meta")
        self.subtitle.set_halign(Gtk.Align.START)
        self.subtitle.set_wrap(False)
        self.subtitle.set_ellipsize(3)  # PANGO_ELLIPSIZE_END if too narrow
        header.append(self.subtitle)
        spacer = Gtk.Box(); spacer.set_hexpand(True); header.append(spacer)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search games…")
        self.search.set_max_width_chars(22)
        self.search.connect("search-changed", self._on_search)
        header.append(self.search)
        self.rescan_btn = Gtk.Button(label="Rescan")
        self.rescan_btn.add_css_class("btn-apply")
        self.rescan_btn.connect("clicked", lambda *_: self.rescan())
        header.append(self.rescan_btn)
        self.append(header)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.list_box.set_margin_start(16)
        self.list_box.set_margin_end(16)
        self.list_box.set_margin_top(12)
        self.list_box.set_margin_bottom(16)
        scroll.set_child(self.list_box)
        self.append(scroll)

        self._search_text = ""
        self.game_cards = []   # (search_key, card, reveal_fn) for the search filter
        self.rescan()
        self._check_db_update()

    def _check_db_update(self):
        """Refresh the fix database AND the community report tally from GitHub in
        the background (each throttled to once a day). If either changed, reload
        and re-render silently — the community counts grow with no user action."""
        def work():
            db_changed = game_db.maybe_update() == "updated"
            reports_changed = report_stats.maybe_update() == "updated"
            if not (db_changed or reports_changed):
                return
            def apply():
                if db_changed:
                    self.db, self.db_err = game_db.load_games()
                self.rescan()   # rescan re-reads the report cache too
                return False
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()

    # ---------- current system, for `when:` filtering ----------
    def _gpu_vendor(self):
        name = (self.win.gpu.name or "").lower()
        if "nvidia" in name:
            return "nvidia"
        if any(x in name for x in ("amd", "radeon")):
            return "amd"
        if "intel" in name:
            return "intel"
        return None

    def _session(self):
        return "wayland" if os.environ.get("WAYLAND_DISPLAY") else \
               ("x11" if os.environ.get("DISPLAY") else None)

    @staticmethod
    def _cpu_vendor():
        try:
            with open("/proc/cpuinfo") as f:
                info = f.read()
            if "AuthenticAMD" in info:
                return "amd"
            if "GenuineIntel" in info:
                return "intel"
        except OSError:
            pass
        return None

    @staticmethod
    def _is_steam_tool(name):
        if not name:
            return False
        n = name.lower()
        return any(t in n for t in (
            "proton", "steam linux runtime", "steamworks common",
            "steamvr", "redistributable"))

    def _clear(self):
        child = self.list_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt

    def rescan(self):
        self._clear()
        self.game_cards = []
        # The community tally (worked/didn't counts per fix). Read once from the
        # local cache and reused for every card; the network refresh happens in
        # the background (_check_db_update).
        self._report_data = report_stats.load()
        if self.db_err:
            self.subtitle.set_text(self.db_err)
            self._empty(f"Cannot load fix database: {self.db_err}")
            return

        root = steam_scanner.find_steam_root()
        if not root:
            self.subtitle.set_text("Steam not found")
            self._empty("No Steam installation found. Only Steam games are "
                        "supported for now.")
            return

        # Only games actually installed (appmanifest present) — a fix for a game
        # the user doesn't have installed is just noise. Steam's own tooling
        # (Proton, runtimes, redistributables) is filtered out.
        installed = steam_scanner.installed_appids(root)
        self.steam_root = root  # for local cover art in _build_game_card
        gpu, session, cpu = self._gpu_vendor(), self._session(), self._cpu_vendor()
        from topology import load_config
        only_verified = load_config().get("only_verified", False)

        rows = []
        for appid, name in installed.items():
            if self._is_steam_tool(name):
                continue
            game = self.db.get(appid)
            disp_name = (game.name if game else name) or f"App {appid}"
            matching = [i for i in (game.issues if game else [])
                        if i.matches_system(gpu, session, cpu)]
            issues = [i for i in matching if not only_verified or i.fix.verified]
            # Fixes we found but hid because "only verified" is on — so the card
            # can say so honestly instead of a misleading "no known issues".
            hidden = len(matching) - len(issues)
            aliases = game.aliases if game else []
            rows.append((appid, disp_name, issues, game is not None, hidden, aliases))

        # Games with fixes first, then alphabetical.
        rows.sort(key=lambda r: (not r[2], r[1].lower()))

        with_fixes = sum(1 for r in rows if r[2])
        self.subtitle.set_text(
            f"{len(rows)} installed games · {with_fixes} with known fixes · "
            f"{len(self.db)} games in database")

        if not rows:
            self._empty("No installed Steam games found. Install a game, then "
                        "rescan.")
            return

        for appid, name, issues, in_db, hidden, aliases in rows:
            card, reveal = self._build_game_card(appid, name, issues, in_db, hidden)
            self.list_box.append(card)
            # Full-text search key: name + aliases + every visible fix's text, so
            # typing e.g. "multiplayer" surfaces games whose fix mentions it.
            parts = [name, " ".join(aliases)]
            for i in issues:
                parts.append(f"{i.symptom} {i.cause} {i.fix.value} "
                             f"{i.fix.type} {getattr(i.fix, 'content', '')}")
            self.game_cards.append((" ".join(parts).lower(), card, reveal))
        self._apply_search()

    def _empty(self, text):
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("page-subtitle")
        lbl.set_wrap(True)
        lbl.set_xalign(0)
        lbl.set_margin_top(20)
        self.list_box.append(lbl)

    def _on_search(self, entry):
        self._search_text = entry.get_text().strip().lower()
        self._apply_search()

    def _apply_search(self):
        """Filter the card list by the full-text key. A non-empty query hides
        non-matches and auto-expands matches so the reason for the match (the fix
        text) is visible; clearing the box shows everything collapsed again."""
        q = getattr(self, "_search_text", "")
        for key, card, reveal in getattr(self, "game_cards", []):
            match = q in key
            card.set_visible(match or not q)
            if reveal:
                reveal(bool(q) and match)

    TILE_PALETTE = [
        ("#e0af68", "rgba(224,175,104,0.12)"), ("#7aa2f7", "rgba(122,162,247,0.12)"),
        ("#9ece6a", "rgba(158,206,106,0.12)"), ("#bb9af7", "rgba(187,154,247,0.12)"),
        ("#7dcfff", "rgba(125,207,255,0.12)"),
    ]

    @staticmethod
    def _initials(name):
        words = [w for w in name.split() if w]
        if len(words) >= 2:
            return (words[0][0] + words[1][0]).upper()
        return name[:2].upper()

    def _build_game_card(self, appid, name, issues, in_db, hidden=0):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("v2-card")
        if issues:
            card.add_css_class("v2-card-fix")

        # Title row: banner art (or initials fallback) + name/meta + status + tier
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=13)

        art = steam_scanner.library_art(getattr(self, "steam_root", None), appid)
        if art.get("header"):
            # Steam's own header banner, clipped to a rounded box via CSS.
            banner = Gtk.Box()
            banner.set_size_request(180, 84)
            banner.set_valign(Gtk.Align.CENTER)
            banner.add_css_class("game-banner")
            css = Gtk.CssProvider()
            load_css(css, f'.gb-{appid} {{ background-image: url("file://{art["header"]}"); }}')
            banner.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            banner.add_css_class(f"gb-{appid}")
            title_row.append(banner)
        else:
            fg, bg = self.TILE_PALETTE[appid % len(self.TILE_PALETTE)]
            tile = Gtk.Label(label=self._initials(name))
            tile.add_css_class("game-tile")
            tile.set_size_request(44, 44)
            tile.set_valign(Gtk.Align.CENTER)
            css = Gtk.CssProvider()
            load_css(css, f".gt-{appid} {{ background: {bg}; color: {fg}; }}")
            tile.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            tile.add_css_class(f"gt-{appid}")
            title_row.append(tile)

        namecol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        namecol.set_valign(Gtk.Align.CENTER)
        nm = Gtk.Label(label=name); nm.add_css_class("game-title"); nm.set_xalign(0)
        nm.set_wrap(False); nm.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        namecol.append(nm)
        if issues:
            meta_txt = f"{len(issues)} fix{'es' if len(issues) != 1 else ''} available"
        elif hidden:
            meta_txt = (f"{hidden} untested fix{'es' if hidden != 1 else ''} hidden "
                        "— turn off 'verified only' in Settings to see them")
        else:
            meta_txt = "no known issues"
        meta = Gtk.Label(label=meta_txt)
        meta.add_css_class("game-meta"); meta.set_xalign(0)
        meta.set_tooltip_text(f"appid {appid}")  # kept for contributors, out of the way
        namecol.append(meta)
        title_row.append(namecol)

        spacer = Gtk.Box(); spacer.set_hexpand(True); title_row.append(spacer)

        # Launch straight from the app, so applying a fix and testing it is a
        # one-click loop instead of alt-tabbing to Steam. Steam honours the
        # launch options we set, so this really does exercise the fix.
        play = Gtk.Button(label="▶ Play"); play.add_css_class("btn-play")
        play.set_valign(Gtk.Align.CENTER)
        play.set_size_request(84, -1)   # fixed column so Play/pill/tier line up
        play.set_tooltip_text("Launch this game through Steam")
        play.connect("clicked", self._on_play, appid)
        title_row.append(play)

        # Fix-status pill — the actionable signal, so it carries the colour.
        pill = Gtk.Label(); pill.set_valign(Gtk.Align.CENTER)
        if issues:
            pill.set_label(f"● {len(issues)} fix{'es' if len(issues) != 1 else ''}")
            pill.add_css_class("fix-pill")
        elif hidden:
            pill.set_label(f"○ {hidden} untested")
            pill.add_css_class("warn-pill")
        else:
            pill.set_label("✓ all good")
            pill.add_css_class("ok-pill")
        pill.set_size_request(94, -1); pill.set_xalign(0.5)   # fixed column
        title_row.append(pill)

        tier_lbl = Gtk.Label(label=""); tier_lbl.add_css_class("game-tier")
        tier_lbl.set_valign(Gtk.Align.CENTER)
        tier_lbl.set_size_request(86, -1); tier_lbl.set_xalign(0.5)  # reserved column
        title_row.append(tier_lbl)

        # Collapsed by default — a long library is unreadable if every card shows
        # all its fixes at once. The header row toggles a revealer with the fixes.
        reveal_fn = None
        if issues:
            chevron = Gtk.Label(label="▸"); chevron.add_css_class("game-chevron")
            chevron.set_valign(Gtk.Align.CENTER)
            title_row.append(chevron)
            card.append(title_row)

            revealer = Gtk.Revealer()
            revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            body.set_margin_top(4)
            for issue in issues:
                body.append(self._build_issue_row(appid, name, issue))
            revealer.set_child(body)
            card.append(revealer)

            def _set_expanded(show, _rev=revealer, _chev=chevron):
                _rev.set_reveal_child(show)
                _chev.set_label("▾" if show else "▸")
            reveal_fn = _set_expanded

            gesture = Gtk.GestureClick()
            gesture.connect("released", lambda *_: _set_expanded(not revealer.get_reveal_child()))
            title_row.add_controller(gesture)
            try:
                title_row.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
            except Exception:
                pass
        else:
            card.append(title_row)

        self._load_tier_async(appid, tier_lbl)
        return card, reveal_fn

    def _on_play(self, btn, appid):
        """Start the game via Steam's URL handler. Steam applies the launch
        options we've set, so this is the quickest way to check a fix worked."""
        launched = self._open_url(f"steam://rungameid/{appid}")
        btn.set_sensitive(False)
        btn.set_label("Launching…" if launched else "Steam?")
        def restore():
            btn.set_sensitive(True); btn.set_label("▶ Play")
            return False
        GLib.timeout_add(4000, restore)

    @staticmethod
    def _open_url(url):
        """Open a URI with the desktop's default handler, working across GTK
        versions (Gtk.UriLauncher is 4.10+, absent on the AppImage's 4.6). Tries
        the GLib route first, then xdg-open. Returns True if something took it."""
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
            return True
        except Exception:
            pass
        try:
            subprocess.Popen(["xdg-open", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    # ---------- community verification: vote + share report ----------
    @staticmethod
    def _cpu_model():
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return None

    @staticmethod
    def _distro_name():
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return None

    def _system_context(self):
        """A one-line hardware fingerprint. This is the whole point of a report:
        a fix that works on NVIDIA+Wayland+AMD may be irrelevant elsewhere, so a
        result is only meaningful next to the system it was tested on."""
        gpu = self.win.gpu.name or self._gpu_vendor() or "unknown GPU"
        cpu = self._cpu_model() or (self._cpu_vendor() or "unknown CPU")
        session = {"wayland": "Wayland", "x11": "X11"}.get(self._session(), "unknown session")
        parts = [f"GPU: {gpu}", f"CPU: {cpu}", session]
        distro = self._distro_name()
        if distro:
            parts.append(distro)
        return " · ".join(parts)

    @staticmethod
    def _fix_summary(fix):
        if fix.type == "launch_option":
            return f"launch option: {fix.value}"
        if fix.type == "file":
            return f"file: {fix.path}"
        if fix.type == "tool_action":
            return f"action: {fix.action}"
        return fix.value

    def _fix_report_text(self, appid, name, issue, worked):
        """Human-readable report — used verbatim both as the GitHub issue body
        and as the clipboard text, so the two channels carry identical info."""
        lines = [
            f"Game: {name} (appid {appid})",
            f"Issue: {issue.symptom}",
            f"Fix: {self._fix_summary(issue.fix)}",
            f"Result: {'✅ Worked' if worked else '❌ Did NOT work'}",
            f"System: {self._system_context()}",
            f"App: Gaming Command Center v{self.win.APP_VERSION}",
        ]
        if issue.fix.source:
            lines.append(f"Fix source: {issue.fix.source}")
        return "\n".join(lines)

    def _github_issue_url(self, appid, name, issue, worked):
        from urllib.parse import quote
        title = f"Fix report: {name} — {'worked' if worked else 'did not work'}"
        body = (self._fix_report_text(appid, name, issue, worked)
                + "\n\n<!-- Anything to add? Extra detail helps. Thanks for testing! -->")
        return (f"{self.win.GITHUB_URL}/issues/new?labels=fix-report"
                f"&title={quote(title)}&body={quote(body)}")

    def _google_form_url(self, appid, name, issue, worked):
        """Pre-filled Google Form URL — the no-account channel. Returns None
        until the Form is configured (FORM_PREFILL_URL + FORM_FIELDS), so callers
        fall back to Copy report meanwhile."""
        if not self.FORM_PREFILL_URL or not self.FORM_FIELDS:
            return None
        from urllib.parse import urlencode
        vals = {
            "game": name,
            "appid": str(appid),
            "fix": self._fix_summary(issue.fix),
            "result": "Worked" if worked else "Didn't work",
            "gpu": self.win.gpu.name or self._gpu_vendor() or "",
            "cpu": self._cpu_model() or self._cpu_vendor() or "",
            "session": {"wayland": "Wayland", "x11": "X11"}.get(self._session(), ""),
            "distro": self._distro_name() or "",
            "appversion": self.win.APP_VERSION,
        }
        params = {entry: vals[k] for k, entry in self.FORM_FIELDS.items() if k in vals}
        sep = "&" if "?" in self.FORM_PREFILL_URL else "?"
        return f"{self.FORM_PREFILL_URL}{sep}{urlencode(params)}"

    def _votes_path(self):
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        return os.path.join(base, "gaming-command-center", "fix_reports.json")

    @staticmethod
    def _vote_key(appid, issue):
        return f"{appid}:{issue.fix.type}:{issue.symptom}"

    def _load_votes(self):
        try:
            import json
            with open(self._votes_path()) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _recorded_vote(self, appid, issue):
        """The user's own past vote for this fix (True/False), or None."""
        v = self._load_votes().get(self._vote_key(appid, issue))
        return v.get("worked") if isinstance(v, dict) else None

    def _record_vote(self, appid, issue, worked):
        import json
        votes = self._load_votes()
        votes[self._vote_key(appid, issue)] = {"worked": bool(worked), "ts": int(time.time())}
        try:
            path = self._votes_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(votes, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def _build_feedback_row(self, appid, name, issue):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        box.add_css_class("fix-feedback")
        box.set_margin_top(3)
        prompt = Gtk.Label(); prompt.add_css_class("feedback-q"); prompt.set_xalign(0)
        prompt.set_valign(Gtk.Align.CENTER)
        prev = self._recorded_vote(appid, issue)
        prompt.set_label("Did this fix work for you?" if prev is None else
                         ("You reported: 👍 worked" if prev else "You reported: 👎 didn't"))
        box.append(prompt)
        up = Gtk.Button(label="👍"); up.add_css_class("vote-btn")
        up.set_tooltip_text("This fix worked for me — share the result")
        up.connect("clicked", self._on_fix_feedback, appid, name, issue, True, prompt)
        down = Gtk.Button(label="👎"); down.add_css_class("vote-btn")
        down.set_tooltip_text("This fix didn't work for me — share the result")
        down.connect("clicked", self._on_fix_feedback, appid, name, issue, False, prompt)
        box.append(up); box.append(down)

        # Live community tally next to the buttons, when reports exist.
        counts = report_stats.counts_for(
            getattr(self, "_report_data", {}), appid, self._fix_summary(issue.fix))
        if counts:
            tally = Gtk.Label(label=f"· 👍 {counts['worked']}  👎 {counts['failed']}")
            tally.add_css_class("feedback-q"); tally.set_valign(Gtk.Align.CENTER)
            tally.set_tooltip_text("Community reports (updates automatically)")
            box.append(tally)
        return box

    def _on_fix_feedback(self, btn, appid, name, issue, worked, prompt):
        self._record_vote(appid, issue, worked)
        prompt.set_label("Thanks! You reported: " +
                         ("👍 worked" if worked else "👎 didn't"))
        self._share_report_dialog(appid, name, issue, worked)

    def _share_report_dialog(self, appid, name, issue, worked):
        """Offer both channels: a one-click pre-filled GitHub issue for users who
        have an account, and 'Copy report' for everyone else — no account needed,
        paste it into Discord/email/wherever. Same text either way."""
        report = self._fix_report_text(appid, name, issue, worked)
        gh_url = self._github_issue_url(appid, name, issue, worked)

        win = Adw.Window(modal=True, transient_for=self.win)
        win.set_title("Share your result")
        win.set_default_size(500, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar(); header.add_css_class("flat")
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        body.set_margin_start(28); body.set_margin_end(28)
        body.set_margin_top(6); body.set_margin_bottom(24)
        root.append(body)

        title = Gtk.Label()
        title.set_markup("<span size='15000' weight='bold' color='#c0caf5'>Thanks for testing! 🙌</span>")
        title.set_xalign(0)
        body.append(title)

        form_url = self._google_form_url(appid, name, issue, worked)
        sub = Gtk.Label()
        if form_url:
            sub.set_markup("<span color='#a9b1d6'>Sharing your result makes the database "
                           "better for everyone. <b>No account needed</b> — hit "
                           "<b>Send report</b> and Submit. Prefer GitHub, or want to "
                           "paste it somewhere yourself? Those work too.</span>")
        else:
            sub.set_markup("<span color='#a9b1d6'>Sharing your result makes the database "
                           "better for everyone. No GitHub account? Just copy the report "
                           "and paste it anywhere — our community, an email, wherever.</span>")
        sub.set_wrap(True); sub.set_xalign(0)
        body.append(sub)

        rep = Gtk.Label(label=report)
        rep.set_wrap(True); rep.set_xalign(0); rep.set_selectable(True)
        rep.add_css_class("report-box")
        body.append(rep)

        status = Gtk.Label(); status.set_xalign(0); status.set_wrap(True)
        body.append(status)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btns.set_halign(Gtk.Align.END); btns.set_margin_top(4)
        close_btn = Gtk.Button(label="Close"); close_btn.add_css_class("flat")
        copy_btn = Gtk.Button(label="Copy report"); copy_btn.add_css_class("btn-apply-sm")
        gh_btn = Gtk.Button(label="Report on GitHub"); gh_btn.add_css_class("btn-apply-sm")

        def on_copy(_b):
            self._copy(report)
            status.set_markup("<span color='#9ece6a'>Copied — paste it wherever you like.</span>")
        def on_gh(_b):
            self._open_url(gh_url)
            status.set_markup("<span color='#9ece6a'>Opening GitHub in your browser…</span>")
        copy_btn.connect("clicked", on_copy)
        gh_btn.connect("clicked", on_gh)
        close_btn.connect("clicked", lambda *_: win.close())
        btns.append(close_btn); btns.append(copy_btn); btns.append(gh_btn)

        if form_url:
            # The account-free primary path: pre-filled form, just Submit.
            send_btn = Gtk.Button(label="Send report"); send_btn.add_css_class("btn-game-on")
            send_btn.set_tooltip_text("Opens a pre-filled form — no account needed, just hit Submit")
            def on_send(_b):
                self._open_url(form_url)
                status.set_markup("<span color='#9ece6a'>Opening the report form — "
                                  "just hit Submit. Thank you!</span>")
            send_btn.connect("clicked", on_send)
            btns.append(send_btn)
        else:
            gh_btn.remove_css_class("btn-apply-sm"); gh_btn.add_css_class("btn-game-on")
        body.append(btns)

        win.set_content(root)
        win.present()

    def _build_issue_row(self, appid, name, issue):
        """v2 issue: text column (symptom + badge, cause, fix) left, action right."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("issue-row")

        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        col.set_hexpand(True)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sym = Gtk.Label(label=issue.symptom); sym.add_css_class("issue-symptom")
        sym.set_xalign(0); sym.set_wrap(True)
        top.append(sym)
        # Trust tier: the green VERIFIED star is a maintainer decision; the blue
        # COMMUNITY badge is data-driven (grows on its own from reports); UNTESTED
        # is the honest default. counts come from the cached community tally.
        counts = report_stats.counts_for(
            getattr(self, "_report_data", {}), appid, self._fix_summary(issue.fix))
        badge = Gtk.Label(); badge.set_valign(Gtk.Align.CENTER)
        if issue.fix.verified:
            badge.set_label("✓ VERIFIED"); badge.add_css_class("badge-verified")
        elif report_stats.is_community_confirmed(counts):
            badge.set_label(f"✓ COMMUNITY ({counts['worked']})")
            badge.add_css_class("badge-community")
        else:
            badge.set_label("UNTESTED"); badge.add_css_class("badge-untested")
        top.append(badge)
        col.append(top)

        if issue.cause:
            cause = Gtk.Label(label=issue.cause); cause.add_css_class("issue-cause")
            cause.set_xalign(0); cause.set_wrap(True)
            col.append(cause)

        fix = issue.fix
        if fix.type == "launch_option":
            detail = Gtk.Label(label=f"launch option: {fix.value}")
        elif fix.type == "file":
            detail = Gtk.Label(label=f"writes {fix.path}")
        elif fix.type == "tool_action":
            detail = Gtk.Label(label=f"action: {fix.action}")
        else:
            detail = Gtk.Label(label=fix.value)
        detail.add_css_class("issue-info"); detail.set_xalign(0); detail.set_wrap(True)
        detail.set_selectable(True)
        col.append(detail)

        # Community verification — the single strongest signal for the database.
        # Anyone can vote (no account needed); sharing the result afterwards is a
        # second, optional step that offers both a GitHub issue and a plain copy.
        col.append(self._build_feedback_row(appid, name, issue))
        row.append(col)

        # Action column
        if fix.is_applicable:
            act = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            act.set_valign(Gtk.Align.CENTER)
            if self._fix_applied(appid, fix):
                # Already in place — show it, don't offer a pointless Apply.
                done = Gtk.Label()
                done.set_markup("<span color='#9ece6a' weight='bold'>✓ Applied</span>")
                done.add_css_class("issue-result"); done.set_xalign(1)
                act.append(done)
            else:
                btn = Gtk.Button(); btn.add_css_class("btn-apply-sm")
                btn.set_label({"launch_option": "APPLY FIX", "file": "CREATE FILE",
                               "tool_action": "APPLY FIX"}.get(fix.type, "APPLY"))
                btn.connect("clicked", self.on_apply_fix, appid, issue)
                act.append(btn)
                result = Gtk.Label(label=""); result.add_css_class("issue-result")
                result.set_xalign(1); result.set_wrap(True)
                act.append(result)
                btn._result = result
            row.append(act)

        return row

    @staticmethod
    def _fix_applied(appid, fix):
        """True if the fix already looks applied, so the card shows '✓ Applied'
        instead of a button the user would click for nothing. Uses a substring
        match, since a launch option often sits inside a larger string (e.g. the
        fix's env var alongside a gamescope wrapper)."""
        if fix.type == "launch_option":
            token = fix.value.replace("%command%", "").strip()
            return bool(token) and token in (steam_scanner.get_launch_options(appid) or "")
        if fix.type == "file":
            want = fix.content.strip()
            if not want:
                return False
            try:
                with open(os.path.expanduser(fix.path)) as f:
                    return want in f.read()
            except OSError:
                return False
        return False

    def on_apply_fix(self, btn, appid, issue):
        fix = issue.fix
        btn.set_sensitive(False)
        btn.set_label("Applying...")
        result = btn._result

        def work():
            ok, msg = self._apply(appid, fix)

            def done():
                btn.set_sensitive(True)
                btn.set_label("Applied" if ok else "Retry")
                color = "#9ece6a" if ok else "#f7768e"
                result.set_markup(
                    f"<span color='{color}'>{GLib.markup_escape_text(msg)}</span>")
                if fix.type == "launch_option" and not ok and "running" in msg.lower():
                    self._copy(fix.value)
                    result.set_markup(
                        "<span color='#e0af68'>Steam is open — copied the option "
                        "to your clipboard. Paste it into the game's Launch "
                        "Options, or close Steam and click again.</span>")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, appid, fix):
        if fix.type == "launch_option":
            # MERGE into existing options, never clobber them — a user's launch
            # line (gamescope, game-performance, other env vars) is often exactly
            # what makes their game run well. Prepend the fix's prefix (the part
            # before %command%, e.g. an env var) unless it's already there.
            existing = (steam_scanner.get_launch_options(appid) or "").strip()
            prefix = fix.value.replace("%command%", "").strip()
            if not existing:
                value = fix.value
            elif prefix and prefix in existing:
                value = existing            # already present — leave it untouched
            else:
                value = f"{prefix} {existing}".strip()
            return steam_scanner.set_launch_options(appid, value)
        if fix.type == "file":
            path = os.path.expanduser(fix.path)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(fix.content)
            except OSError as e:
                return False, f"Could not write {fix.path}: {e}"
            return True, f"Wrote {fix.path}"
        if fix.type == "tool_action" and fix.action == "game_mode":
            if not self.win.topo.complete:
                return False, "CPU layout unknown — restore all cores first"
            keep = self.win.topo.keep_ccd()
            plan = self.win.topo.park_plan(keep)
            if not plan:
                return False, "This CPU has only one CCD — Game Mode n/a"
            ok, err = CCDController.park(plan)
            return (True, f"Game Mode on — kept CCD{keep}") if ok else (False, err)
        return False, "Unsupported fix"

    def _copy(self, text):
        try:
            self.win.get_clipboard().set(text)
        except Exception:
            pass

    def _load_tier_async(self, appid, label):
        def work():
            tier, total = steam_scanner.protondb_tier(appid)
            if not tier:
                return
            colors = {"platinum": ("#c0caf5", "rgba(192,202,245,0.14)"),
                      "gold": ("#e0af68", "rgba(224,175,104,0.14)"),
                      "silver": ("#9aa5ce", "rgba(154,165,206,0.14)"),
                      "bronze": ("#cd7f32", "rgba(205,127,50,0.14)"),
                      "borked": ("#f7768e", "rgba(247,118,142,0.14)"),
                      "pending": ("#565f89", "rgba(86,95,137,0.14)")}
            fg, bg = colors.get(tier, ("#565f89", "rgba(86,95,137,0.14)"))

            def apply():
                label.set_label(tier.upper())
                label.set_tooltip_text(f"ProtonDB: {tier.capitalize()} · {total} reports")
                css = Gtk.CssProvider()
                load_css(css, f".tier-{appid} {{ background: {bg}; color: {fg}; }}")
                label.get_style_context().add_provider(
                    css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                label.add_css_class(f"tier-{appid}")
                return False
            GLib.idle_add(apply)

        threading.Thread(target=work, daemon=True).start()


# ============================================================
# Main Window
# ============================================================
class CommandCenter(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Gaming Command Center")
        self.set_default_size(1180, 820)
        self.topo = CPUTopology()
        self.gpu = GPUInfo()
        self.benching = False
        self.best_ccd = None
        self._stop_monitor = threading.Event()
        from topology import load_config
        try:
            self._monitor_interval = float(load_config().get("monitor_interval", 1.5))
        except (TypeError, ValueError):
            self._monitor_interval = 1.5

        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        css = """
        /* === v2 design system: typography === */
        * {
            font-family: 'Chakra Petch', sans-serif;
        }
        .mono, .stat-value, .stat-value-green, .stat-value-blue, .stat-value-orange,
        .bench-mhz, .info-card-sub, .thread-freq, .thread-id, .gpu-bar-value,
        .page-subtitle, .status-footer {
            font-family: 'JetBrains Mono', monospace;
        }
        window { background: #13141c; }

        /* === Sidebar === */
        .sidebar { background: #16161e; min-width: 206px; }
        .sidebar-item {
            padding: 10px 12px;
            border-radius: 8px;
            color: #565f89;
        }
        .sidebar-item:hover {
            background: rgba(255,255,255,0.04);
            color: #a9b1d6;
        }
        .sidebar-item-active {
            background: linear-gradient(90deg, rgba(122,162,247,0.16), rgba(122,162,247,0.05));
            color: #7aa2f7;
            border-left: 3px solid #7aa2f7;
        }
        .sidebar-title { font-size: 12px; font-weight: 700; color: #c0caf5; letter-spacing: 1.5px; }
        .sidebar-subtitle { font-size: 10px; color: #565f89; }
        .sidebar-footer { font-size: 9px; color: #565f89; }
        .sidebar-item { padding: 9px 12px; border-radius: 9px; color: #565f89; font-weight: 700; font-size: 13px; letter-spacing: 0.4px; border-left: 3px solid transparent; }

        /* Side hero status box */
        .side-hero {
            background: rgba(158,206,106,0.06); border: 1px solid rgba(158,206,106,0.2);
            border-radius: 11px; padding: 12px;
        }
        .side-update {
            background: rgba(224,175,104,0.1); border: 1px solid rgba(224,175,104,0.35);
            border-radius: 11px; padding: 9px 12px;
        }
        .side-update:hover { background: rgba(224,175,104,0.18); }
        .side-hero-gaming {
            background: linear-gradient(135deg, rgba(224,175,104,0.1), rgba(247,118,142,0.1));
            border: 1px solid rgba(224,175,104,0.3);
        }
        .hero-dot { border-radius: 50%; background: #9ece6a; box-shadow: 0 0 8px #9ece6a; }
        .hero-dot-gaming { background: #e0af68; box-shadow: 0 0 8px #e0af68; }
        .hero-mode-gaming { color: #e0af68; }
        .hero-mode { font-size: 10px; letter-spacing: 1.5px; font-weight: 700; color: #9ece6a; }
        .hero-sub { font-size: 10px; color: #a9b1d6; font-family: 'JetBrains Mono', monospace; }
        .status-footer { font-size: 9px; color: #414868; font-family: 'JetBrains Mono', monospace; }
        .logo-img { border-radius: 9px; }

        /* Shared header */
        .header-title { font-size: 19px; font-weight: 700; color: #c0caf5; letter-spacing: 2.5px; }
        .header-sub { font-size: 10px; color: #565f89; font-family: 'JetBrains Mono', monospace; }

        /* Game Mode toggle pill */
        .gm-toggle {
            border-radius: 12px; padding: 9px 14px;
            background: rgba(158,206,106,0.06); border: 1px solid rgba(158,206,106,0.2);
        }
        .gm-toggle-on {
            background: linear-gradient(135deg, rgba(224,175,104,0.1), rgba(247,118,142,0.1));
            border: 1px solid rgba(224,175,104,0.3);
        }
        .gm-toggle-label { font-weight: 700; font-size: 13px; letter-spacing: 1.5px; color: #9ece6a; }
        .gm-toggle-label-on { color: #e0af68; }
        .gm-pill { border-radius: 13px; background: #2a2b3d; padding: 3px; }
        .gm-pill-on { background: #e0af68; }
        .gm-knob { border-radius: 50%; background: #13141c; margin-left: 0px; }
        .gm-knob-on { margin-left: 22px; }

        /* === Page headers === */
        .page-title {
            font-size: 16px;
            font-weight: bold;
            color: #c0caf5;
            letter-spacing: 2px;
        }
        .page-subtitle { font-size: 10px; color: #565f89; }

        /* === Stat tiles === */
        .stat-tile {
            background: #16161e;
            border-radius: 10px;
            padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .stat-value { font-size: 22px; font-weight: 800; color: #c0caf5; }
        .stat-value-green { font-size: 22px; font-weight: 800; color: #9ece6a; }
        .stat-value-blue { font-size: 22px; font-weight: 800; color: #7aa2f7; }
        .stat-value-orange { font-size: 22px; font-weight: 800; color: #e0af68; }
        .stat-label { font-size: 10px; color: #565f89; text-transform: uppercase; letter-spacing: 1px; }

        /* === CCD cards === */
        .ccd-card {
            background: #16161e;
            border-radius: 14px;
            padding: 14px 16px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .ccd-card-active {
            background: rgba(158,206,106,0.06);
            border: 1px solid rgba(158,206,106,0.15);
        }
        .ccd-card-parked {
            background: rgba(247,118,142,0.05);
            border: 1px solid rgba(247,118,142,0.12);
        }
        .ccd-card-best {
            border: 1px solid rgba(158,206,106,0.35);
        }
        .ccd-title { font-size: 15px; font-weight: 700; color: #c0caf5; }
        .ccd-badge {
            font-size: 10px; font-weight: 700;
            padding: 2px 8px; border-radius: 6px;
        }
        .badge-gaming { background: rgba(224,175,104,0.15); color: #e0af68; }
        .badge-parked { background: rgba(247,118,142,0.12); color: #f7768e; }
        .badge-active { background: rgba(158,206,106,0.12); color: #9ece6a; }
        .badge-best { background: rgba(158,206,106,0.18); color: #9ece6a; }

        .core-dot { border-radius: 50%; min-width: 12px; min-height: 12px; }
        .core-on { background: #9ece6a; }
        .core-off { background: #414868; }
        .core-dot-boost { background: #e0af68; }

        .section-header {
            font-size: 13px; font-weight: 700; color: #7aa2f7;
            text-transform: uppercase; letter-spacing: 1.5px;
            margin-top: 14px; margin-bottom: 2px;
        }

        /* === GPU card === */
        .gpu-card {
            background: #1f2335;
            border-radius: 14px;
            padding: 16px;
            border: 1px solid rgba(122,162,247,0.08);
        }

        progressbar trough { background: #1a1b26; border-radius: 4px; min-height: 6px; }
        progressbar progress { background: #7aa2f7; border-radius: 4px; }

        /* === Buttons === */
        .btn-game-on {
            background: linear-gradient(135deg, #e0af68, #f7768e);
            color: #1a1b26; font-weight: 700;
            border-radius: 10px; padding: 10px;
        }
        .btn-game-off {
            background: linear-gradient(135deg, #9ece6a, #73daca);
            color: #1a1b26; font-weight: 700;
            border-radius: 10px; padding: 10px;
        }
        .btn-apply {
            background: #7aa2f7; color: #1a1b26; font-weight: 700;
            border-radius: 10px; padding: 8px;
        }
        .btn-bench {
            background: #2a2b3d; color: #c0caf5;
            border-radius: 10px; padding: 8px;
            border: 1px solid rgba(255,255,255,0.06);
        }

        /* === Benchmark === */
        .bench-group {
            background: #16161e; border-radius: 14px; padding: 16px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .bench-ccd-name { color: #c0caf5; font-weight: 700; font-size: 14px; }
        .bench-ccd-avg { color: #c0caf5; }
        .bench-badge { color: #1a1b26; }
        .bench-badge-best {
            background: #e0af68; color: #1a1b26; font-weight: bold;
            border-radius: 6px; padding: 1px 8px; font-size: 11px;
        }
        .bench-cpu-label { color: #565f89; }
        .bench-cpu-active { color: #7aa2f7; font-weight: bold; }
        .bench-mhz { color: #c0caf5; font-family: monospace; }
        levelbar.bench-bar trough {
            background: #14151f; border-radius: 5px; min-height: 14px;
            border: 1px solid rgba(255,255,255,0.04);
        }
        levelbar.bench-bar block.filled {
            background: linear-gradient(to right, #7aa2f7, #9ece6a);
            border-radius: 5px; min-height: 14px;
        }

        /* === Games === */
        .game-card {
            background: #1e1f2e; border-radius: 12px; padding: 14px 16px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .game-title { color: #c0caf5; font-weight: 700; font-size: 15px; }
        .game-meta { color: #565f89; font-size: 10px; font-family: 'JetBrains Mono', monospace; }
        .game-tile {
            border-radius: 10px; font-weight: 700; font-size: 17px; letter-spacing: 1px;
        }
        .game-tier { font-size: 9px; font-weight: 700; letter-spacing: 1px;
            padding: 3px 9px; border-radius: 5px; }
        .game-banner {
            border-radius: 9px; background-size: cover; background-position: center;
            border: 1px solid rgba(255,255,255,0.08);
        }
        .fix-pill {
            background: rgba(122,162,247,0.14); color: #7aa2f7;
            border: 1px solid rgba(122,162,247,0.28);
            border-radius: 20px; padding: 3px 12px; font-size: 11px; font-weight: 700;
        }
        .ok-pill { color: #9ece6a; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }
        .warn-pill { color: #e0af68; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }
        .game-chevron { color: #7f849c; font-size: 13px; padding: 0 2px 0 6px; }
        .v2-card-fix {
            border-color: rgba(122,162,247,0.55); background: #191b29;
            box-shadow: inset 3px 0 0 0 #7aa2f7, 0 4px 20px rgba(122,162,247,0.12);
        }
        .issue-row {
            background: #12131b; border-radius: 10px; padding: 11px 14px;
            margin-top: 2px; border: 1px solid rgba(255,255,255,0.04);
        }
        .issue-symptom { color: #e0af68; font-weight: 700; font-size: 12px; }
        .issue-cause { color: #565f89; font-size: 11px; }
        .btn-apply-sm {
            background: rgba(122,162,247,0.1); border: 1px solid rgba(122,162,247,0.2);
            color: #7aa2f7; font-weight: 700; font-size: 10px; border-radius: 8px; padding: 7px 14px;
        }
        .btn-apply-sm:hover { background: rgba(122,162,247,0.18); }
        .btn-play {
            background: rgba(158,206,106,0.12); border: 1px solid rgba(158,206,106,0.28);
            color: #9ece6a; font-weight: 700; font-size: 11px; border-radius: 8px; padding: 5px 13px;
        }
        .btn-play:hover { background: rgba(158,206,106,0.22); }
        .btn-play:disabled { color: #565f89; border-color: rgba(255,255,255,0.06); background: transparent; }
        .fix-feedback { margin-top: 2px; }
        .feedback-q { color: #565f89; font-size: 11px; }
        .vote-btn {
            background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 7px; padding: 1px 8px; font-size: 12px; min-height: 0;
        }
        .vote-btn:hover { background: rgba(255,255,255,0.10); }
        .report-box {
            background: #16171f; border: 1px solid rgba(255,255,255,0.07);
            border-radius: 8px; padding: 11px 13px;
            color: #9aa5ce; font-family: monospace; font-size: 11px;
        }
        .badge-verified {
            background: rgba(158,206,106,0.15); color: #9ece6a;
            border-radius: 6px; padding: 1px 8px; font-size: 11px; font-weight: bold;
        }
        .badge-community {
            background: rgba(122,162,247,0.15); color: #7aa2f7;
            border-radius: 6px; padding: 1px 8px; font-size: 11px; font-weight: bold;
        }
        .badge-untested {
            background: rgba(224,175,104,0.12); color: #e0af68;
            border-radius: 6px; padding: 1px 8px; font-size: 11px;
        }
        .issue-info {
            color: #9aa5ce; font-family: monospace; font-size: 12px;
            margin-top: 4px;
        }
        .issue-result { font-size: 12px; }

        /* === Empty states (System Doctor, Benchmark) === */
        .doctor-chip, .empty-chip {
            background: #1e1f2e; color: #9aa5ce;
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 20px; padding: 2px 12px; font-size: 12px;
        }
        .doctor-group-label { color: #565f89; font-size: 12px; font-weight: bold; }
        .empty-headline { color: #c0caf5; }
        .empty-card {
            background: #1e1f2e; border-radius: 12px; padding: 12px 16px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .info-card {
            background: #1e1f2e; border-radius: 10px; padding: 8px 12px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .info-card:hover { background: #24263a; border-color: rgba(122,162,247,0.3); }
        .info-card-title { color: #c0caf5; font-weight: bold; font-size: 13px; }
        .info-card-sub { color: #565f89; font-size: 12px; }

        /* ===== v2 dashboard ===== */
        .v2-card {
            background: #16161e; border-radius: 14px; padding: 16px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .card-title { font-size: 11px; font-weight: 700; color: #7aa2f7; letter-spacing: 2px; }
        .card-meta { font-size: 10px; color: #a9b1d6; font-family: 'JetBrains Mono', monospace; }

        /* Stat tiles */
        .v2-tile {
            background: #16161e; border-radius: 12px; padding: 12px 14px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .tile-value { font-size: 21px; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: #c0caf5; }
        .tile-unit { font-size: 11px; color: #565f89; font-family: 'JetBrains Mono', monospace; }
        .tile-label { font-size: 9px; color: #565f89; letter-spacing: 1.2px; font-weight: 600; }
        .c-cputemp { color: #9ece6a; } .c-cpuclock { color: #7aa2f7; }
        .c-ram { color: #bb9af7; } .c-gputemp { color: #e0af68; } .c-gpupower { color: #bb9af7; }
        levelbar.tile-bar trough { background: #1a1b26; border-radius: 2px; min-height: 4px; }
        levelbar.tile-bar block.filled { border-radius: 2px; min-height: 4px; }
        levelbar.lb-cputemp block.filled { background: #9ece6a; }
        levelbar.lb-cpuclock block.filled { background: #7aa2f7; }
        levelbar.lb-ram block.filled { background: #bb9af7; }
        levelbar.lb-gputemp block.filled { background: #e0af68; }
        levelbar.lb-gpupower block.filled { background: #bb9af7; }

        /* CCD topology card */
        .ccd-spec { font-size: 10px; color: #565f89; font-family: 'JetBrains Mono', monospace; }
        .ccd-avg { font-size: 11px; color: #9ece6a; font-family: 'JetBrains Mono', monospace; font-weight: 700; }
        .thread-cell {
            border-radius: 7px; background: rgba(255,255,255,0.025);
            border: 1px solid rgba(255,255,255,0.05); padding: 6px 2px;
        }
        .thread-boost {
            background: rgba(224,175,104,0.08); border: 1px solid rgba(224,175,104,0.3);
        }
        .thread-off {
            background: rgba(247,118,142,0.03); border: 1px solid rgba(247,118,142,0.1);
        }
        .thread-id { font-size: 8px; color: #565f89; font-family: 'JetBrains Mono', monospace; }
        .thread-freq { font-size: 11px; font-weight: 700; color: #c0caf5; font-family: 'JetBrains Mono', monospace; }
        .thread-freq-boost { color: #e0af68; }
        .thread-freq-off { color: #414868; }
        .ccd-card { border-radius: 12px; padding: 12px 14px; }
        .ccd-card-active { border: 1px solid rgba(158,206,106,0.25); background: rgba(158,206,106,0.04); }
        .ccd-card-best { border: 1px solid rgba(158,206,106,0.35); background: rgba(158,206,106,0.06); }
        .ccd-card-parked { border: 1px solid rgba(247,118,142,0.2); background: rgba(247,118,142,0.03); }

        /* GPU bars */
        .gpu-bar-label { font-size: 9px; color: #565f89; letter-spacing: 1.2px; font-weight: 600; }
        .gpu-bar-value { font-size: 11px; color: #c0caf5; font-family: 'JetBrains Mono', monospace; font-weight: 700; }
        levelbar.gpu-bar trough { background: #1a1b26; border-radius: 3px; min-height: 6px; }
        levelbar.gpu-bar block.filled {
            background: linear-gradient(90deg, #7aa2f7, #7dcfff); border-radius: 3px; min-height: 6px;
        }
        .gpu-pstate {
            font-size: 9px; font-weight: 700; padding: 2px 8px; border-radius: 5px;
            background: rgba(122,162,247,0.12); color: #7aa2f7; letter-spacing: 1px;
        }
        .offset-tile { background: #1a1b26; border-radius: 10px; padding: 10px 12px; border: 1px solid rgba(255,255,255,0.05); }
        .offset-label { font-size: 9px; color: #565f89; letter-spacing: 1px; font-weight: 600; }
        .offset-value { font-size: 15px; color: #e0af68; font-family: 'JetBrains Mono', monospace; font-weight: 700; }
        .btn-quiet { background: rgba(255,255,255,0.03); color: #a9b1d6; border-radius: 9px; padding: 7px; border: 1px solid rgba(255,255,255,0.07); }

        /* System Doctor v2 */
        .doctor-row { border-radius: 10px; padding: 10px 14px; border: 1px solid transparent; }
        .doctor-row-ok { background: rgba(158,206,106,0.03); border-color: rgba(158,206,106,0.08); }
        .doctor-row-warning { background: rgba(224,175,104,0.05); border-color: rgba(224,175,104,0.14); }
        .doctor-row-info { background: rgba(122,162,247,0.03); border-color: rgba(122,162,247,0.08); }
        .doctor-row-critical { background: rgba(247,118,142,0.07); border-color: rgba(247,118,142,0.28); }
        .doctor-dot { border-radius: 50%; }
        .dot-ok { background: #9ece6a; box-shadow: 0 0 6px #9ece6a; }
        .dot-warning { background: #e0af68; box-shadow: 0 0 6px #e0af68; }
        .dot-info { background: #7aa2f7; box-shadow: 0 0 6px #7aa2f7; }
        .dot-critical { background: #f7768e; box-shadow: 0 0 8px #f7768e; }
        .doctor-name { color: #c0caf5; font-weight: 700; font-size: 12px; }
        .doctor-msg { color: #9aa5ce; font-size: 10px; font-family: 'JetBrains Mono', monospace; }
        .btn-fix {
            background: rgba(224,175,104,0.12); border: 1px solid rgba(224,175,104,0.25);
            color: #e0af68; font-weight: 700; font-size: 10px; border-radius: 8px; padding: 6px 14px;
        }
        .btn-fix:hover { background: rgba(224,175,104,0.2); }
        .doctor-summary {
            border-radius: 10px; padding: 9px 14px; font-size: 11px; margin-bottom: 8px;
        }
        .summary-warn { background: rgba(224,175,104,0.08); border: 1px solid rgba(224,175,104,0.18); color: #e0af68; }
        .summary-ok { background: rgba(158,206,106,0.06); border: 1px solid rgba(158,206,106,0.15); color: #9ece6a; }
        .summary-critical { background: rgba(247,118,142,0.1); border: 1px solid rgba(247,118,142,0.3); color: #f7768e; }

        /* Settings grouped cards */
        .settings-group {
            background: #16161e; border-radius: 14px;
            border: 1px solid rgba(255,255,255,0.06);
        }
        .settings-row { padding: 13px 16px; border-bottom: 1px solid rgba(255,255,255,0.04); }
        .settings-row:last-child { border-bottom: none; }
        .settings-name { color: #c0caf5; font-weight: 700; font-size: 13px; }
        .settings-desc { color: #565f89; font-size: 10px; }

        /* === Sliders === */
        scale trough { background: #1a1b26; min-height: 6px; border-radius: 3px; }
        scale highlight { background: #7aa2f7; border-radius: 3px; }
        scale slider {
            background: #7aa2f7; min-width: 16px; min-height: 16px;
            border-radius: 50%; box-shadow: 0 0 8px rgba(122,162,247,0.3);
        }
        scale { margin-top: 4px; margin-bottom: 4px; }

        /* === Dropdown === */
        dropdown {
            background: #1a1b26; border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.06);
        }

        /* === Banners === */
        .banner-gaming {
            background: linear-gradient(90deg, rgba(224,175,104,0.1), rgba(247,118,142,0.1));
            border: 1px solid rgba(224,175,104,0.15); color: #e0af68;
            border-radius: 10px; padding: 8px 14px; font-weight: 600;
        }
        .banner-normal {
            background: rgba(158,206,106,0.06);
            border: 1px solid rgba(158,206,106,0.1); color: #9ece6a;
            border-radius: 10px; padding: 8px 14px; font-weight: 600;
        }

        /* === Benchmark === */
        .bench-progress { margin-top: 6px; }

        /* === Separator === */
        separator { background: rgba(255,255,255,0.04); }

        /* === Wizard / Game Doctor rows === */
        .wizard-row {
            background: #16161e;
            border-radius: 10px;
            padding: 10px 14px;
            border: 1px solid rgba(255,255,255,0.04);
        }
        .wizard-name { font-weight: 700; font-size: 13px; color: #c0caf5; }
        .wizard-msg { font-size: 11px; color: #a9b1d6; }
        .wizard-fix { font-size: 10px; color: #e0af68; }

        .scan-row {
            background: #16161e;
            border-radius: 8px;
            padding: 10px 12px;
            margin: 2px 0;
            border: 1px solid rgba(255,255,255,0.04);
        }
        .scan-row-warning {
            background: rgba(224,175,104,0.04);
            border: 1px solid rgba(224,175,104,0.08);
        }
        .scan-row-ok {
            background: rgba(158,206,106,0.04);
            border: 1px solid rgba(158,206,106,0.08);
        }
        .scan-row-info {
            background: rgba(122,162,247,0.04);
            border: 1px solid rgba(122,162,247,0.08);
        }
        .scan-summary {
            font-size: 13px; font-weight: 700;
            padding: 10px 16px; border-radius: 10px; margin-top: 8px;
        }
        .scan-summary-warn {
            background: rgba(224,175,104,0.1);
            border: 1px solid rgba(224,175,104,0.15);
            color: #e0af68;
        }
        .scan-summary-ok {
            background: rgba(158,206,106,0.1);
            border: 1px solid rgba(158,206,106,0.15);
            color: #9ece6a;
        }
        .scan-summary-info {
            background: rgba(122,162,247,0.1);
            border: 1px solid rgba(122,162,247,0.15);
            color: #7aa2f7;
        }
        """
        # Keep the unscaled stylesheet + a live provider so the Text-size
        # setting can re-scale the whole UI instantly (see _apply_font_scale).
        self._base_css = css
        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_font_scale(float(load_config().get("ui_font_scale", UI_FONT_SCALE)))

        self.build_ui()
        self.refresh()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        self._check_app_update()
        self.connect("close-request", self._on_close)

    def _on_close(self, *_):
        self._stop_monitor.set()
        return False

    # ============================================================
    # UI Construction
    # ============================================================
    def build_ui(self):
        """Build the main window: headerbar + sidebar + content area."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(outer)

        # HeaderBar for window dragging (with sidebar title)
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Gtk.Label(label=""))
        outer.append(header)

        # Main area: sidebar + content
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        outer.append(main_box)

        # Sidebar — fixed width so switching pages never reflows it
        sidebar = self._build_sidebar()
        sidebar.set_hexpand(False)
        main_box.append(sidebar)

        # Vertical separator
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(sep)

        # Content area
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_hexpand(True)

        # Shared header: page title/subtitle (left) + Game Mode toggle (right)
        content_box.append(self._build_shared_header())

        # ViewStack for page switching. Homogeneous width so all pages request
        # the same width — switching never resizes the window or the sidebar.
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        self.view_stack.set_hexpand(True)
        self.view_stack.set_hhomogeneous(True)

        # Dashboard page
        self.view_stack.add_titled(
            self._build_dashboard_page(), "dashboard", "Dashboard")

        # Games page (per-game fixes)
        self.games_page = GamesPage(self)
        self.view_stack.add_titled(self.games_page, "games", "Games")

        # Game Doctor page
        self.game_doctor = GameDoctorPage()
        self.view_stack.add_titled(
            self.game_doctor, "doctor", "System Doctor")

        # Benchmark page
        self.view_stack.add_titled(
            self._build_benchmark_page(), "benchmark", "Benchmark")

        # Settings page
        self.view_stack.add_titled(
            self._build_settings_page(), "settings", "Settings")

        content_box.append(self.view_stack)
        main_box.append(content_box)

        # Default to Dashboard (also sets header title + active nav icon).
        # GCC_START_PAGE lets tooling open straight to a page for screenshots.
        self.switch_page(os.environ.get("GCC_START_PAGE", "dashboard"))

    # Line-icon paths from the v2 design (16x16 viewBox, stroke-based).
    NAV_ICONS = {
        "dashboard": "M2 8.5 8 3l6 5.5M4 7.5V13h8V7.5",
        "games": "M5 6h6a3.5 3.5 0 0 1 0 7c-1.2 0-2-.6-2.4-1.4H7.4C7 12.4 6.2 13 5 13"
                 "a3.5 3.5 0 0 1 0-7ZM5.5 8.5v2M4.5 9.5h2M10.8 9h.01M12.2 10h.01",
        "doctor": "M8 2.5l4.5 2v3.5c0 3-2 5-4.5 5.5-2.5-.5-4.5-2.5-4.5-5.5V4.5L8 2.5ZM8 6v4M6 8h4",
        "benchmark": "M3 13h2V8h2v5h2V5h2v8h2M2 13h12",
        "settings": "M8 5.5A2.5 2.5 0 1 1 8 10.5 2.5 2.5 0 0 1 8 5.5ZM8 1.8v1.7M8 12.5v1.7"
                    "M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2"
                    "M12.4 3.6l-1.2 1.2M4.8 11.2l-1.2 1.2",
    }

    @staticmethod
    def _svg_texture(path_d, color, size=16):
        """Render a stroke-SVG path to a Gdk.Texture, or None if the SVG
        pixbuf loader is unavailable (librsvg missing)."""
        from gi.repository import GdkPixbuf
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
               f'viewBox="0 0 16 16" fill="none" stroke="{color}" stroke-width="1.6" '
               f'stroke-linecap="round" stroke-linejoin="round"><path d="{path_d}"/></svg>')
        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.write(svg.encode())
            loader.close()
            return Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
        except Exception:
            return None

    def _set_nav_icon(self, item, active):
        tex = self._svg_texture(self.NAV_ICONS[item._page],
                                "#7aa2f7" if active else "#565f89")
        if tex is not None:
            item._icon.set_from_paintable(tex)

    # ============================================================
    # Sidebar
    # ============================================================
    def _build_sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        sidebar.add_css_class("sidebar")
        sidebar.set_size_request(206, -1)

        # App logo + two-line name
        logo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        logo_box.set_margin_start(12); logo_box.set_margin_end(12)
        logo_box.set_margin_top(18); logo_box.set_margin_bottom(16)
        # Load from the logo bundled next to the script, not a per-user icon
        # path — the old hardcoded /home/<user>/… path broke for everyone else
        # and was never populated by install.sh anyway.
        logo_path = os.path.join(BASE_DIR, "GCC_logo.png")
        if os.path.exists(logo_path):
            logo_img = Gtk.Image.new_from_file(logo_path)
            logo_img.set_pixel_size(36)
            logo_img.add_css_class("logo-img")
            logo_box.append(logo_img)
        title = Gtk.Label(label="GAMING\nCOMMAND CENTER")
        title.set_halign(Gtk.Align.START)
        title.set_justify(Gtk.Justification.LEFT)
        title.add_css_class("sidebar-title")
        logo_box.append(title)
        sidebar.append(logo_box)

        # Navigation items with icons
        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        nav_box.set_margin_start(10); nav_box.set_margin_end(10)
        self.sidebar_items = {}
        for label, page_name in [
            ("Dashboard", "dashboard"), ("Games", "games"),
            ("System Doctor", "doctor"), ("Benchmark", "benchmark"),
            ("Settings", "settings"),
        ]:
            item = self._make_sidebar_item(label, page_name)
            nav_box.append(item)
            self.sidebar_items[page_name] = item
        sidebar.append(nav_box)

        spacer = Gtk.Box(); spacer.set_vexpand(True); sidebar.append(spacer)

        # Update badge — hidden until the release check finds a newer version;
        # clicking jumps to Settings › About where the update action lives.
        self.sidebar_update_btn = Gtk.Button()
        self.sidebar_update_btn.add_css_class("side-update")
        self.sidebar_update_btn.set_visible(False)
        self.sidebar_update_btn.set_margin_start(12); self.sidebar_update_btn.set_margin_end(12)
        self.sidebar_update_btn.set_margin_bottom(10)
        self.sidebar_update_btn.connect("clicked", lambda *_: self.switch_page("settings"))
        self.sidebar_update_lbl = Gtk.Label(); self.sidebar_update_lbl.set_xalign(0)
        self.sidebar_update_btn.set_child(self.sidebar_update_lbl)
        sidebar.append(self.sidebar_update_btn)

        # Hero status box (mode + detail), like the design's side widget
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        hero.add_css_class("side-hero")
        hero.set_margin_start(12); hero.set_margin_end(12); hero.set_margin_bottom(10)
        self.side_hero = hero
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.hero_dot = Gtk.Box(); self.hero_dot.set_size_request(7, 7)
        self.hero_dot.add_css_class("hero-dot")
        mode_row.append(self.hero_dot)
        self.hero_mode = Gtk.Label(label="NORMAL MODE"); self.hero_mode.set_xalign(0)
        self.hero_mode.add_css_class("hero-mode")
        mode_row.append(self.hero_mode)
        hero.append(mode_row)
        self.hero_sub = Gtk.Label(label="all cores · schedutil"); self.hero_sub.set_xalign(0)
        self.hero_sub.add_css_class("hero-sub")
        # Ellipsize so a longer status (e.g. "balance performance") can't widen
        # the sidebar and make it jump when the mode changes.
        self.hero_sub.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.hero_sub.set_hexpand(True)
        hero.append(self.hero_sub)
        sidebar.append(hero)

        # governor / helper line
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        foot.set_margin_start(14); foot.set_margin_end(14); foot.set_margin_bottom(12)
        self.status_footer_lbl = Gtk.Label(label="")
        self.status_footer_lbl.set_xalign(0); self.status_footer_lbl.set_hexpand(True)
        self.status_footer_lbl.set_ellipsize(3)  # don't let a long cpu line widen the sidebar
        self.status_footer_lbl.add_css_class("status-footer")
        foot.append(self.status_footer_lbl)
        helper_ok = Gtk.Label()
        helper_ok.set_markup("<span color='#9ece6a'>helper ✓</span>")
        helper_ok.add_css_class("status-footer")
        foot.append(helper_ok)
        sidebar.append(foot)

        return sidebar

    def _make_sidebar_item(self, label_text, page_name):
        """Clickable nav item with an icon that recolors when active."""
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        item.add_css_class("sidebar-item")
        item._page = page_name

        icon = Gtk.Image()
        icon.set_pixel_size(16)
        item._icon = icon
        item.append(icon)

        lbl = Gtk.Label(label=label_text)
        lbl.set_halign(Gtk.Align.START)
        item.append(lbl)
        item._label = lbl

        self._set_nav_icon(item, False)

        gesture = Gtk.GestureClick()
        gesture.connect("pressed", lambda *args: self.switch_page(page_name))
        item.add_controller(gesture)
        item.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        return item

    def side_hero_set(self, gaming):
        self.side_hero.set_css_classes(["side-hero", "side-hero-gaming"] if gaming
                                       else ["side-hero"])

    def _build_shared_header(self):
        """Title/subtitle on the left, the Game Mode toggle on the right —
        visible on every page, like the v2 design."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        bar.add_css_class("page-header")
        bar.set_margin_start(24); bar.set_margin_end(24)
        bar.set_margin_top(18); bar.set_margin_bottom(6)

        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.header_title = Gtk.Label(label="DASHBOARD"); self.header_title.set_xalign(0)
        self.header_title.add_css_class("header-title")
        txt.append(self.header_title)
        self.header_sub = Gtk.Label(label=""); self.header_sub.set_xalign(0)
        self.header_sub.add_css_class("header-sub")
        txt.append(self.header_sub)
        bar.append(txt)

        spacer = Gtk.Box(); spacer.set_hexpand(True); bar.append(spacer)

        # Game Mode toggle (clickable pill)
        gm = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        gm.add_css_class("gm-toggle")
        self.gm_toggle_lbl = Gtk.Label(label="GAME MODE OFF")
        self.gm_toggle_lbl.add_css_class("gm-toggle-label")
        gm.append(self.gm_toggle_lbl)
        pill = Gtk.Box(); pill.set_size_request(48, 26)
        pill.add_css_class("gm-pill")
        knob = Gtk.Box(); knob.set_size_request(20, 20)
        knob.add_css_class("gm-knob")
        knob.set_halign(Gtk.Align.START); knob.set_valign(Gtk.Align.CENTER)
        pill.append(knob)
        self.gm_pill = pill; self.gm_knob = knob
        gm.append(pill)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *a: self.on_toggle_gm(None))
        gm.add_controller(click)
        gm.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        self.gm_toggle = gm
        bar.append(gm)
        return bar

    # Page title + subtitle shown in the shared header.
    PAGE_TITLES = {
        "dashboard": ("DASHBOARD", None),  # subtitle filled with real specs
        "games": ("GAMES", "per-game fixes from community database · steam library scan"),
        "doctor": ("SYSTEM DOCTOR", "system-level checks: drivers, kernel, gamemode, audio, session"),
        "benchmark": ("BENCHMARK", "find your best CCD · single-core boost test per thread"),
        "settings": ("SETTINGS", "monitoring · game fixes · game mode · about"),
    }

    def switch_page(self, page_name):
        """Switch the ViewStack, recolor nav icons, update the shared header."""
        self.view_stack.set_visible_child_name(page_name)
        for name, item in self.sidebar_items.items():
            active = name == page_name
            item.set_css_classes(["sidebar-item", "sidebar-item-active"] if active
                                 else ["sidebar-item"])
            self._set_nav_icon(item, active)
        title, sub = self.PAGE_TITLES.get(page_name, (page_name.upper(), ""))
        self.header_title.set_label(title)
        if sub is None:
            cpu = self.topo.get_cpu_name()
            gpu = self.gpu.name or "GPU"
            sub = f"{cpu} · {gpu} · CachyOS / Wayland"
        self.header_sub.set_label(sub)

    # ============================================================
    # Page Header Helper
    # ============================================================
    def _page_header(self, title, subtitle=""):
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(16)
        header.set_margin_bottom(8)

        title_lbl = Gtk.Label(label=title)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.add_css_class("page-title")
        header.append(title_lbl)

        if subtitle:
            sub_lbl = Gtk.Label(label=subtitle)
            sub_lbl.set_halign(Gtk.Align.START)
            sub_lbl.add_css_class("page-subtitle")
            header.append(sub_lbl)

        return header

    # ============================================================
    # Dashboard Page (3-column layout)
    # ============================================================
    def _build_dashboard_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_margin_start(24); outer.set_margin_end(24)
        outer.set_margin_top(6); outer.set_margin_bottom(18)

        # --- Stat tiles row (CPU temp/clock, RAM, GPU temp/power) ---
        tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tiles.set_homogeneous(True)
        self.tiles = {}
        for key, label, unit, color in [
            ("cputemp", "CPU TEMP", "°C", "#9ece6a"),
            ("cpuclock", "CPU CLOCK", "MHz avg", "#7aa2f7"),
            ("ram", "MEMORY", "GB", "#bb9af7"),
            ("gputemp", "GPU TEMP", "°C", "#e0af68"),
            ("gpupower", "GPU POWER", "W", "#bb9af7"),
        ]:
            tiles.append(self._v2_tile(key, label, unit, color))
        outer.append(tiles)

        # --- Two-column: [CCD topology + overview] | GPU ---
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        cols.set_vexpand(True)

        # Left column: CCD topology card, then the overview card below it
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        left.set_hexpand(True)

        topo_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        topo_card.add_css_class("v2-card")
        thead = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        tl = Gtk.Label(label="CPU · CCD TOPOLOGY"); tl.add_css_class("card-title"); tl.set_xalign(0)
        thead.append(tl)
        sp = Gtk.Box(); sp.set_hexpand(True); thead.append(sp)
        self.topo_meta = Gtk.Label(label=""); self.topo_meta.add_css_class("card-meta")
        thead.append(self.topo_meta)
        topo_card.append(thead)
        self.ccd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.ccd_box.set_valign(Gtk.Align.START)  # keep CCD cards their natural height
        topo_card.append(self.ccd_box)
        self.ccd_cards = {}
        self.rebuild_ccd_cards()
        left.append(topo_card)
        cols.append(left)

        # Right column: GPU monitoring (compact) + overview below it
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        right.set_size_request(360, -1)
        right.append(self._build_gpu_panel())
        right.append(self._build_overview_card())
        cols.append(right)

        outer.append(cols)
        scroll.set_child(outer)
        page.append(scroll)
        return page

    def _v2_tile(self, key, label, unit, color):
        """A stat tile: big value + unit, small label, thin progress bar."""
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tile.add_css_class("v2-tile")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_valign(Gtk.Align.BASELINE)
        val = Gtk.Label(label="—"); val.add_css_class("tile-value"); val.set_xalign(0)
        val.set_attributes(None)
        row.append(val)
        u = Gtk.Label(label=unit); u.add_css_class("tile-unit"); u.set_xalign(0)
        row.append(u)
        tile.append(row)
        lab = Gtk.Label(label=label); lab.add_css_class("tile-label"); lab.set_xalign(0)
        tile.append(lab)
        bar = Gtk.LevelBar(); bar.set_min_value(0.0); bar.set_max_value(1.0)
        bar.set_value(0.0); bar.add_css_class("tile-bar"); bar.add_css_class(f"lb-{key}")
        tile.append(bar)
        val.add_css_class(f"c-{key}")
        self.tiles[key] = {"val": val, "bar": bar, "unit": u}
        return tile

    def _build_overview_card(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        card.add_css_class("v2-card")
        t = Gtk.Label(label="OVERVIEW"); t.add_css_class("card-title"); t.set_xalign(0)
        card.append(t)
        self.games_info = self._info_card("🎮", "Games", "Scanning…", "games")
        card.append(self.games_info)
        self.bench_info = self._info_card("🏆", "CCD Benchmark", "…", "benchmark")
        card.append(self.bench_info)
        # Recovery escape hatch, not a normal-mode switch: the header Game Mode
        # toggle already handles on/off. Hidden by default and only shown by
        # _render when the CPU layout is unknown (cores parked but unreadable) —
        # the one state where the toggle refuses and this is the only way back.
        self.restore_btn = Gtk.Button(label="Restore all cores")
        self.restore_btn.add_css_class("btn-quiet")
        self.restore_btn.set_margin_top(2)
        self.restore_btn.set_visible(False)
        self.restore_btn.connect("clicked", self.on_restore_cores)
        card.append(self.restore_btn)
        self.gm_status_lbl = Gtk.Label(label="")
        self.gm_status_lbl.set_xalign(0); self.gm_status_lbl.set_wrap(True)
        self.gm_status_lbl.add_css_class("info-card-sub")
        card.append(self.gm_status_lbl)
        self._refresh_overview_async()
        return card

    def _info_card(self, emoji, title, subtitle, target_page):
        """Small clickable card for the dashboard overview → jumps to a page."""
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        card.add_css_class("info-card")
        card.set_margin_top(4)

        ic = Gtk.Label()
        ic.set_markup(f"<span size='20000'>{emoji}</span>")
        card.append(ic)

        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        txt.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=title); t.add_css_class("info-card-title"); t.set_xalign(0)
        txt.append(t)
        sub = Gtk.Label(label=subtitle); sub.add_css_class("info-card-sub"); sub.set_xalign(0)
        sub.set_wrap(True)
        txt.append(sub)
        card.append(txt)

        spacer = Gtk.Box(); spacer.set_hexpand(True); card.append(spacer)
        arrow = Gtk.Label(); arrow.set_markup("<span color='#565f89'>›</span>")
        card.append(arrow)

        gesture = Gtk.GestureClick()
        gesture.connect("pressed", lambda *a: self.switch_page(target_page))
        card.add_controller(gesture)
        card.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        card._sub = sub
        return card

    def _refresh_overview_async(self):
        """Fill the overview cards off the main thread (Steam scan + config read)."""
        def work():
            from topology import load_config
            games_line = "No Steam games detected"
            try:
                db, _ = game_db.load_games()
                root = steam_scanner.find_steam_root()
                if root:
                    installed = {a: n for a, n in steam_scanner.installed_appids(root).items()
                                 if not GamesPage._is_steam_tool(n)}
                    with_fixes = sum(1 for a in installed if a in db)
                    games_line = (f"{len(installed)} installed · {with_fixes} with known fixes"
                                  if installed else "No Steam games detected")
            except Exception:
                games_line = "Could not scan Steam"

            bench = load_config().get("bench")
            if isinstance(bench, dict) and bench.get("cpu") == self.topo.get_cpu_name():
                keep = load_config().get("keep_ccd")
                bench_line = (f"Done — Game Mode keeps CCD{keep}" if keep is not None
                              else "Done — see Benchmark page")
            elif self.topo.ccd_count() < 2:
                bench_line = "Single-CCD CPU — not needed"
            else:
                bench_line = "Not run yet — find your best CCD"

            def apply():
                self.games_info._sub.set_text(games_line)
                self.bench_info._sub.set_text(bench_line)
                return False
            GLib.idle_add(apply)

        threading.Thread(target=work, daemon=True).start()

    # ============================================================
    # Benchmark Page
    # ============================================================
    def _build_benchmark_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # Title now lives in the shared header.

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(16)
        content.set_margin_bottom(16)

        # Header row: status/info (left) + run button (right)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.bench_status = Gtk.Label(label="silicon lottery test · sustained boost per core")
        self.bench_status.set_xalign(0); self.bench_status.set_wrap(True)
        self.bench_status.add_css_class("game-meta")
        self.bench_status.set_hexpand(True)
        head.append(self.bench_status)
        self.bench_btn = Gtk.Button(label="RUN BENCHMARK")
        self.bench_btn.add_css_class("btn-apply-sm")
        self.bench_btn.connect("clicked", self.on_benchmark)
        head.append(self.bench_btn)
        content.append(head)

        # Overall progress
        self.bench_progress = Gtk.ProgressBar()
        self.bench_progress.set_margin_top(6)
        self.bench_progress.set_visible(False)
        content.append(self.bench_progress)

        # Live per-core bars, grouped by CCD, built on demand
        self.bench_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.bench_results.set_margin_top(12)
        content.append(self.bench_results)
        self.bench_rows = {}      # cpu -> {"bar", "val", "row"}
        self.bench_ccd_hdr = {}   # ccd_id -> {"avg", "badge"}

        # Verdict line under the bars
        self.bench_verdict = Gtk.Label(label="")
        self.bench_verdict.set_halign(Gtk.Align.START)
        self.bench_verdict.set_xalign(0)
        self.bench_verdict.set_wrap(True)
        self.bench_verdict.set_margin_top(4)
        content.append(self.bench_verdict)

        # Show the last saved result if there is one, else the empty state.
        if not self._restore_bench():
            self.bench_results.append(self._build_bench_empty())

        scroll.set_child(content)
        page.append(scroll)

        return page

    def _build_bench_empty(self):
        """Pre-run state explaining what the benchmark measures."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(30)

        icon = Gtk.Label()
        icon.set_markup("<span size='46000'>🏆</span>")
        box.append(icon)

        multi = self.topo.ccd_count() > 1
        head = Gtk.Label()
        head.add_css_class("empty-headline")
        head.set_markup("<span size='15000' weight='bold'>"
                        + ("Which CCD won the silicon lottery?" if multi
                           else "Silicon benchmark") + "</span>")
        box.append(head)

        if multi:
            desc = Gtk.Label(label="Every physical core is loaded one at a time and its "
                                   "sustained boost clock is measured. The CCD that holds "
                                   "the higher clock is the better silicon — and becomes the "
                                   "one Game Mode keeps.")
        else:
            desc = Gtk.Label(label="This CPU has a single CCD, so there's nothing to compare. "
                                   "The benchmark still measures each core's sustained boost "
                                   "clock if you want to see per-core quality.")
        desc.add_css_class("page-subtitle")
        desc.set_wrap(True)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_max_width_chars(52)
        box.append(desc)

        meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta.set_halign(Gtk.Align.CENTER)
        meta.set_margin_top(6)
        cores = sum(self.topo.core_count(c) for c in self.topo.get_all_ccd_ids())
        for txt in (f"{self.topo.ccd_count()} CCD" + ("s" if self.topo.ccd_count() != 1 else ""),
                    f"{cores} cores", "~{}s".format(max(4, cores * 2)),
                    "forces performance governor"):
            chip = Gtk.Label(label=txt)
            chip.add_css_class("empty-chip")
            meta.append(chip)
        box.append(meta)

        hint = Gtk.Label()
        hint.set_markup("<span color='#565f89' size='11000'>Click “Run CCD Benchmark” above to start</span>")
        hint.set_margin_top(8)
        box.append(hint)
        return box

    def _bench_frac(self, mhz):
        """Bar fill for a clock, scaled 50%..100% of rated max boost so small
        differences between cores read clearly."""
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
                max_mhz = int(f.read()) // 1000
        except OSError:
            max_mhz = 5000
        floor = int(max_mhz * 0.5)
        if mhz <= floor:
            return 0.0
        return min((mhz - floor) / (max_mhz - floor), 1.0)

    def _save_bench(self, all_results):
        """Persist the full per-core result so the page shows it again next
        launch instead of an empty state."""
        data = {str(ccd): {str(cpu): mhz for cpu, mhz in cores.items()}
                for ccd, cores in all_results.items()}
        save_config({"bench": {"cpu": self.topo.get_cpu_name(), "results": data}})

    def _load_saved_bench(self):
        """Return a prior run's {ccd: {cpu: mhz}} if it matches this CPU, else None."""
        from topology import load_config
        bench = load_config().get("bench")
        if not isinstance(bench, dict) or bench.get("cpu") != self.topo.get_cpu_name():
            return None
        try:
            return {int(ccd): {int(cpu): float(mhz) for cpu, mhz in cores.items()}
                    for ccd, cores in bench["results"].items()}
        except (KeyError, TypeError, ValueError):
            return None

    def _restore_bench(self):
        """Render a saved benchmark result on page build, if there is one."""
        saved = self._load_saved_bench()
        if not saved:
            return False
        self._build_bench_rows()
        for ccd, cores in saved.items():
            for cpu, mhz in cores.items():
                self._bench_final_row(cpu, int(mhz), self._bench_frac(mhz))
        self._show_bench(saved, persist=False)
        self.bench_status.set_markup(
            "<span color='#565f89'>Showing your last benchmark — run again to refresh</span>")
        return True

    def _build_bench_rows(self):
        """Lay out one bar per physical core, grouped by CCD, ready to fill in
        live. Rebuilt each run since the online core set can change."""
        child = self.bench_results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.bench_results.remove(child)
            child = nxt
        self.bench_rows = {}
        self.bench_ccd_hdr = {}

        # CCD groups sit side by side, like the design.
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_homogeneous(True)
        self.bench_results.append(hbox)

        for ccd_id in self.topo.get_all_ccd_ids():
            cores = [c for c in self.topo.get_physical_cores(ccd_id)
                     if self.topo.is_cpu_online(c)]
            if not cores:
                continue

            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            group.add_css_class("bench-group")
            group.set_valign(Gtk.Align.START)

            hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = Gtk.Label(label=f"CCD{ccd_id}")
            name.add_css_class("bench-ccd-name")
            name.set_xalign(0)
            hdr.append(name)
            badge = Gtk.Label(label="")
            badge.add_css_class("bench-badge")
            hdr.append(badge)
            spacer = Gtk.Box(); spacer.set_hexpand(True); hdr.append(spacer)
            avg = Gtk.Label(label="")
            avg.add_css_class("bench-ccd-avg")
            hdr.append(avg)
            group.append(hdr)
            self.bench_ccd_hdr[ccd_id] = {"avg": avg, "badge": badge}

            for i, cpu in enumerate(cores):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lbl = Gtk.Label(label=f"T{i}")
                lbl.set_size_request(34, -1)
                lbl.set_xalign(0)
                lbl.add_css_class("bench-cpu-label")
                row.append(lbl)

                bar = Gtk.LevelBar()
                bar.set_hexpand(True)
                bar.set_valign(Gtk.Align.CENTER)
                bar.set_min_value(0.0)
                bar.set_max_value(1.0)
                bar.set_value(0.0)
                bar.add_css_class("bench-bar")
                row.append(bar)

                val = Gtk.Label(label="— MHz")
                val.set_size_request(90, -1)
                val.set_xalign(1)
                val.add_css_class("bench-mhz")
                row.append(val)

                group.append(row)
                self.bench_rows[cpu] = {"bar": bar, "val": val, "label": lbl}

            hbox.append(group)

    # ============================================================
    # Settings Page
    # ============================================================
    APP_VERSION = "0.1.2"
    GITHUB_URL = "https://github.com/LordHayne/GCC"

    def _build_settings_page(self):
        from topology import load_config
        cfg = load_config()

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(24); box.set_margin_end(24)
        box.set_margin_top(8); box.set_margin_bottom(20)

        # --- MONITORING ---
        card = self._settings_group(box, "MONITORING")
        intervals = [("Fast (1 s)", 1.0), ("Normal (1.5 s)", 1.5),
                     ("Relaxed (3 s)", 3.0), ("Battery (5 s)", 5.0)]
        cur = float(cfg.get("monitor_interval", 1.5))
        idx = min(range(len(intervals)), key=lambda i: abs(intervals[i][1] - cur))
        combo = Gtk.DropDown.new_from_strings([n for n, _ in intervals])
        combo.set_selected(idx); combo.set_valign(Gtk.Align.CENTER)
        combo.connect("notify::selected", lambda d, _p:
                      self._on_interval_changed(intervals[d.get_selected()][1]))
        self._settings_row(card, "Refresh rate",
                           "How often live stats update. Slower = less CPU use.", combo)

        # --- APPEARANCE ---
        card = self._settings_group(box, "APPEARANCE")
        scales = [("Compact", 1.0), ("Default", 1.2),
                  ("Large", 1.4), ("Extra Large", 1.6)]
        cur_scale = float(cfg.get("ui_font_scale", UI_FONT_SCALE))
        sidx = min(range(len(scales)), key=lambda i: abs(scales[i][1] - cur_scale))
        scombo = Gtk.DropDown.new_from_strings([n for n, _ in scales])
        scombo.set_selected(sidx); scombo.set_valign(Gtk.Align.CENTER)
        scombo.connect("notify::selected", lambda d, _p:
                       self._on_font_scale(scales[d.get_selected()][1]))
        self._settings_row(card, "Text size",
                           "Scales all UI text. Applies instantly.", scombo)

        # --- GAME FIXES ---
        card = self._settings_group(box, "GAME FIXES")
        sw = Gtk.Switch(); sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(bool(cfg.get("only_verified", False)))
        sw.connect("notify::active", lambda s, _p: self._on_only_verified(s.get_active()))
        self._settings_row(card, "Only show verified fixes",
                           "Hide untested community suggestions on the Games page.", sw)

        # --- POWER SAVING ---
        card = self._settings_group(box, "POWER SAVING")
        psw = Gtk.Switch(); psw.set_valign(Gtk.Align.CENTER)
        psw.set_active(self._power_saving_active())
        psw.connect("notify::active", lambda s, _p: self._on_power_saving(s.get_active()))
        self.power_switch = psw
        self._settings_row(card, "Power saving mode",
                           "Not a gaming setting — the opposite. Sleeps the audio codec, "
                           "lowers SATA link power and sets the power profile to save "
                           "energy. Turn it OFF for gaming (keeps audio pop-free).", psw)

        # --- GAME MODE ---
        if self.topo.ccd_count() > 1:
            card = self._settings_group(box, "GAME MODE")
            ids = self.topo.get_all_ccd_ids()
            labels = ["Auto (benchmark)"] + [f"CCD{c} ({self.topo.core_count(c)} cores)"
                                             for c in ids]
            ccd_combo = Gtk.DropDown.new_from_strings(labels)
            manual = cfg.get("keep_ccd_manual")
            ccd_combo.set_selected(ids.index(manual) + 1 if manual in ids else 0)
            ccd_combo.set_valign(Gtk.Align.CENTER)
            ccd_combo.connect("notify::selected", lambda d, _p:
                              self._on_keep_ccd(None if d.get_selected() == 0
                                                else ids[d.get_selected() - 1]))
            self._settings_row(card, "CCD to keep",
                               "Which CCD stays active. Auto uses the benchmark winner.",
                               ccd_combo)

        # --- ABOUT ---
        card = self._settings_group(box, "ABOUT")
        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        about.set_margin_start(16); about.set_margin_end(16)
        about.set_margin_top(13); about.set_margin_bottom(13)
        name = Gtk.Label(); name.set_xalign(0)
        name.set_markup("<span weight='bold' color='#c0caf5' size='14000'>Gaming Command Center</span>")
        about.append(name)
        ver = Gtk.Label(label=f"Version {self.APP_VERSION} \u00b7 GPL-3.0-or-later")
        ver.add_css_class("info-card-sub"); ver.set_xalign(0)
        about.append(ver)
        # Update status \u2014 filled in by the background release check on startup.
        self.update_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.update_box.set_margin_top(4)
        about.append(self.update_box)
        self._render_update_status()
        links = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        links.set_margin_top(6)
        links.append(Gtk.LinkButton.new_with_label(self.GITHUB_URL, "GitHub"))
        links.append(Gtk.LinkButton.new_with_label(self.GITHUB_URL + "/issues", "Report a bug"))
        about.append(links)
        card.append(about)

        scroll.set_child(box)
        page.append(scroll)
        return page

    # ---------- app-code update (About section) ----------
    def _check_app_update(self):
        """Background release check on startup; fills the About update row."""
        def work():
            info = app_update.check_update(self.APP_VERSION, BASE_DIR)
            def apply():
                self.update_info = info
                self._render_update_status()
                return False
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()

    def _render_update_status(self):
        info = getattr(self, "update_info", None)

        # Sidebar badge (may exist before the About box does, and vice-versa).
        badge = getattr(self, "sidebar_update_btn", None)
        if badge is not None:
            if info and info.get("available"):
                self.sidebar_update_lbl.set_markup(
                    f"<span size='9500' weight='bold' color='#e0af68'>⬆ Update available</span>")
                badge.set_visible(True)
            else:
                badge.set_visible(False)

        box = getattr(self, "update_box", None)
        if box is None:
            return
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); box.remove(child); child = nxt

        if not info:
            return   # not checked yet / offline — show nothing
        if not info["available"]:
            lbl = Gtk.Label(); lbl.set_xalign(0)
            lbl.set_markup("<span color='#9ece6a'>✓ You're on the latest version</span>")
            lbl.add_css_class("info-card-sub")
            box.append(lbl)
            return

        latest = info["latest"]
        head = Gtk.Label(); head.set_xalign(0)
        head.set_markup(f"<span color='#e0af68' weight='bold'>⬆ Update available: "
                        f"{GLib.markup_escape_text(latest)}</span>")
        box.append(head)

        if info["channel"] == "source":
            btn = Gtk.Button(label="Update now (git pull)")
            btn.add_css_class("btn-apply"); btn.set_halign(Gtk.Align.START)
            self._update_result = Gtk.Label(); self._update_result.set_xalign(0)
            self._update_result.add_css_class("info-card-sub")
            btn.connect("clicked", self._on_git_update)
            box.append(btn); box.append(self._update_result)
        elif info["channel"] == "appimage":
            if app_update.appimage_update_tool() and os.environ.get("APPIMAGE"):
                btn = Gtk.Button(label="Update now (delta)")
                btn.add_css_class("btn-apply"); btn.set_halign(Gtk.Align.START)
                self._update_result = Gtk.Label(); self._update_result.set_xalign(0)
                self._update_result.add_css_class("info-card-sub")
                btn.connect("clicked", self._on_appimage_update)
                box.append(btn); box.append(self._update_result)
            else:
                link = Gtk.LinkButton.new_with_label(app_update.DOWNLOAD_URL, f"Download {latest}")
                link.set_halign(Gtk.Align.START); box.append(link)
                hint = Gtk.Label(label="Then replace your current .AppImage. "
                                       "(Install AppImageUpdate for one-click delta updates.)")
                hint.add_css_class("info-card-sub"); hint.set_xalign(0); box.append(hint)
        else:  # managed (AUR / distro package)
            lbl = Gtk.Label(label="Update via your package manager, or grab it from Releases:")
            lbl.add_css_class("info-card-sub"); lbl.set_xalign(0); box.append(lbl)
            link = Gtk.LinkButton.new_with_label(app_update.RELEASES_PAGE, "Releases")
            link.set_halign(Gtk.Align.START); box.append(link)

    def _on_appimage_update(self, btn):
        btn.set_sensitive(False); btn.set_label("Updating…")
        self._update_result.set_text("")
        appimg = os.environ.get("APPIMAGE")
        def work():
            ok, msg = app_update.run_appimage_update(appimg)
            def done():
                btn.set_label("Update now (delta)")
                btn.set_sensitive(not ok)
                if ok:
                    self._update_result.set_markup(
                        "<span color='#9ece6a'>✓ Updated — restart to apply</span>")
                else:
                    self._update_result.set_markup(
                        f"<span color='#f7768e'>{GLib.markup_escape_text(msg)}</span>")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _on_git_update(self, btn):
        btn.set_sensitive(False); btn.set_label("Updating…")
        self._update_result.set_text("")
        def work():
            ok, msg = app_update.git_pull(BASE_DIR)
            def done():
                btn.set_label("Update now (git pull)")
                btn.set_sensitive(not ok)
                safe = GLib.markup_escape_text(msg)
                if ok:
                    self._update_result.set_markup(
                        f"<span color='#9ece6a'>✓ {safe} — restart to apply</span>")
                else:
                    self._update_result.set_markup(f"<span color='#f7768e'>{safe}</span>")
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _settings_group(self, box, title):
        """Append a section header + an empty group card; return the card so the
        caller can add rows to it (v2 grouped-settings look)."""
        hdr = Gtk.Label(label=title); hdr.add_css_class("card-title"); hdr.set_xalign(0)
        hdr.set_margin_top(6); hdr.set_margin_bottom(2)
        box.append(hdr)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("settings-group")
        box.append(card)
        return card

    def _settings_row(self, card, title, subtitle, control):
        """One row inside a group card: name + desc left, control right."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        row.add_css_class("settings-row")
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        txt.set_hexpand(True); txt.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=title); t.add_css_class("settings-name"); t.set_xalign(0)
        txt.append(t)
        s = Gtk.Label(label=subtitle); s.add_css_class("settings-desc"); s.set_xalign(0)
        s.set_wrap(True)
        txt.append(s)
        row.append(txt)
        control.set_valign(Gtk.Align.CENTER)
        row.append(control)
        card.append(row)

    def _apply_font_scale(self, scale):
        """Re-load the stylesheet at `scale`. Reuses the same provider that's
        already on the display, so GTK restyles the whole UI live."""
        self._font_scale = scale
        load_css(self._css_provider, scale_font_sizes(self._base_css, scale))

    def _on_font_scale(self, scale):
        save_config({"ui_font_scale": scale})
        self._apply_font_scale(scale)

    def _on_interval_changed(self, seconds):
        self._monitor_interval = seconds
        save_config({"monitor_interval": seconds})

    def _on_only_verified(self, active):
        save_config({"only_verified": bool(active)})
        if hasattr(self, "games_page"):
            self.games_page.rescan()

    # ---------- Power Saving toggle (runtime, opt-in — NOT for gaming) ----------
    def _power_saving_active(self):
        """Best-effort current state — the audio codec sleep is the clearest tell."""
        try:
            with open("/sys/module/snd_hda_intel/parameters/power_save") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    @staticmethod
    def _set_power_profile(profile):
        """Ask power-profiles-daemon for a profile (that's where the governor is
        managed on modern systems). No-op if PPD isn't installed."""
        if shutil.which("powerprofilesctl"):
            try:
                subprocess.run(["powerprofilesctl", "set", profile],
                               capture_output=True, text=True, timeout=5)
            except Exception:
                pass

    def _apply_power_saving(self, on):
        """Apply/undo power saving synchronously (callers run it in a thread)."""
        if on:   # save power: codec sleeps, SATA idles, profile → power-saver
            CCDController.helper("audio", "DONE_AUDIO", "")
            CCDController.helper("sata", "DONE_SATA", "")
            self._set_power_profile("power-saver")
        else:    # gaming-friendly: no codec sleep (no pops), full links, balanced
            CCDController.helper("audio-off", "DONE_AUDIO", "")
            CCDController.helper("sata-off", "DONE_SATA", "")
            self._set_power_profile("balanced")

    def _on_power_saving(self, on):
        if getattr(self, "_power_sync", False):
            return   # programmatic switch update (e.g. from Game Mode) — no re-apply
        threading.Thread(target=lambda: self._apply_power_saving(on), daemon=True).start()

    def _sync_power_switch(self, on):
        """Reflect state in the Settings switch without re-triggering the handler."""
        sw = getattr(self, "power_switch", None)
        if sw is not None and sw.get_active() != on:
            self._power_sync = True
            sw.set_active(on)
            self._power_sync = False

    def _on_keep_ccd(self, ccd):
        # None -> Auto: drop the manual override so the benchmark winner applies.
        save_config({"keep_ccd_manual": ccd if ccd is not None else -1})
        self.refresh()

    # ============================================================
    # UI Building Helpers
    # ============================================================
    def _section_header(self, text):
        lbl = Gtk.Label(label=text)
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class("section-header")
        return lbl

    def _stat_tile(self, parent, label, value, color_class=""):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tile.add_css_class("stat-tile")
        v = Gtk.Label(label=value)
        v.add_css_class("stat-value")
        if color_class:
            v.add_css_class(f"stat-value-{color_class}")
        tile.append(v)
        l = Gtk.Label(label=label)
        l.add_css_class("stat-label")
        l.set_halign(Gtk.Align.START)
        tile.append(l)
        parent.append(tile)
        return v

    def rebuild_ccd_cards(self):
        """(Re)create one card per CCD. Called on startup and after the topology
        is re-detected, since parking removes CPUs from sysfs."""
        child = self.ccd_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.ccd_box.remove(child)
            child = nxt

        self.ccd_cards = {}
        for ccd_id in self.topo.get_all_ccd_ids():
            card = self._build_ccd_card(ccd_id)
            self.ccd_box.append(card)
            self.ccd_cards[ccd_id] = card

    def _build_ccd_card(self, ccd_id):
        """CCD card with a per-thread frequency grid (v2 design)."""
        cpus = self.topo.get_ccd_cpus(ccd_id)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("ccd-card")

        # Title row: name + badge + spec + avg
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=f"CCD{ccd_id}"); title.add_css_class("ccd-title")
        title_row.append(title)
        badge = Gtk.Label(label=""); badge.add_css_class("ccd-badge")
        title_row.append(badge)
        sp = Gtk.Box(); sp.set_hexpand(True); title_row.append(sp)
        spec = Gtk.Label(
            label=f"{self.topo.core_count(ccd_id)}C/{len(cpus)}T · 32 MB L3")
        spec.add_css_class("ccd-spec")
        title_row.append(spec)
        avg = Gtk.Label(label="—"); avg.add_css_class("ccd-avg")
        title_row.append(avg)
        card.append(title_row)

        # Thread grid — 6 columns, compact fixed-height cells
        grid = Gtk.Grid()
        grid.set_row_spacing(6); grid.set_column_spacing(6)
        grid.set_column_homogeneous(True)
        cells = {}
        for i, cpu in enumerate(sorted(cpus)):
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            cell.add_css_class("thread-cell")
            cell.set_size_request(-1, 48)
            tid = Gtk.Label(label=f"T{i}"); tid.add_css_class("thread-id")
            cell.append(tid)
            freq = Gtk.Label(label="—"); freq.add_css_class("thread-freq")
            cell.append(freq)
            grid.attach(cell, i % 6, i // 6, 1, 1)
            cells[cpu] = {"cell": cell, "freq": freq}
        card.append(grid)

        card._badge = badge
        card._cells = cells
        card._avg = avg
        return card

    def _gpu_bar(self, label):
        """A GPU metric bar: label + value on one row, fill bar below."""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lab = Gtk.Label(label=label); lab.add_css_class("gpu-bar-label"); lab.set_xalign(0)
        lab.set_hexpand(True)
        top.append(lab)
        val = Gtk.Label(label="—"); val.add_css_class("gpu-bar-value")
        top.append(val)
        wrap.append(top)
        bar = Gtk.LevelBar(); bar.set_min_value(0.0); bar.set_max_value(1.0)
        bar.set_value(0.0); bar.add_css_class("gpu-bar")
        wrap.append(bar)
        return wrap, val, bar

    def _build_gpu_panel(self):
        """GPU card: metric bars + overclocking controls (v2 design)."""
        gpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        gpu_card.add_css_class("v2-card")

        # Header: GPU · <short name> + P-state badge
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        short = (self.gpu.name or "GPU").replace("NVIDIA GeForce ", "")
        self.gpu_name_lbl = Gtk.Label(label=f"GPU · {short}")
        self.gpu_name_lbl.add_css_class("card-title"); self.gpu_name_lbl.set_xalign(0)
        head.append(self.gpu_name_lbl)
        sp = Gtk.Box(); sp.set_hexpand(True); head.append(sp)
        self.gpu_pstate_badge = Gtk.Label(label="—"); self.gpu_pstate_badge.add_css_class("gpu-pstate")
        head.append(self.gpu_pstate_badge)
        gpu_card.append(head)

        # Metric bars
        self.gpu_bars = {}
        for key, label in [("util", "UTILIZATION"), ("vram", "VRAM"),
                           ("core", "CORE CLOCK"), ("power", "POWER DRAW")]:
            wrap, val, bar = self._gpu_bar(label)
            gpu_card.append(wrap)
            self.gpu_bars[key] = {"val": val, "bar": bar}

        # Overclocking / clock-lock controls will live in a dedicated tab later.
        # This dashboard panel is monitoring-only. The backend (CCDController
        # .gpu_lock / .gpu_unlock via the helper) is already in place for it.
        return gpu_card

    # ============================================================
    # Refresh / Live Updates
    # ============================================================
    # ============================================================
    # Monitoring — sampled off the main thread
    # ============================================================
    def _monitor_loop(self):
        """Sampling `sensors` and nvidia-smi/nvidia-settings costs six
        subprocesses per tick. Doing that on the GTK thread froze the UI every
        1.5s, so all of it happens here and only the drawing is handed back."""
        while not self._stop_monitor.is_set():
            self._sample()
            self._stop_monitor.wait(self._monitor_interval)

    def _sample(self):
        try:
            data = self._collect()
        except Exception:
            return  # a transient sysfs/nvidia hiccup must not kill the sampler
        GLib.idle_add(self._render, data)

    def refresh(self):
        """Take one sample now, without blocking the caller (a GTK handler)."""
        threading.Thread(target=self._sample, daemon=True).start()
        return False  # so GLib.timeout_add(..., self.refresh) fires only once

    @staticmethod
    def _read_ram():
        """(used_gb, total_gb) from /proc/meminfo, or (0, 0)."""
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    info[k] = int(v.split()[0])  # kB
            total = info.get("MemTotal", 0) / 1048576
            avail = info.get("MemAvailable", 0) / 1048576
            return total - avail, total
        except (OSError, ValueError):
            return 0.0, 0.0

    def _collect(self):
        topo = self.topo
        ccds = {}
        all_freqs = []
        for ccd_id in topo.get_all_ccd_ids():
            cpus = topo.get_ccd_cpus(ccd_id)
            freqs = {c: (topo.get_cpu_freq(c) if topo.is_cpu_online(c) else None)
                     for c in cpus}
            ccds[ccd_id] = {"cpus": sorted(cpus), "freqs": freqs}
            all_freqs += [f for f in freqs.values() if f]

        self.gpu.update()  # slow: nvidia-smi + 3x nvidia-settings
        ram_used, ram_total = self._read_ram()

        return {
            "cores": topo.online_core_count(),
            "freq0": topo.get_cpu_freq(0),
            "avg_clock": sum(all_freqs) // len(all_freqs) if all_freqs else 0,
            "temp": topo.get_temp(),  # slow: `sensors`
            "gov": self._cpu_perf_label(),
            "game_mode": topo.game_mode_active(),
            "parked": topo.get_parked_ccds(),
            "keep": topo.keep_ccd(),
            "ccd_count": topo.ccd_count(),
            "complete": topo.complete,
            "ccds": ccds,
            "ram_used": ram_used,
            "ram_total": ram_total,
        }

    @staticmethod
    def _cpu_perf_label():
        """CPU performance state for the sidebar. On EPP drivers (amd-pstate-epp,
        intel_pstate) the scaling_governor name stays 'powersave' in active mode
        and is misleading — the EPP is what actually sets the balance, so show
        that instead (e.g. 'performance', 'balance performance')."""
        def r(p):
            try:
                with open(p) as f:
                    return f.read().strip()
            except OSError:
                return None
        driver = r("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver") or ""
        if "epp" in driver or driver == "intel_pstate":
            epp = r("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
            if epp:
                friendly = {"performance": "performance", "balance_performance": "balanced",
                            "balance_power": "power-saving", "power": "power-saving"}
                return friendly.get(epp, epp.replace("_", " "))
        return r("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "?"

    @staticmethod
    def _frac(x, hi):
        return max(0.0, min(x / hi, 1.0)) if hi else 0.0

    def _render(self, d):
        cores = d["cores"]
        # Stat tiles
        self.tiles["cputemp"]["val"].set_label(f"{d['temp']:.0f}")
        self.tiles["cputemp"]["bar"].set_value(self._frac(d['temp'], 95))
        self.tiles["cpuclock"]["val"].set_label(f"{d['avg_clock']/1000:.2f}"
                                                if d['avg_clock'] else "—")
        self.tiles["cpuclock"]["unit"].set_label("GHz avg")
        self.tiles["cpuclock"]["bar"].set_value(self._frac(d['avg_clock'], 4700))
        if d["ram_total"]:
            self.tiles["ram"]["val"].set_label(f"{d['ram_used']:.1f}")
            self.tiles["ram"]["unit"].set_label(f"/ {d['ram_total']:.0f} GB")
            self.tiles["ram"]["bar"].set_value(self._frac(d['ram_used'], d['ram_total']))
        self.tiles["gputemp"]["val"].set_label(f"{self.gpu.temp:.0f}")
        self.tiles["gputemp"]["bar"].set_value(self._frac(self.gpu.temp, 95))
        self.tiles["gpupower"]["val"].set_label(f"{self.gpu.power_draw:.0f}")
        if self.gpu.power_limit:
            self.tiles["gpupower"]["unit"].set_label(f"W / {self.gpu.power_limit:.0f}")
            self.tiles["gpupower"]["bar"].set_value(
                self._frac(self.gpu.power_draw, self.gpu.power_limit))

        gm_on = d["game_mode"]
        parked = ", ".join(f"CCD{c}" for c in d["parked"])

        # Shared header toggle
        self.gm_toggle_lbl.set_label("GAME MODE ON" if gm_on else "GAME MODE OFF")
        for w, on_cls in ((self.gm_toggle, "gm-toggle-on"),
                          (self.gm_toggle_lbl, "gm-toggle-label-on"),
                          (self.gm_pill, "gm-pill-on"), (self.gm_knob, "gm-knob-on")):
            (w.add_css_class if gm_on else w.remove_css_class)(on_cls)

        # Side hero box
        if gm_on:
            self.hero_mode.set_label("GAME MODE")
            self.hero_sub.set_label(f"{parked} parked · {d['gov']}")
            self.hero_mode.set_css_classes(["hero-mode", "hero-mode-gaming"])
            self.hero_dot.set_css_classes(["hero-dot", "hero-dot-gaming"])
            self.side_hero_set(True)
        else:
            self.hero_mode.set_label("NORMAL MODE")
            self.hero_sub.set_label(f"all cores · {d['gov']}")
            self.hero_mode.set_css_classes(["hero-mode"])
            self.hero_dot.set_css_classes(["hero-dot"])
            self.side_hero_set(False)
        self.status_footer_lbl.set_label(f"cpu: {d['gov']}")

        # Power Saving and Game Mode are opposites — lock the Power Saving switch
        # while Game Mode is on (Game Mode already forced power saving off).
        if hasattr(self, "power_switch"):
            self.power_switch.set_sensitive(not gm_on)

        # Game Mode needs something to park — a single-CCD CPU has nothing.
        single_ccd = d["ccd_count"] < 2
        gm_ok = not single_ccd and d["complete"]
        self.gm_toggle.set_sensitive(gm_ok)
        if single_ccd:
            tip = "Game Mode needs a CPU with 2 or more CCDs (Ryzen 9 / Threadripper)"
        elif not d["complete"]:
            tip = "CPU layout unknown while cores are parked — restore all cores first"
        else:
            tip = (f"Parks every CCD except CCD{d['keep']} "
                   f"({self.topo.core_count(d['keep'])} cores stay active)")
        self.gm_toggle.set_tooltip_text(tip)
        if hasattr(self, "gm_btn"):
            self.gm_btn.set_sensitive(gm_ok)
            self.gm_btn.set_tooltip_text(tip)

        # Show the "Restore all cores" recovery button only when it's actually
        # needed — the layout is unknown (cores parked but the topology can't be
        # read), the single state where the Game Mode toggle can't help. In
        # normal use it stays hidden, since Game Mode off already restores.
        if hasattr(self, "restore_btn"):
            self.restore_btn.set_visible(not d["complete"])

        # Topology meta line + CCD thread grids
        if hasattr(self, "topo_meta"):
            self.topo_meta.set_label(f"{cores * (2 if self.topo.smt_enabled() else 1)}"
                                     f"/{len(self.topo.present_cpus())} threads · {d['temp']:.0f}°C")
        for ccd_id, card in self.ccd_cards.items():
            info = d["ccds"].get(ccd_id)
            if not info:
                continue
            cpus = info["cpus"]
            freqs = info["freqs"]
            online = sum(1 for c in cpus if freqs[c] is not None)

            card.set_css_classes(
                ["ccd-card", "ccd-card-parked"] if online == 0 else
                (["ccd-card", "ccd-card-best"] if d["ccd_count"] > 1 and ccd_id == d["keep"]
                 else ["ccd-card", "ccd-card-active"]))
            if online == 0:
                card._badge.set_css_classes(["ccd-badge", "badge-parked"])
                card._badge.set_label("PARKED")
            else:
                best = d["ccd_count"] > 1 and ccd_id == d["keep"]
                card._badge.set_css_classes(["ccd-badge", "badge-best" if best else "badge-active"])
                card._badge.set_label("ACTIVE · BEST" if best else "ACTIVE")

            # Thread cells
            for cpu, w in card._cells.items():
                f = freqs.get(cpu)
                if f is None:
                    w["cell"].set_css_classes(["thread-cell", "thread-off"])
                    w["freq"].set_label("OFF")
                    w["freq"].set_css_classes(["thread-freq", "thread-freq-off"])
                elif f > 4200:
                    w["cell"].set_css_classes(["thread-cell", "thread-boost"])
                    w["freq"].set_label(f"{f/1000:.2f}")
                    w["freq"].set_css_classes(["thread-freq", "thread-freq-boost"])
                else:
                    w["cell"].set_css_classes(["thread-cell"])
                    w["freq"].set_label(f"{f/1000:.2f}")
                    w["freq"].set_css_classes(["thread-freq"])

            live = [f for f in freqs.values() if f is not None]
            card._avg.set_label(f"Ø {sum(live)/len(live)/1000:.2f} GHz" if live else "parked")

        # GPU header + P-state + bars
        short = (self.gpu.name or "GPU").replace("NVIDIA GeForce ", "")
        self.gpu_name_lbl.set_label(f"GPU · {short}")
        self.gpu_pstate_badge.set_label(self.gpu.pstate or "—")
        self.gpu_bars["util"]["val"].set_label(f"{self.gpu.util} %")
        self.gpu_bars["util"]["bar"].set_value(self._frac(self.gpu.util, 100))
        if self.gpu.vram_total:
            self.gpu_bars["vram"]["val"].set_label(
                f"{self.gpu.vram_used/1024:.1f} / {self.gpu.vram_total/1024:.0f} GB")
            self.gpu_bars["vram"]["bar"].set_value(self._frac(self.gpu.vram_used, self.gpu.vram_total))
        self.gpu_bars["core"]["val"].set_label(f"{self.gpu.clock_gr} MHz")
        self.gpu_bars["core"]["bar"].set_value(
            self._frac(self.gpu.clock_gr, self.gpu.max_clock_gr or 2000))
        self.gpu_bars["power"]["val"].set_label(f"{self.gpu.power_draw:.0f} W")
        self.gpu_bars["power"]["bar"].set_value(self._frac(self.gpu.power_draw, self.gpu.power_limit or 300))
        return False

    # ============================================================
    # Game Mode Toggle
    # ============================================================
    def _gm_status(self, msg, color="#9ece6a"):
        if hasattr(self, "gm_status_lbl"):
            self.gm_status_lbl.set_markup(
                f"<span color='{color}'>{GLib.markup_escape_text(msg)}</span>")

    def on_toggle_gm(self, btn):
        if not self.topo.complete:
            self._gm_status("CPU layout unknown — restore all cores first", "#f7768e")
            return

        active = self.topo.game_mode_active()
        keep = self.topo.keep_ccd()
        plan = self.topo.park_plan(keep)

        if not active and not plan:
            self._gm_status("Nothing to park — this CPU has only one CCD", "#f7768e")
            return

        self.gm_toggle.set_sensitive(False)
        if hasattr(self, "gm_btn"):
            self.gm_btn.set_sensitive(False)
            self.gm_btn.set_label("Please wait...")

        gpu_max = getattr(self.gpu, "max_clock_gr", 0)   # NVIDIA only; 0 otherwise

        def run_in_thread():
            if active:
                ok, err = CCDController.unpark_all()
                if ok:
                    self._set_power_profile("balanced")   # back to the normal profile
                if gpu_max:
                    CCDController.gpu_unlock()             # let the GPU clock idle again
                msg = "All cores restored" if ok else err
            else:
                # Everything for gaming, in this ORDER: set the CPU performance
                # profile FIRST while all cores are still online — power-profiles-
                # daemon writes every cpufreq policy and fails (EBUSY) on a core we
                # parked — then undo power saving and park the weaker CCD last.
                self._set_power_profile("performance")
                CCDController.helper("audio-off", "DONE_AUDIO", "")
                CCDController.helper("sata-off", "DONE_SATA", "")
                if gpu_max:
                    # Pin the GPU at its max graphics clock — max performance and
                    # a fix for the NVIDIA P8 idle bug (clock stuck low under load).
                    CCDController.gpu_lock(gpu_max, gpu_max)
                GLib.idle_add(lambda: self._sync_power_switch(False))
                ok, err = CCDController.park(plan)
                msg = (f"Game Mode on — CCD{keep} kept, {len(plan)} threads parked"
                       if ok else err)
            # The kernel needs a moment before sysfs reflects the new state.
            time.sleep(0.5)

            def update_ui():
                self.gm_toggle.set_sensitive(True)
                if hasattr(self, "gm_btn"):
                    self.gm_btn.set_sensitive(True)
                self._gm_status(msg, "#9ece6a" if ok else "#f7768e")
                self.refresh()

            GLib.idle_add(update_ui)

        threading.Thread(target=run_in_thread, daemon=True).start()

    def on_restore_cores(self, btn):
        """Unpark everything — works even when the topology is unknown."""
        btn.set_sensitive(False)

        def run_in_thread():
            ok, err = CCDController.unpark_all()
            time.sleep(0.5)

            def update_ui():
                btn.set_sensitive(True)
                if ok:
                    # Cores are back: re-detect and cache the full layout.
                    self.topo.detect()
                    self.rebuild_ccd_cards()
                    self.gm_status_lbl.set_markup(
                        "<span color='#9ece6a'>All cores restored — CPU layout detected</span>")
                else:
                    self.gm_status_lbl.set_markup(
                        f"<span color='#f7768e'>{GLib.markup_escape_text(err)}</span>")
                self.refresh()

            GLib.idle_add(update_ui)

        threading.Thread(target=run_in_thread, daemon=True).start()

    # ============================================================
    # CCD Benchmark — per-core sustained boost clock
    # ============================================================
    #
    # The old benchmark timed `openssl speed aes-256-cbc`, which runs on the
    # dedicated AES-NI unit — its throughput barely tracks silicon quality, so
    # it could not tell a good CCD from a bad one. The silicon lottery is about
    # which cores hold the highest boost clock under load, so measure that
    # directly: pin a busy loop to one core and sample its frequency.
    #
    # This is only meaningful under the `performance` governor. Under powersave
    # (amd-pstate-epp) the boost a core reaches is governed by power/EPP heuristics,
    # not silicon, and the result flips between runs — so the benchmark forces
    # `performance` for the duration and restores the previous governor after.

    def _measure_core_boost(self, cpu, on_sample=None, load_s=1.4, settle=0.4):
        """Median held frequency (MHz) of one core under a single-thread load.
        on_sample(freq) fires per reading so the UI can animate the bar live."""
        load = subprocess.Popen(
            ["taskset", "-c", str(cpu), "sh", "-c", "while :; do :; done"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(settle)  # let the core ramp to its boost clock first
            samples = []
            end = time.monotonic() + load_s
            while time.monotonic() < end:
                f = self.topo.get_cpu_freq(cpu)
                if f > 0:
                    samples.append(f)
                    if on_sample:
                        on_sample(f)
                time.sleep(0.05)
        finally:
            load.terminate()
            load.wait()
        if not samples:
            return 0
        samples.sort()
        return samples[len(samples) // 2]  # median resists brief dips

    def on_benchmark(self, btn):
        if self.benching:
            return

        def abort(message):
            self.benching = False
            self.bench_btn.set_label("RUN BENCHMARK")
            self.bench_btn.set_sensitive(True)
            self.bench_progress.set_visible(False)
            self.bench_status.set_markup(f"<span color='#f7768e'>{message}</span>")

        # A parked core cannot be benchmarked — taskset would land on an offline
        # CPU and read 0, so the parked CCD would look infinitely slow.
        if self.topo.game_mode_active():
            abort("Cores are parked — disable Game Mode first, "
                  "otherwise the parked CCD cannot be measured.")
            return

        # One CPU per physical core, per CCD, straight from the topology.
        all_cores = []
        for ccd_id in self.topo.get_all_ccd_ids():
            online = [c for c in self.topo.get_physical_cores(ccd_id)
                      if self.topo.is_cpu_online(c)]
            if online:
                all_cores.append((ccd_id, online))
        total_cores = sum(len(cores) for _, cores in all_cores)
        if total_cores == 0:
            abort("No cores available for benchmark")
            return

        self.benching = True
        self.bench_btn.set_label("RUNNING…")
        self.bench_btn.set_sensitive(False)
        self.bench_verdict.set_label("")
        self.bench_progress.set_visible(True)
        self.bench_progress.set_fraction(0.0)
        self._build_bench_rows()

        frac = self._bench_frac  # bars span 50%..100% of rated max boost
        cores_done = [0]
        all_results = {}

        def run_benchmark():
            prev_gov = self.topo.get_governor()
            forced = False
            if prev_gov != "performance":
                forced = CCDController.set_governor("performance")
                note = ("governor forced to performance for the test"
                        if forced else
                        "could not set performance governor — results may be unreliable")
                GLib.idle_add(lambda: self.bench_status.set_markup(
                    f"<span color='#565f89'>{note}</span>"))
                time.sleep(0.3)
            try:
                for ccd_id, physical in all_cores:
                    results = {}
                    for cpu in physical:
                        GLib.idle_add(self._bench_active_row, cpu, ccd_id,
                                      cores_done[0], total_cores)

                        def on_sample(f, c=cpu):
                            GLib.idle_add(self._bench_live, c, f, frac(f))

                        results[cpu] = self._measure_core_boost(cpu, on_sample=on_sample)
                        cores_done[0] += 1
                        GLib.idle_add(self._bench_final_row, cpu, results[cpu],
                                      frac(results[cpu]))
                        GLib.idle_add(lambda: self.bench_progress.set_fraction(
                            cores_done[0] / total_cores))
                    all_results[ccd_id] = results
            finally:
                # Always hand the governor back, even if a measurement threw.
                if forced:
                    CCDController.set_governor(prev_gov)

            GLib.idle_add(lambda: self.bench_progress.set_fraction(1.0))
            GLib.idle_add(lambda: self._finish_benchmark(all_results, forced, prev_gov))

        threading.Thread(target=run_benchmark, daemon=True).start()

    # --- live row updates (all run on the GTK thread) ---

    def _bench_active_row(self, cpu, ccd_id, done, total):
        self.bench_status.set_markup(
            f"<span color='#7aa2f7' weight='bold'>Measuring CCD{ccd_id}, CPU {cpu}</span>"
            f"   <span color='#565f89'>{done}/{total} cores done</span>")
        row = self.bench_rows.get(cpu)
        if row:
            row["label"].add_css_class("bench-cpu-active")
        return False

    def _bench_live(self, cpu, mhz, fraction):
        row = self.bench_rows.get(cpu)
        if row:
            row["bar"].set_value(fraction)
            row["val"].set_label(f"{mhz} MHz")
        return False

    def _bench_final_row(self, cpu, mhz, fraction):
        row = self.bench_rows.get(cpu)
        if row:
            row["bar"].set_value(fraction)
            row["val"].set_markup(f"<b>{mhz} MHz</b>")
            row["label"].remove_css_class("bench-cpu-active")
        return False

    def _finish_benchmark(self, all_results, forced, prev_gov):
        self.benching = False
        self.bench_btn.set_label("RUN BENCHMARK")
        self.bench_btn.set_sensitive(True)
        self.bench_progress.set_visible(False)
        self._show_bench(all_results, forced, prev_gov)

    def _show_bench(self, all_results, forced=False, prev_gov="", persist=True):
        """Fill in each CCD's average and the verdict once measuring is done."""
        ccd_avgs = {}
        best_ccd, best_avg = None, 0
        for ccd_id, results in all_results.items():
            live = [v for v in results.values() if v > 0]
            if not live:
                continue
            avg = sum(live) / len(live)
            ccd_avgs[ccd_id] = avg
            if avg > best_avg:
                best_avg, best_ccd = avg, ccd_id

        if not ccd_avgs:
            self.bench_status.set_markup(
                "<span color='#f7768e' weight='bold'>No results — benchmark failed</span>")
            return

        if persist:
            self.bench_status.set_markup(
                "<span color='#9ece6a' weight='bold'>Benchmark complete</span>")
            self._save_bench(all_results)

        for ccd_id, hdr in self.bench_ccd_hdr.items():
            if ccd_id not in ccd_avgs:
                continue
            hdr["avg"].set_markup(
                f"<span weight='bold'>{ccd_avgs[ccd_id]:.0f} MHz avg</span>")
            if ccd_id == best_ccd and len(ccd_avgs) > 1:
                hdr["badge"].set_label("BEST SILICON")
                hdr["badge"].add_css_class("bench-badge-best")

        if best_ccd is None:
            return
        parts = []
        others = [v for k, v in ccd_avgs.items() if k != best_ccd]
        if others and max(others) > 0:
            diff = best_avg - max(others)
            pct = diff / max(others) * 100
            parts.append(f"<span color='#e0af68' weight='bold'>CCD{best_ccd} holds "
                         f"{diff:.0f} MHz ({pct:.1f}%) more boost — the better silicon.</span>")
        else:
            parts.append(f"<span color='#e0af68' weight='bold'>CCD{best_ccd} is the "
                         f"only measured CCD.</span>")

        # Feed the winner back into Game Mode, which parks everything else.
        if len(ccd_avgs) > 1 and save_config({"keep_ccd": best_ccd}):
            parked = ", ".join(f"CCD{c}" for c in self.topo.get_all_ccd_ids()
                               if c != best_ccd)
            parts.append(f"<span color='#9ece6a'>Game Mode will now keep CCD{best_ccd} "
                         f"and park {parked}.</span>")

        self.bench_verdict.set_markup("\n".join(parts))
        self.best_ccd = best_ccd


# ============================================================
# First-run setup dialog
# ============================================================
class SetupDialog(Adw.Window):
    """One-click system integration. Shown on first launch when the helpers or
    polkit rule are missing, so nobody has to run install.sh in a terminal —
    a single button installs everything via pkexec."""

    def __init__(self, parent):
        super().__init__(modal=True, transient_for=parent)
        self.set_title("Gaming Command Center — Setup")
        self.set_default_size(520, 620)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(34); body.set_margin_end(34)
        body.set_margin_top(4); body.set_margin_bottom(30)
        body.set_valign(Gtk.Align.CENTER); body.set_vexpand(True)
        root.append(body)

        logo_path = os.path.join(BASE_DIR, "GCC_logo.png")
        if os.path.exists(logo_path):
            logo = Gtk.Image.new_from_file(logo_path)
            logo.set_pixel_size(84)
            logo.add_css_class("logo-img")
            logo.set_margin_bottom(2)
            body.append(logo)

        title = Gtk.Label()
        title.set_markup("<span size='17000' weight='bold' color='#c0caf5'>Welcome 🎮</span>")
        body.append(title)

        sub = Gtk.Label()
        sub.set_markup("<span color='#a9b1d6'>A few system components need a one-time "
                       "setup so Game Mode\nand the fixes work without a terminal.</span>")
        sub.set_justify(Gtk.Justification.CENTER); sub.set_wrap(True)
        body.append(sub)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        card.add_css_class("info-card"); card.set_margin_top(6)
        for text in ("Helper programs (CCD parking, /etc fixes)",
                     "Polkit rule (Game Mode without a password)",
                     "App icon &amp; menu launcher"):
            row = Gtk.Label()
            row.set_markup(f"<span color='#9ece6a'>✓</span>  "
                           f"<span color='#c0caf5'>{text}</span>")
            row.set_halign(Gtk.Align.START)
            card.append(row)
        body.append(card)

        note = Gtk.Label()
        note.set_markup("<span size='9500' color='#565f89'>Asks for your admin password "
                        "once. Reversible any time with ./uninstall.sh.</span>")
        note.set_justify(Gtk.Justification.CENTER); note.set_wrap(True)
        body.append(note)

        self.status = Gtk.Label(); self.status.set_wrap(True)
        self.status.set_justify(Gtk.Justification.CENTER)
        body.append(self.status)
        self.spinner = Gtk.Spinner()
        body.append(self.spinner)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btns.set_halign(Gtk.Align.CENTER); btns.set_margin_top(8)
        self.skip_btn = Gtk.Button(label="Later")
        self.skip_btn.add_css_class("flat")
        self.skip_btn.connect("clicked", lambda *_: self.close())
        self.setup_btn = Gtk.Button(label="Set up now")
        self.setup_btn.add_css_class("btn-game-on")
        self.setup_btn.connect("clicked", self.on_setup)
        btns.append(self.skip_btn); btns.append(self.setup_btn)
        body.append(btns)

        self.set_content(root)

    def on_setup(self, _btn):
        self.setup_btn.set_sensitive(False)
        self.skip_btn.set_sensitive(False)
        self.spinner.start()
        self.status.set_markup("<span color='#a9b1d6'>Setting up… watch for the "
                               "password dialog.</span>")

        def work():
            ok, reason = run_privileged_setup()

            def done():
                self.spinner.stop()
                if ok:
                    self.status.set_markup("<span color='#9ece6a' weight='bold'>"
                                           "✅ Done — everything's set up!</span>")
                    self.setup_btn.set_visible(False)
                    self.skip_btn.set_label("Close")
                    self.skip_btn.set_sensitive(True)
                    GLib.timeout_add(1400, self._close_now)
                else:
                    safe = GLib.markup_escape_text(reason)
                    self.status.set_markup(f"<span color='#f7768e'>❌ {safe}</span>")
                    self.setup_btn.set_label("Try again")
                    self.setup_btn.set_sensitive(True)
                    self.skip_btn.set_sensitive(True)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _close_now(self):
        self.close()
        return False


# ============================================================
# Application
# ============================================================
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.gaming.commandcenter")

    def do_activate(self):
        try:
            win = CommandCenter(self)
            win.present()
            # First launch without system integration → offer one-click setup.
            if needs_setup():
                SetupDialog(win).present()
        except Exception:
            # If building the UI throws, print the traceback and quit so the
            # process releases the application-id. Otherwise GLib swallows the
            # exception, the main loop keeps running windowless, and the app
            # looks "stuck" — every later launch just forwards to this zombie.
            import traceback
            traceback.print_exc()
            self.quit()


if __name__ == "__main__":
    app = App()
    app.run(None)