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
from gi.repository import Gtk, Adw, GLib, Gdk, GObject
import subprocess, os, re, shutil, threading, time
from system_scanner import scan_system
from topology import CPUTopology, format_cpu_list, save_config
import game_db
import steam_scanner


# ============================================================
# GPU Info (NVIDIA)
# ============================================================
class GPUInfo:
    def __init__(self):
        self.gr_offset = self.mem_offset = 0
        self.powermizer = 0
        self.update()
        self.update_oc()

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
    def etc_helper(action, expect, success_msg):
        """Run a persistent /etc action. Prompts for admin authentication, and
        the helper reports where it put the backup — surface that to the user."""
        ok, note = CCDController._run([action], expect, timeout=120,
                                      binary=CCDController.ETC_HELPER)
        if not ok:
            return False, note
        return True, f"{success_msg} ({note})" if note else success_msg


class GPUController:
    @staticmethod
    def set_gr_offset(offset):
        try:
            subprocess.run(["nvidia-settings", "-a",
                           f"GPUGraphicsClockOffsetAllPerformanceLevels={offset}"],
                          capture_output=True, text=True, timeout=3)
            return True
        except: return False

    @staticmethod
    def set_mem_offset(offset):
        try:
            subprocess.run(["nvidia-settings", "-a",
                           f"GPUMemoryTransferRateOffsetAllPerformanceLevels={offset}"],
                          capture_output=True, text=True, timeout=3)
            return True
        except: return False

    @staticmethod
    def set_powermizer(mode):
        try:
            subprocess.run(["nvidia-settings", "-a", f"GPUPowerMizerMode={mode}"],
                          capture_output=True, text=True, timeout=3)
            return True
        except: return False


# ============================================================
# System Doctor Page — system-level checks and fixes (formerly Game Doctor)
# ============================================================
class GameDoctorPage(Gtk.Box):
    """System Doctor page — runs system_scanner.scan_system() and displays results."""

    # What the scanner looks at, grouped for the empty-state preview.
    CHECK_GROUPS = [
        ("GPU", ["NVIDIA driver", "GPU P-state", "ReBAR", "Coolbits"]),
        ("CPU", ["Governor", "CCD / Game Mode"]),
        ("Gaming tools", ["GameMode", "gamescope", "GE-Proton"]),
        ("Power & I/O", ["SATA link power", "Audio power save"]),
        ("Session", ["Wayland / X11", "NVIDIA modprobe", "Monitor"]),
    ]

    def __init__(self, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)

        # Page header, consistent with the other pages
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head.set_margin_start(16); head.set_margin_end(16)
        head.set_margin_top(16); head.set_margin_bottom(8)
        t = Gtk.Label(label="SYSTEM DOCTOR"); t.add_css_class("page-title"); t.set_halign(Gtk.Align.START)
        head.append(t)
        st = Gtk.Label(label="Scan for common Linux gaming issues and fix them with one click")
        st.add_css_class("page-subtitle"); st.set_halign(Gtk.Align.START)
        head.append(st)
        self.append(head)

        # Scan button + status
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.set_margin_start(16); top_bar.set_margin_end(16)
        top_bar.set_margin_top(4); top_bar.set_margin_bottom(8)

        self.scan_btn = Gtk.Button(label="Scan System")
        self.scan_btn.add_css_class("btn-apply")
        self.scan_btn.connect("clicked", self.on_scan_clicked)
        top_bar.append(self.scan_btn)

        spacer = Gtk.Box(); spacer.set_hexpand(True); top_bar.append(spacer)

        self.scan_status_lbl = Gtk.Label(label="")
        self.scan_status_lbl.add_css_class("stat-label")
        top_bar.append(self.scan_status_lbl)
        self.append(top_bar)

        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Scrolled area for results
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_vexpand(True)
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results_box.set_margin_start(10)
        self.results_box.set_margin_end(10)
        self.results_box.set_margin_top(8)
        self.results_box.set_margin_bottom(8)
        self.results_box.append(self._build_empty_state())

        clamp.set_child(self.results_box)
        self.scroll.set_child(clamp)
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

        desc = Gtk.Label(label="15+ checks across your GPU, CPU, gaming tools and "
                               "power settings — each with a one-click fix.")
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
        hint.set_markup("<span color='#565f89' size='11000'>Click “Scan System” above to begin</span>")
        hint.set_margin_top(10)
        box.append(hint)
        return box

        # Summary at bottom
        self.summary_lbl = Gtk.Label(label="")
        self.summary_lbl.set_halign(Gtk.Align.CENTER)
        self.summary_lbl.set_margin_top(4)
        self.summary_lbl.set_margin_bottom(8)
        self.append(self.summary_lbl)

        self.scanning = False

    def on_scan_clicked(self, btn):
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.set_sensitive(False)
        self.scan_btn.set_label("Scanning...")
        self.scan_status_lbl.set_label("Scanning system...")

        # Clear old results
        child = self.results_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child

        # Add spinner
        spinner = Gtk.Spinner()
        spinner.set_margin_top(40)
        spinner.start()
        self.results_box.append(spinner)

        def run_scan():
            try:
                checks = scan_system()
            except Exception as e:
                checks = []
                GLib.idle_add(lambda: self.scan_status_lbl.set_label(f"Error: {e}"))
            GLib.idle_add(lambda: self.display_results(checks))

        threading.Thread(target=run_scan, daemon=True).start()

    def display_results(self, checks):
        self.scanning = False
        self.scan_btn.set_sensitive(True)
        self.scan_btn.set_label("Scan System")
        self.scan_status_lbl.set_label("Scan complete")

        # Clear
        child = self.results_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.results_box.remove(child)
            child = next_child

        ok_count = 0
        warn_count = 0
        info_count = 0

        for check in checks:
            if check.status == "ok":
                ok_count += 1
            elif check.status == "warning":
                warn_count += 1
            elif check.status == "info":
                info_count += 1

            row = self._build_check_row(check)
            self.results_box.append(row)

        # Summary
        summary_text = f"{ok_count} OK, {warn_count} Warnings, {info_count} Info"
        if warn_count > 0:
            self.summary_lbl.set_markup(
                f"<span color='#e0af68' weight='bold'>  {summary_text}</span>")
        elif ok_count > 0:
            self.summary_lbl.set_markup(
                f"<span color='#9ece6a' weight='bold'>  {summary_text}</span>")
        else:
            self.summary_lbl.set_markup(
                f"<span color='#7aa2f7' weight='bold'>  {summary_text}</span>")

    def _build_check_row(self, check):
        """Build a single check result row with visible card background."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("wizard-row")
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(3)
        row.set_margin_bottom(0)

        # Status icon
        icons = {"ok": "OK", "warning": "!", "info": "i"}
        icon_lbl = Gtk.Label(label=icons.get(check.status, "i"))
        icon_lbl.set_size_request(28, -1)
        icon_lbl.set_xalign(0.5)
        icon_lbl.set_valign(Gtk.Align.START)
        icon_lbl.set_margin_top(4)
        row.append(icon_lbl)

        # Text column
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)

        name_lbl = Gtk.Label(label=check.name)
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_xalign(0)
        name_lbl.add_css_class("wizard-name")
        text_box.append(name_lbl)

        msg_lbl = Gtk.Label(label=check.message)
        msg_lbl.set_halign(Gtk.Align.START)
        msg_lbl.set_xalign(0)
        msg_lbl.set_wrap(True)
        msg_lbl.add_css_class("wizard-msg")
        text_box.append(msg_lbl)

        # Fix hint for warnings
        if check.fix_message and check.status == "warning":
            fix_lbl = Gtk.Label(label=f"-> {check.fix_message}")
            fix_lbl.set_halign(Gtk.Align.START)
            fix_lbl.set_xalign(0)
            fix_lbl.set_wrap(True)
            fix_lbl.add_css_class("wizard-fix")
            text_box.append(fix_lbl)

        row.append(text_box)

        # Fix button for warnings
        if check.status == "warning" and check.fix_message:
            fix_btn = Gtk.Button(label="Apply Fix")
            fix_btn.add_css_class("btn-apply")
            fix_btn.set_valign(Gtk.Align.CENTER)
            fix_btn.set_margin_start(8)
            # The row's message label doubles as the result line, so a failed
            # fix can say *why* instead of just turning the button red.
            fix_btn._msg_lbl = msg_lbl
            fix_btn.connect("clicked", self.on_fix_clicked, check)
            row.append(fix_btn)

        return row

    # --- individual fixes: each returns (ok, message) and never lies ---

    def _fix_governor(self):
        return CCDController.helper("governor", "DONE_GOVERNOR",
                                    "CPU governor set to powersave")

    def _fix_audio(self):
        return CCDController.helper("audio", "DONE_AUDIO", "Audio power save enabled")

    def _fix_sata(self):
        return CCDController.helper("sata", "DONE_SATA",
                                    "SATA link power set to med_power_with_dipm")

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

    def on_fix_clicked(self, btn, check):
        """Run the fix for this check in a worker thread and report what happened."""
        fixes = {
            "CPU Governor":     self._fix_governor,
            "CCD / Game Mode":  self._fix_game_mode,
            "Audio Power Save": self._fix_audio,
            "SATA Link Power":  self._fix_sata,
            "GameMode Config":  self._fix_gamemode_ini,
            "NVIDIA Modprobe":  self._fix_modprobe,
            "Coolbits / GPU-OC": self._fix_coolbits,
            "GameMode":         lambda: self._install_package("gamemode"),
            "gamescope":        lambda: self._install_package("gamescope"),
        }
        fix = fixes.get(check.name)
        if fix is None:
            btn._msg_lbl.set_label("No automatic fix for this check yet")
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
                return False

            GLib.idle_add(update_ui)

        threading.Thread(target=apply_in_thread, daemon=True).start()


# ============================================================
# Games Page — per-game fixes from games.yaml
# ============================================================
class GamesPage(Gtk.Box):
    """Detects the user's Steam games, matches them against games.yaml, and
    offers one-click fixes. The database is the trust boundary (game_db only
    ever loads whitelisted fix types); this page just applies them."""

    def __init__(self, win, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self.win = win
        self.db, self.db_err = game_db.load_games()

        # Header + rescan
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(16)
        header.set_margin_bottom(8)
        title = Gtk.Label(label="GAMES")
        title.add_css_class("page-title")
        title.set_halign(Gtk.Align.START)
        header.append(title)
        spacer = Gtk.Box(); spacer.set_hexpand(True); header.append(spacer)
        self.rescan_btn = Gtk.Button(label="Rescan")
        self.rescan_btn.add_css_class("btn-apply")
        self.rescan_btn.connect("clicked", lambda *_: self.rescan())
        header.append(self.rescan_btn)
        self.append(header)

        self.subtitle = Gtk.Label(label="")
        self.subtitle.add_css_class("page-subtitle")
        self.subtitle.set_halign(Gtk.Align.START)
        self.subtitle.set_margin_start(16)
        self.subtitle.set_wrap(True)
        self.append(self.subtitle)
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

        self.rescan()

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
        gpu, session = self._gpu_vendor(), self._session()
        from topology import load_config
        only_verified = load_config().get("only_verified", False)

        rows = []
        for appid, name in installed.items():
            if self._is_steam_tool(name):
                continue
            game = self.db.get(appid)
            disp_name = (game.name if game else name) or f"App {appid}"
            issues = [i for i in (game.issues if game else [])
                      if i.matches_system(gpu, session)
                      and (not only_verified or i.fix.verified)]
            rows.append((appid, disp_name, issues, game is not None))

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

        for appid, name, issues, in_db in rows:
            self.list_box.append(self._build_game_card(appid, name, issues, in_db))

    def _empty(self, text):
        lbl = Gtk.Label(label=text)
        lbl.add_css_class("page-subtitle")
        lbl.set_wrap(True)
        lbl.set_xalign(0)
        lbl.set_margin_top(20)
        self.list_box.append(lbl)

    def _build_game_card(self, appid, name, issues, in_db):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("game-card")

        # Title row: name + ProtonDB tier (lazy) + issue count
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=name)
        title.add_css_class("game-title")
        title.set_xalign(0)
        title_row.append(title)
        tier_lbl = Gtk.Label(label="")
        tier_lbl.add_css_class("game-tier")
        title_row.append(tier_lbl)
        spacer = Gtk.Box(); spacer.set_hexpand(True); title_row.append(spacer)
        summary = Gtk.Label(label=(
            f"{len(issues)} known issue{'s' if len(issues) != 1 else ''}"
            if issues else "no known issues for your setup"))
        summary.add_css_class("stat-label")
        title_row.append(summary)
        card.append(title_row)

        for issue in issues:
            card.append(self._build_issue_row(appid, issue))

        self._load_tier_async(appid, tier_lbl)
        return card

    def _build_issue_row(self, appid, issue):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.add_css_class("issue-row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sym = Gtk.Label(label=issue.symptom)
        sym.add_css_class("issue-symptom")
        sym.set_xalign(0)
        sym.set_wrap(True)
        sym.set_hexpand(True)
        top.append(sym)
        # Trust badge: a green check for tested fixes, an amber flag otherwise.
        badge = Gtk.Label()
        if issue.fix.verified:
            badge.set_label("✓ Verified")
            badge.add_css_class("badge-verified")
        else:
            badge.set_label("untested")
            badge.add_css_class("badge-untested")
        badge.set_valign(Gtk.Align.START)
        top.append(badge)
        row.append(top)

        if issue.cause:
            cause = Gtk.Label(label=issue.cause)
            cause.add_css_class("issue-cause")
            cause.set_xalign(0)
            cause.set_wrap(True)
            row.append(cause)

        fix = issue.fix
        if fix.type == "info":
            info = Gtk.Label(label=fix.value)
            info.add_css_class("issue-info")
            info.set_xalign(0)
            info.set_wrap(True)
            info.set_selectable(True)  # so the user can copy a command
            row.append(info)
        else:
            action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            btn = Gtk.Button()
            btn.add_css_class("btn-apply")
            if fix.type == "launch_option":
                btn.set_label("Apply launch option")
            elif fix.type == "file":
                btn.set_label("Create config file")
            elif fix.type == "tool_action":
                btn.set_label("Apply fix")
            btn.connect("clicked", self.on_apply_fix, appid, issue)
            action_row.append(btn)
            result = Gtk.Label(label="")
            result.add_css_class("issue-result")
            result.set_xalign(0)
            result.set_wrap(True)
            action_row.append(result)
            btn._result = result
            row.append(action_row)

            if fix.type == "launch_option":
                # The exact string, always visible and selectable — the manual
                # fallback if Steam is open.
                lo = Gtk.Label(label=fix.value)
                lo.add_css_class("issue-info")
                lo.set_xalign(0)
                lo.set_wrap(True)
                lo.set_selectable(True)
                row.append(lo)

        return row

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
            return steam_scanner.set_launch_options(appid, fix.value)
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
            colors = {"platinum": "#c0caf5", "gold": "#e0af68",
                      "silver": "#9aa5ce", "bronze": "#cd7f32",
                      "borked": "#f7768e", "pending": "#565f89"}
            c = colors.get(tier, "#565f89")
            GLib.idle_add(lambda: label.set_markup(
                f"<span color='{c}'>ProtonDB: {tier.capitalize()} "
                f"({total} reports)</span>") or False)

        threading.Thread(target=work, daemon=True).start()


# ============================================================
# Main Window
# ============================================================
class CommandCenter(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Gaming Command Center")
        self.set_default_size(960, 680)
        self.topo = CPUTopology()
        self.gpu = GPUInfo()
        self.benching = False
        self.best_ccd = None
        self._stop_monitor = threading.Event()
        self._oc_touched = False   # user is editing the OC controls
        self._syncing_oc = False   # we are writing them ourselves
        from topology import load_config
        try:
            self._monitor_interval = float(load_config().get("monitor_interval", 1.5))
        except (TypeError, ValueError):
            self._monitor_interval = 1.5

        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        css = """
        /* === Sidebar === */
        .sidebar { background: #16161e; }
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
            background: #24253b;
            color: #c0caf5;
        }
        .sidebar-title { font-size: 16px; font-weight: bold; color: #c0caf5; }
        .sidebar-subtitle { font-size: 10px; color: #565f89; }
        .sidebar-footer { font-size: 9px; color: #565f89; }

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
            background: #24253b;
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
            background: #24253b;
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
            background: #1e1f2e; border-radius: 10px; padding: 10px 12px;
            border: 1px solid rgba(255,255,255,0.05);
        }
        .bench-ccd-name { color: #c0caf5; font-weight: bold; font-size: 14px; }
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
        .game-title { color: #c0caf5; font-weight: bold; font-size: 16px; }
        .game-tier { font-size: 11px; }
        .issue-row {
            background: #14151f; border-radius: 8px; padding: 8px 10px;
            margin-top: 6px;
        }
        .issue-symptom { color: #e0af68; font-weight: bold; }
        .issue-cause { color: #565f89; font-size: 12px; }
        .badge-verified {
            background: rgba(158,206,106,0.15); color: #9ece6a;
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
            background: #24253b;
            border-radius: 10px;
            padding: 10px 14px;
            border: 1px solid rgba(255,255,255,0.04);
        }
        .wizard-name { font-weight: 700; font-size: 13px; color: #c0caf5; }
        .wizard-msg { font-size: 11px; color: #a9b1d6; }
        .wizard-fix { font-size: 10px; color: #e0af68; }

        .scan-row {
            background: #24253b;
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
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(css)
        except (AttributeError, TypeError):
            provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.build_ui()
        self.refresh()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
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

        # Sidebar
        sidebar = self._build_sidebar()
        main_box.append(sidebar)

        # Vertical separator
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(sep)

        # Content area
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content_box.set_hexpand(True)

        # ViewStack for page switching
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

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

        # Default to Dashboard
        self.view_stack.set_visible_child_name("dashboard")

    # ============================================================
    # Sidebar
    # ============================================================
    def _build_sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.add_css_class("sidebar")
        sidebar.set_size_request(180, -1)

        # App logo + name at top
        logo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        logo_box.set_margin_start(14)
        logo_box.set_margin_end(14)
        logo_box.set_margin_top(16)
        logo_box.set_margin_bottom(16)

        # Logo image (circular, 36px)
        try:
            logo_img = Gtk.Image.new_from_file("/home/thomas/.local/share/icons/hicolor/256x256/apps/gaming-command-center.png")
            logo_img.set_pixel_size(36)
            logo_box.append(logo_img)
        except:
            pass

        # Text column
        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Gaming")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("sidebar-title")
        text_col.append(title)

        subtitle = Gtk.Label(label="Command Center")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.add_css_class("sidebar-subtitle")
        text_col.append(subtitle)

        logo_box.append(text_col)
        sidebar.append(logo_box)

        # Navigation items
        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        nav_box.set_margin_start(8)
        nav_box.set_margin_end(8)

        self.sidebar_items = {}
        nav_entries = [
            ("Dashboard", "dashboard"),
            ("Games", "games"),
            ("System Doctor", "doctor"),
            ("Benchmark", "benchmark"),
            ("Settings", "settings"),
        ]
        for label, page_name in nav_entries:
            item = self._make_sidebar_item(label, page_name)
            nav_box.append(item)
            self.sidebar_items[page_name] = item

        sidebar.append(nav_box)

        # Spacer to push footer to bottom
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        # Status footer at bottom
        footer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        footer_box.set_margin_start(14)
        footer_box.set_margin_end(14)
        footer_box.set_margin_top(8)
        footer_box.set_margin_bottom(14)

        self.status_footer_lbl = Gtk.Label(label="")
        self.status_footer_lbl.set_halign(Gtk.Align.START)
        self.status_footer_lbl.set_markup(
            "<span color='#565f89'>  System Ready</span>")
        footer_box.append(self.status_footer_lbl)

        sidebar.append(footer_box)

        return sidebar

    def _make_sidebar_item(self, label_text, page_name):
        """Create a clickable sidebar navigation item."""
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        item.add_css_class("sidebar-item")

        lbl = Gtk.Label(label=label_text)
        lbl.set_halign(Gtk.Align.START)
        item.append(lbl)
        item._label = lbl

        gesture = Gtk.GestureClick()
        gesture.connect("pressed", lambda *args: self.switch_page(page_name))
        item.add_controller(gesture)

        return item

    def switch_page(self, page_name):
        """Switch the ViewStack to the selected page and update sidebar CSS."""
        self.view_stack.set_visible_child_name(page_name)
        for name, item in self.sidebar_items.items():
            if name == page_name:
                item.add_css_class("sidebar-item-active")
            else:
                item.remove_css_class("sidebar-item-active")

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

        # Top header
        cpu_name = self.topo.get_cpu_name()
        gpu_name = self.gpu.name if self.gpu.name else "NVIDIA GPU"
        specs_text = f"{cpu_name}  -  {gpu_name}  -  CachyOS / Wayland"
        page.append(self._page_header("DASHBOARD", specs_text))
        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Scrollable 3-column content
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(12)
        content.set_margin_bottom(16)

        # --- Center-left column: CPU stats + CCD cards + Game Mode ---
        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_col.set_hexpand(True)

        # Status banner
        self.banner = Gtk.Label(label="")
        self.banner.set_halign(Gtk.Align.START)
        left_col.append(self.banner)

        # CPU stats tiles (Cores, Clock, Temp, Governor)
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        stats.set_margin_top(4)
        stats.set_homogeneous(True)
        self.lbl_threads = self._stat_tile(stats, "Cores", "24", "blue")
        self.lbl_freq = self._stat_tile(stats, "Clock", "---", "orange")
        self.lbl_temp = self._stat_tile(stats, "Temp", "--", "green")
        self.lbl_governor = self._stat_tile(stats, "Governor", "---", "")
        left_col.append(stats)

        # CCD section — cards are rebuilt whenever the topology changes
        ccx = self.topo.ccx_per_ccd()
        header = "CPU CCDs" + (f"  ({ccx} CCX per CCD)" if ccx > 1 else "")
        left_col.append(self._section_header(header))
        self.ccd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_col.append(self.ccd_box)
        self.ccd_cards = {}
        self.rebuild_ccd_cards()

        # Game Mode button
        self.gm_btn = Gtk.Button(label="Enable Game Mode")
        self.gm_btn.set_margin_top(8)
        self.gm_btn.add_css_class("btn-game-on")
        self.gm_btn.connect("clicked", self.on_toggle_gm)
        left_col.append(self.gm_btn)

        # Recovery: brings every core back even if the layout is unknown
        self.restore_btn = Gtk.Button(label="Restore all cores")
        self.restore_btn.set_margin_top(4)
        self.restore_btn.connect("clicked", self.on_restore_cores)
        left_col.append(self.restore_btn)

        self.gm_status_lbl = Gtk.Label(label="")
        self.gm_status_lbl.set_halign(Gtk.Align.START)
        self.gm_status_lbl.set_wrap(True)
        self.gm_status_lbl.set_margin_top(4)
        left_col.append(self.gm_status_lbl)

        # Quick-info cards: detected games + benchmark status, both clickable.
        left_col.append(self._section_header("Overview"))
        self.games_info = self._info_card(
            "🎮", "Games", "Scanning…", "games")
        left_col.append(self.games_info)
        self.bench_info = self._info_card(
            "🏆", "CCD Benchmark", "…", "benchmark")
        left_col.append(self.bench_info)
        self._refresh_overview_async()

        content.append(left_col)

        # --- Right column: GPU monitoring + Overclocking ---
        right_col = self._build_gpu_panel()
        content.append(right_col)

        scroll.set_child(content)
        page.append(scroll)

        return page

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

        page.append(self._page_header(
            "BENCHMARK",
            "Test each CPU core to find the best CCD for gaming"))
        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(16)
        content.set_margin_bottom(16)

        # Run button
        self.bench_btn = Gtk.Button(label="Run CCD Benchmark")
        self.bench_btn.add_css_class("btn-bench")
        self.bench_btn.set_halign(Gtk.Align.START)
        self.bench_btn.connect("clicked", self.on_benchmark)
        content.append(self.bench_btn)

        # One-line status while running (which core, governor note)
        self.bench_status = Gtk.Label(label="")
        self.bench_status.set_halign(Gtk.Align.START)
        self.bench_status.set_xalign(0)
        self.bench_status.set_wrap(True)
        self.bench_status.set_margin_top(8)
        content.append(self.bench_status)

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

        for ccd_id in self.topo.get_all_ccd_ids():
            cores = [c for c in self.topo.get_physical_cores(ccd_id)
                     if self.topo.is_cpu_online(c)]
            if not cores:
                continue

            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            group.add_css_class("bench-group")

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

            for cpu in cores:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                lbl = Gtk.Label(label=f"CPU {cpu}")
                lbl.set_size_request(56, -1)
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

            self.bench_results.append(group)

    # ============================================================
    # Settings Page
    # ============================================================
    APP_VERSION = "0.1.0"
    GITHUB_URL = "https://github.com/LordHayne/GCC"

    def _build_settings_page(self):
        from topology import load_config
        cfg = load_config()

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.append(self._page_header("SETTINGS"))
        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(16); box.set_margin_end(16)
        box.set_margin_top(14); box.set_margin_bottom(20)

        # --- Monitoring interval ---
        box.append(self._section_header("Monitoring"))
        intervals = [("Fast (1 s)", 1.0), ("Normal (1.5 s)", 1.5),
                     ("Relaxed (3 s)", 3.0), ("Battery (5 s)", 5.0)]
        cur = float(cfg.get("monitor_interval", 1.5))
        idx = min(range(len(intervals)), key=lambda i: abs(intervals[i][1] - cur))
        row = self._settings_row("Refresh rate",
                                 "How often live stats update. Slower = less CPU use.")
        combo = Gtk.DropDown.new_from_strings([n for n, _ in intervals])
        combo.set_selected(idx)
        combo.set_valign(Gtk.Align.CENTER)
        combo.connect("notify::selected", lambda d, _p:
                      self._on_interval_changed(intervals[d.get_selected()][1]))
        row.append(combo)
        box.append(row)

        # --- Games / fixes ---
        box.append(self._section_header("Game fixes"))
        row = self._settings_row("Only show verified fixes",
                                 "Hide untested community suggestions on the Games page.")
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(bool(cfg.get("only_verified", False)))
        sw.connect("notify::active", lambda s, _p: self._on_only_verified(s.get_active()))
        row.append(sw)
        box.append(row)

        # --- CCD choice for Game Mode ---
        if self.topo.ccd_count() > 1:
            box.append(self._section_header("Game Mode"))
            row = self._settings_row("CCD to keep",
                                     "Which CCD stays active. Auto uses the benchmark winner.")
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
            row.append(ccd_combo)
            box.append(row)

        # --- About ---
        box.append(self._section_header("About"))
        about = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        about.add_css_class("empty-card")
        name = Gtk.Label(); name.set_xalign(0)
        name.set_markup(f"<span weight='bold' color='#c0caf5' size='14000'>Gaming Command Center</span>")
        about.append(name)
        ver = Gtk.Label(label=f"Version {self.APP_VERSION} \u00b7 GPL-3.0-or-later")
        ver.add_css_class("info-card-sub"); ver.set_xalign(0)
        about.append(ver)
        links = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        links.set_margin_top(6)
        gh = Gtk.LinkButton.new_with_label(self.GITHUB_URL, "GitHub")
        bug = Gtk.LinkButton.new_with_label(self.GITHUB_URL + "/issues", "Report a bug")
        links.append(gh); links.append(bug)
        about.append(links)
        box.append(about)

        clamp.set_child(box)
        scroll.set_child(clamp)
        page.append(scroll)
        return page

    def _settings_row(self, title, subtitle):
        """A labelled settings row; caller appends the control widget."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("info-card")
        row.set_margin_top(4)
        txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        txt.set_hexpand(True); txt.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=title); t.add_css_class("info-card-title"); t.set_xalign(0)
        txt.append(t)
        s = Gtk.Label(label=subtitle); s.add_css_class("info-card-sub"); s.set_xalign(0)
        s.set_wrap(True)
        txt.append(s)
        row.append(txt)
        return row

    def _on_interval_changed(self, seconds):
        self._monitor_interval = seconds
        save_config({"monitor_interval": seconds})

    def _on_only_verified(self, active):
        save_config({"only_verified": bool(active)})
        if hasattr(self, "games_page"):
            self.games_page.rescan()

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
        """Simplified CCD card: name, badge, core dots, avg freq."""
        cpus = self.topo.get_ccd_cpus(ccd_id)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("ccd-card")
        card.set_margin_top(4)

        # Title row: CCD name + badge + thread count
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=f"CCD{ccd_id}")
        title.add_css_class("ccd-title")
        title_row.append(title)

        badge = Gtk.Label(label="")
        badge.add_css_class("ccd-badge")
        title_row.append(badge)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        title_row.append(spacer)

        count_lbl = Gtk.Label(label=f"{self.topo.core_count(ccd_id)} Cores")
        count_lbl.add_css_class("stat-label")
        title_row.append(count_lbl)
        card.append(title_row)

        # Core dots
        cores_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cores_box.set_margin_top(2)
        for cpu in sorted(cpus):
            dot = Gtk.Box()
            dot.set_size_request(12, 12)
            dot.add_css_class("core-dot")
            dot.set_tooltip_text(f"CPU {cpu}")
            cores_box.append(dot)
        card.append(cores_box)

        # Avg freq line
        avg_freq_lbl = Gtk.Label(label="---")
        avg_freq_lbl.set_halign(Gtk.Align.START)
        avg_freq_lbl.add_css_class("stat-label")
        card.append(avg_freq_lbl)

        card._badge = badge
        card._cores = cores_box
        card._avg_freq = avg_freq_lbl
        return card

    def _build_gpu_panel(self):
        """Build the right-column GPU panel with monitoring + overclocking."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        panel.set_size_request(290, -1)
        panel.set_hexpand(False)

        gpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        gpu_card.add_css_class("gpu-card")

        # GPU name
        self.gpu_name_lbl = Gtk.Label(label=self.gpu.name or "NVIDIA GPU")
        self.gpu_name_lbl.set_markup(
            f"<span size='14' weight='bold' color='#c0caf5'>{self.gpu.name or 'NVIDIA GPU'}</span>")
        self.gpu_name_lbl.set_halign(Gtk.Align.START)
        gpu_card.append(self.gpu_name_lbl)

        # GPU stats row 1: Core, Memory, Power
        gpu_stats1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gpu_stats1.set_homogeneous(True)
        gpu_stats1.set_margin_top(6)
        self.gpu_clock_lbl = self._stat_tile(gpu_stats1, "Core", "---", "blue")
        self.gpu_mem_lbl = self._stat_tile(gpu_stats1, "Memory", "---", "")
        self.gpu_power_lbl = self._stat_tile(gpu_stats1, "Power", "---", "orange")
        gpu_card.append(gpu_stats1)

        # GPU stats row 2: Temp, VRAM
        gpu_stats2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        gpu_stats2.set_homogeneous(True)
        gpu_stats2.set_margin_top(4)
        self.gpu_temp_lbl = self._stat_tile(gpu_stats2, "Temp", "---", "green")
        self.gpu_vram_lbl = self._stat_tile(gpu_stats2, "VRAM", "---", "")
        gpu_card.append(gpu_stats2)

        # Info label (P-State, Util, Power Limit)
        self.gpu_info_lbl = Gtk.Label(label="")
        self.gpu_info_lbl.set_halign(Gtk.Align.START)
        self.gpu_info_lbl.add_css_class("stat-label")
        self.gpu_info_lbl.set_margin_top(4)
        gpu_card.append(self.gpu_info_lbl)

        # Clock progress bar
        self.gpu_clock_bar = Gtk.ProgressBar()
        self.gpu_clock_bar.set_margin_top(6)
        gpu_card.append(self.gpu_clock_bar)

        # OC Section
        oc_title = Gtk.Label(label="Overclocking")
        oc_title.set_markup(
            "<span weight='bold' color='#7aa2f7' size='12'>Overclocking</span>")
        oc_title.set_halign(Gtk.Align.START)
        oc_title.set_margin_top(10)
        gpu_card.append(oc_title)

        # Core offset slider
        gr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gr_row.set_margin_top(6)
        gr_lbl = Gtk.Label(label="Core")
        gr_lbl.set_markup("<span color='#c0caf5'>Core</span>")
        gr_row.append(gr_lbl)
        self.gr_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.gr_slider.set_hexpand(True)
        self.gr_slider.set_value(0)
        self.gr_slider.connect("value-changed", self.on_gr_slider)
        scroll_ctrl_gr = Gtk.EventControllerScroll()
        scroll_ctrl_gr.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl_gr.connect("scroll", lambda c, dx, dy: True)
        self.gr_slider.add_controller(scroll_ctrl_gr)
        gr_row.append(self.gr_slider)
        self.gr_value_lbl = Gtk.Label(label="+0 MHz")
        self.gr_value_lbl.set_xalign(1)
        self.gr_value_lbl.add_css_class("stat-label")
        gr_row.append(self.gr_value_lbl)
        gpu_card.append(gr_row)

        # VRAM offset slider
        mem_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mem_row.set_margin_top(4)
        mem_lbl = Gtk.Label(label="VRAM")
        mem_lbl.set_markup("<span color='#c0caf5'>VRAM</span>")
        mem_row.append(mem_lbl)
        self.mem_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.mem_slider.set_hexpand(True)
        self.mem_slider.set_value(0)
        self.mem_slider.connect("value-changed", self.on_mem_slider)
        scroll_ctrl_mem = Gtk.EventControllerScroll()
        scroll_ctrl_mem.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll_ctrl_mem.connect("scroll", lambda c, dx, dy: True)
        self.mem_slider.add_controller(scroll_ctrl_mem)
        mem_row.append(self.mem_slider)
        self.mem_value_lbl = Gtk.Label(label="+0 MHz")
        self.mem_value_lbl.set_xalign(1)
        self.mem_value_lbl.add_css_class("stat-label")
        mem_row.append(self.mem_value_lbl)
        gpu_card.append(mem_row)

        # PowerMizer dropdown
        pm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pm_row.set_margin_top(6)
        pm_lbl = Gtk.Label(label="PowerMizer")
        pm_lbl.set_markup("<span color='#c0caf5'>PowerMizer</span>")
        pm_row.append(pm_lbl)
        self.pm_combo = Gtk.DropDown.new_from_strings(
            ["Adaptive", "Max Performance", "Auto"])
        self.pm_combo.set_selected(0)
        pm_row.append(self.pm_combo)
        gpu_card.append(pm_row)

        # Apply OC button
        self.oc_btn = Gtk.Button(label="Apply OC")
        self.oc_btn.set_margin_top(8)
        self.oc_btn.add_css_class("btn-apply")
        self.oc_btn.connect("clicked", self.on_apply_oc)
        gpu_card.append(self.oc_btn)

        self.oc_status_lbl = Gtk.Label(label="")
        self.oc_status_lbl.set_margin_top(4)
        self.oc_status_lbl.set_halign(Gtk.Align.START)
        gpu_card.append(self.oc_status_lbl)

        panel.append(gpu_card)
        return panel

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

    def _collect(self):
        topo = self.topo
        ccds = {}
        for ccd_id in topo.get_all_ccd_ids():
            cpus = topo.get_ccd_cpus(ccd_id)
            freqs = {c: (topo.get_cpu_freq(c) if topo.is_cpu_online(c) else None)
                     for c in cpus}
            ccds[ccd_id] = {"cpus": sorted(cpus), "freqs": freqs}

        self.gpu.update()  # slow: nvidia-smi + 3x nvidia-settings

        return {
            "cores": topo.online_core_count(),
            "freq0": topo.get_cpu_freq(0),
            "temp": topo.get_temp(),  # slow: `sensors`
            "gov": topo.get_governor(),
            "game_mode": topo.game_mode_active(),
            "parked": topo.get_parked_ccds(),
            "keep": topo.keep_ccd(),
            "ccd_count": topo.ccd_count(),
            "complete": topo.complete,
            "ccds": ccds,
        }

    def _render(self, d):
        cores = d["cores"]
        self.lbl_threads.set_label(str(cores))
        self.lbl_freq.set_label(f"{d['freq0']}")
        self.lbl_temp.set_label(f"{d['temp']:.0f}")
        self.lbl_governor.set_label(d["gov"])

        # Game mode banner + button
        if d["game_mode"]:
            parked = ", ".join(f"CCD{c}" for c in d["parked"])
            self.banner.set_markup(
                f"<span color='#e0af68' weight='600'>GAME MODE - {parked} parked, "
                f"{cores} cores active</span>")
            self.banner.add_css_class("banner-gaming")
            self.banner.remove_css_class("banner-normal")
            self.gm_btn.set_label("Disable Game Mode")
            self.gm_btn.remove_css_class("btn-game-on")
            self.gm_btn.add_css_class("btn-game-off")
            self.status_footer_lbl.set_markup(
                "<span color='#e0af68'>  Game Mode ON</span>")
        else:
            self.banner.set_markup(
                f"<span color='#9ece6a' weight='600'>NORMAL - {cores} cores active</span>")
            self.banner.add_css_class("banner-normal")
            self.banner.remove_css_class("banner-gaming")
            self.gm_btn.set_label("Enable Game Mode")
            self.gm_btn.add_css_class("btn-game-on")
            self.gm_btn.remove_css_class("btn-game-off")
            self.status_footer_lbl.set_markup(
                f"<span color='#9ece6a'>  {cores} cores active</span>")

        # Game Mode needs something to park — a single-CCD CPU has nothing.
        single_ccd = d["ccd_count"] < 2
        self.gm_btn.set_sensitive(not single_ccd and d["complete"])
        if single_ccd:
            self.gm_btn.set_tooltip_text(
                "Game Mode needs a CPU with 2 or more CCDs (Ryzen 9 / Threadripper)")
        elif not d["complete"]:
            self.gm_btn.set_tooltip_text(
                "CPU layout unknown while cores are parked — restore all cores first")
        else:
            keep = d["keep"]
            self.gm_btn.set_tooltip_text(
                f"Parks every CCD except CCD{keep} "
                f"({self.topo.core_count(keep)} cores stay active)")

        # CCD cards
        for ccd_id, card in self.ccd_cards.items():
            info = d["ccds"].get(ccd_id)
            if not info:
                continue
            cpus = info["cpus"]
            freqs = info["freqs"]
            online = sum(1 for c in cpus if freqs[c] is not None)

            card.remove_css_class("ccd-card-active")
            card.remove_css_class("ccd-card-parked")
            if online == 0:
                card.add_css_class("ccd-card-parked")
                card._badge.set_label("PARKED")
                card._badge.add_css_class("badge-parked")
                card._badge.remove_css_class("badge-active")
            else:
                card.add_css_class("ccd-card-active")
                label = "ACTIVE"
                if d["ccd_count"] > 1 and ccd_id == d["keep"]:
                    label = "ACTIVE · KEEP"
                if online < len(cpus):
                    label = f"PARTIAL ({online}/{len(cpus)})"
                card._badge.set_label(label)
                card._badge.add_css_class("badge-active")
                card._badge.remove_css_class("badge-parked")

            # Core dots
            child = card._cores.get_first_child()
            for cpu in cpus:
                if child is None:
                    break
                child.remove_css_class("core-on")
                child.remove_css_class("core-off")
                child.remove_css_class("core-dot-boost")
                f = freqs[cpu]
                if f is None:
                    child.add_css_class("core-off")
                elif f > 4200:
                    child.add_css_class("core-dot-boost")
                else:
                    child.add_css_class("core-on")
                child = child.get_next_sibling()

            live = [f for f in freqs.values() if f is not None]
            card._avg_freq.set_label(
                f"Avg {sum(live) // len(live)} MHz" if live else "parked")

        # GPU
        self.gpu_name_lbl.set_markup(
            f"<span size='14' weight='bold' color='#c0caf5'>{self.gpu.name or 'NVIDIA GPU'}</span>")
        self.gpu_clock_lbl.set_label(str(self.gpu.clock_gr))
        self.gpu_mem_lbl.set_label(str(self.gpu.clock_mem))
        self.gpu_power_lbl.set_label(f"{self.gpu.power_draw:.0f}W")
        self.gpu_temp_lbl.set_label(f"{self.gpu.temp:.0f}")
        self.gpu_vram_lbl.set_label(str(self.gpu.vram_used))
        self.gpu_info_lbl.set_label(
            f"P-State: {self.gpu.pstate}  |  Util: {self.gpu.util}%  |  "
            f"Power Limit: {self.gpu.power_limit:.0f}W"
        )
        if self.gpu.max_clock_gr > 0:
            self.gpu_clock_bar.set_fraction(
                min(self.gpu.clock_gr / self.gpu.max_clock_gr, 1.0))

        # OC — only mirror the driver's values while the user is not editing,
        # otherwise the next tick would yank the slider back mid-drag.
        if not self._oc_touched:
            self._syncing_oc = True
            self.gr_slider.set_value(self.gpu.gr_offset)
            self.mem_slider.set_value(self.gpu.mem_offset)
            self.gr_value_lbl.set_label(f"{self.gpu.gr_offset:+d} MHz")
            self.mem_value_lbl.set_label(f"{self.gpu.mem_offset:+d} MHz")
            self.pm_combo.set_selected(self.gpu.powermizer)
            self._syncing_oc = False

        return False

    # ============================================================
    # Game Mode Toggle
    # ============================================================
    def on_toggle_gm(self, btn):
        if not self.topo.complete:
            self.gm_status_lbl.set_markup(
                "<span color='#f7768e'>CPU layout unknown — click 'Restore all cores' first</span>")
            return

        active = self.topo.game_mode_active()
        keep = self.topo.keep_ccd()
        plan = self.topo.park_plan(keep)

        if not active and not plan:
            self.gm_status_lbl.set_markup(
                "<span color='#f7768e'>Nothing to park — this CPU has only one CCD</span>")
            return

        self.gm_btn.set_sensitive(False)
        self.gm_btn.set_label("Please wait...")

        def run_in_thread():
            if active:
                ok, err = CCDController.unpark_all()
                msg = "All cores restored" if ok else err
            else:
                ok, err = CCDController.park(plan)
                msg = (f"Game Mode on — CCD{keep} kept, {len(plan)} threads parked"
                       if ok else err)
            # The kernel needs a moment before sysfs reflects the new state.
            time.sleep(0.5)

            def update_ui():
                self.gm_btn.set_sensitive(True)
                color = "#9ece6a" if ok else "#f7768e"
                self.gm_status_lbl.set_markup(f"<span color='{color}'>{GLib.markup_escape_text(msg)}</span>")
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
    # GPU Overclocking
    # ============================================================
    def on_gr_slider(self, slider):
        v = int(slider.get_value())
        self.gr_value_lbl.set_label(f"{v:+d} MHz")
        if not self._syncing_oc:
            self._oc_touched = True

    def on_mem_slider(self, slider):
        v = int(slider.get_value())
        self.mem_value_lbl.set_label(f"{v:+d} MHz")
        if not self._syncing_oc:
            self._oc_touched = True

    def on_apply_oc(self, btn):
        gr_off = int(self.gr_slider.get_value())
        mem_off = int(self.mem_slider.get_value())
        pm = self.pm_combo.get_selected()
        btn.set_sensitive(False)

        def apply_in_thread():
            ok = True
            if gr_off != self.gpu.gr_offset:
                ok = GPUController.set_gr_offset(gr_off)
            if mem_off != self.gpu.mem_offset:
                ok = ok and GPUController.set_mem_offset(mem_off)
            GPUController.set_powermizer(pm)
            self.gpu.update_oc()  # read back what the driver actually accepted

            def update_ui():
                btn.set_sensitive(True)
                self._oc_touched = False  # let the monitor mirror the driver again
                if ok:
                    self.oc_status_lbl.set_markup(
                        f"<span color='#9ece6a'>Core {self.gpu.gr_offset:+d} MHz | "
                        f"VRAM {self.gpu.mem_offset:+d} MHz | "
                        f"PM: {['Adaptive','Max Perf','Auto'][pm]}</span>")
                else:
                    self.oc_status_lbl.set_markup(
                        "<span color='#f7768e'>Error - Coolbits enabled?</span>")
                self.refresh()
                return False

            GLib.idle_add(update_ui)

        threading.Thread(target=apply_in_thread, daemon=True).start()

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
            self.bench_btn.set_label("Run CCD Benchmark")
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
        self.bench_btn.set_label("Benchmark running...")
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
        self.bench_btn.set_label("Run CCD Benchmark")
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
# Application
# ============================================================
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.gaming.commandcenter")

    def do_activate(self):
        win = CommandCenter(self)
        win.present()


if __name__ == "__main__":
    app = App()
    app.run(None)