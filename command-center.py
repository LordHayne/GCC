#!/usr/bin/env python3
"""
Gaming Command Center — Kommandozentrale für AMD Ryzen + NVIDIA GPU
Dark themed GUI with CCD-Parking, GPU-OC, and Live Monitoring.
Now with Setup Wizard tab for system checks.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk, GObject
import subprocess, os, re, threading, time
from system_scanner import scan_system

# ============================================================
# CPU Topology
# ============================================================
class CPUTopology:
    def __init__(self):
        self.ccds = {}
        self.detect()

    def detect(self):
        self.ccds = {}
        cache_ids = {}
        for cpu in range(128):
            path = f"/sys/devices/system/cpu/cpu{cpu}/cache/index3/id"
            try:
                with open(path) as f:
                    cid = int(f.read().strip())
                if cid not in cache_ids:
                    cache_ids[cid] = []
                cache_ids[cid].append(cpu)
            except:
                continue
        sorted_ids = sorted(cache_ids.keys())
        ccd_id = 0
        prev_id = -2
        for cid in sorted_ids:
            if cid > prev_id + 1:
                ccd_id += 1
            if ccd_id not in self.ccds:
                self.ccds[ccd_id] = []
            self.ccds[ccd_id].extend(cache_ids[cid])
            prev_id = cid
        self.ccds = {k: sorted(v) for k, v in sorted(self.ccds.items())}

    def get_ccd_cpus(self, ccd_id):
        return self.ccds.get(ccd_id, [])

    def get_all_ccd_ids(self):
        return sorted(self.ccds.keys())

    def ccd_count(self):
        return len(self.ccds)

    def is_cpu_online(self, cpu):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/online") as f:
                return f.read().strip() == "1"
        except:
            return True

    def get_cpu_freq(self, cpu):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq") as f:
                return int(f.read().strip()) // 1000
        except:
            return 0

    def get_governor(self):
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as f:
                return f.read().strip()
        except:
            return "?"

    def get_temp(self):
        try:
            r = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.split("\n"):
                if "Tctl:" in line:
                    m = re.search(r'([+-]?[\d.]+)°C', line)
                    if m: return float(m.group(1))
        except:
            pass
        return 0.0

    def get_game_mode(self):
        """Check if CCD1 is parked by testing CPU6 directly.
        Don't rely on self.ccds — offline CPUs disappear from topology."""
        try:
            with open("/sys/devices/system/cpu/cpu6/online") as f:
                return f.read().strip() == "0"
        except:
            # CPU6 might not exist (non-2-CCD CPU) — check total CPU count
            return False

    def get_online_count(self):
        """Count online CPU cores (SMT threads / 2 = physical cores).
        Reads sysfs directly — don't rely on self.ccds which loses CCD1 when parked."""
        threads = 0
        for cpu in range(128):
            online_path = f"/sys/devices/system/cpu/cpu{cpu}/online"
            try:
                with open(online_path) as f:
                    if f.read().strip() == "1":
                        threads += 1
            except FileNotFoundError:
                # CPU0 has no 'online' file but is always online
                # Check if the CPU directory exists
                if os.path.isdir(f"/sys/devices/system/cpu/cpu{cpu}"):
                    threads += 1
                else:
                    break  # No more CPUs
        # SMT: 2 threads per core → physical cores = threads / 2
        smt = True
        try:
            with open("/sys/devices/system/cpu/smt/control") as f:
                smt = f.read().strip() != "off"
        except:
            pass
        if smt and threads > 1:
            return threads // 2
        return threads

    def get_total_count(self):
        """Total CPU count (including offline)"""
        return subprocess.run(["nproc", "--all"], capture_output=True, text=True).stdout.strip()

    def get_cpu_name(self):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
        except:
            pass
        return "Unknown CPU"


# ============================================================
# GPU Info (NVIDIA)
# ============================================================
class GPUInfo:
    def __init__(self):
        self.update()

    def update(self):
        self.name = ""
        self.vram_total = self.vram_used = 0
        self.power_draw = self.power_limit = self.temp = 0.0
        self.clock_gr = self.clock_mem = self.max_clock_gr = self.max_clock_mem = 0
        self.pstate = ""
        self.util = 0
        self.gr_offset = self.mem_offset = 0
        self.powermizer = 0
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
        try:
            r = subprocess.run(["nvidia-settings", "-q", "GPUGraphicsClockOffsetAllPerformanceLevels"],
                              capture_output=True, text=True, timeout=2)
            m = re.search(r'\): (-?\d+)', r.stdout)
            if m: self.gr_offset = int(m.group(1))
        except: pass
        try:
            r = subprocess.run(["nvidia-settings", "-q", "GPUMemoryTransferRateOffsetAllPerformanceLevels"],
                              capture_output=True, text=True, timeout=2)
            m = re.search(r'\): (-?\d+)', r.stdout)
            if m: self.mem_offset = int(m.group(1))
        except: pass
        try:
            r = subprocess.run(["nvidia-settings", "-q", "GPUPowerMizerMode"],
                              capture_output=True, text=True, timeout=2)
            m = re.search(r'\): (\d+)', r.stdout)
            if m: self.powermizer = int(m.group(1))
        except: pass


# ============================================================
# Controllers
# ============================================================
class CCDController:
    HELPER = "/usr/local/bin/gaming-ccd-helper"

    @staticmethod
    def park():
        try:
            r = subprocess.run(["pkexec", CCDController.HELPER, "on"],
                              capture_output=True, text=True, timeout=60)
            return "DONE_ON" in (r.stdout or "")
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False

    @staticmethod
    def unpark():
        try:
            r = subprocess.run(["pkexec", CCDController.HELPER, "off"],
                              capture_output=True, text=True, timeout=60)
            return "DONE_OFF" in (r.stdout or "")
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False


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
# Setup Wizard Tab
# ============================================================
class SetupWizard(Gtk.Box):
    """Setup Wizard tab — runs system_scanner.scan_system() and displays results."""

    def __init__(self, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)

        # Scan button at top
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.set_margin_start(12)
        top_bar.set_margin_end(12)
        top_bar.set_margin_top(12)
        top_bar.set_margin_bottom(8)

        self.scan_btn = Gtk.Button(label="🔍 Scan System")
        self.scan_btn.add_css_class("btn-apply")
        self.scan_btn.connect("clicked", self.on_scan_clicked)
        top_bar.append(self.scan_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top_bar.append(spacer)

        self.scan_status_lbl = Gtk.Label(label="")
        self.scan_status_lbl.add_css_class("stat-label")
        top_bar.append(self.scan_status_lbl)

        self.append(top_bar)

        # Separator
        self.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Scrolled area for results
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_vexpand(True)
        self.scroll.set_propagate_natural_height(False)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results_box.set_margin_start(6)
        self.results_box.set_margin_end(6)
        self.results_box.set_margin_top(8)
        self.results_box.set_margin_bottom(8)

        # Placeholder before first scan
        self.placeholder = Gtk.Label(label='Click "Scan System" to start')
        self.placeholder.set_markup("<span color='#565f89' size='13'>Click \"Scan System\" to start</span>")
        self.placeholder.set_margin_top(40)
        self.results_box.append(self.placeholder)

        clamp.set_child(self.results_box)
        self.scroll.set_child(clamp)
        self.append(self.scroll)

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
        self.scan_btn.set_label("⏳ Scanning...")
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
        self.scan_btn.set_label("🔍 Scan System")
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
                f"<span color='#e0af68' weight='bold'>⚠️ {summary_text}</span>")
        elif ok_count > 0:
            self.summary_lbl.set_markup(
                f"<span color='#9ece6a' weight='bold'>✅ {summary_text}</span>")
        else:
            self.summary_lbl.set_markup(
                f"<span color='#7aa2f7' weight='bold'>ℹ️ {summary_text}</span>")

    def _build_check_row(self, check):
        """Build a single check result row."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_start(10)
        row.set_margin_end(10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)

        # Status icon
        if check.status == "ok":
            icon_text = "✅"
            icon_color = "#9ece6a"
        elif check.status == "warning":
            icon_text = "⚠️"
            icon_color = "#e0af68"
        else:
            icon_text = "ℹ️"
            icon_color = "#7aa2f7"

        icon_lbl = Gtk.Label(label=icon_text)
        icon_lbl.set_markup(f"<span color='{icon_color}' size='18'>{icon_text}</span>")
        icon_lbl.set_valign(Gtk.Align.START)
        icon_lbl.set_margin_top(2)
        row.append(icon_lbl)

        # Name + message column
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        name_lbl = Gtk.Label(label=check.name)
        name_lbl.set_markup(f"<span color='#c0caf5' weight='bold' size='13'>{check.name}</span>")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_xalign(0)
        text_box.append(name_lbl)

        msg_lbl = Gtk.Label(label=check.message)
        msg_lbl.set_markup(f"<span color='#a9b1d6' size='11'>{check.message}</span>")
        msg_lbl.set_halign(Gtk.Align.START)
        msg_lbl.set_xalign(0)
        msg_lbl.set_wrap(True)
        text_box.append(msg_lbl)

        # Fix message if present
        if check.fix_message and check.status == "warning":
            fix_lbl = Gtk.Label(label=check.fix_message)
            fix_lbl.set_markup(f"<span color='#e0af68' size='10'>→ {check.fix_message}</span>")
            fix_lbl.set_halign(Gtk.Align.START)
            fix_lbl.set_xalign(0)
            fix_lbl.set_wrap(True)
            text_box.append(fix_lbl)

        row.append(text_box)

        # Fix button for warnings
        if check.status == "warning" and check.fix_message:
            fix_btn = Gtk.Button(label="Apply Fix")
            fix_btn.add_css_class("btn-apply")
            fix_btn.set_valign(Gtk.Align.CENTER)
            fix_btn.set_margin_start(8)
            fix_btn.connect("clicked", self.on_fix_clicked, check)
            row.append(fix_btn)

        return row

    def on_fix_clicked(self, btn, check):
        """Non-functional for now — just visual placeholder."""
        btn.set_label("⏳...")
        btn.set_sensitive(False)
        GLib.timeout_add(1500, lambda: [btn.set_label("Apply Fix"), btn.set_sensitive(True), False][2])


# ============================================================
# Main Window
# ============================================================
class CommandCenter(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Gaming Command Center")
        self.set_default_size(680, 720)
        # Set window icon
        try:
            pixbuf = Gdk.pixbuf_new_from_file("/home/thomas/.local/share/icons/hicolor/256x256/apps/gaming-command-center.png")
            self.set_icon(pixbuf)
        except:
            pass
        self.topo = CPUTopology()
        self.gpu = GPUInfo()
        self.benching = False
        self.best_ccd = None

        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        css = """
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

        /* CCD cards — simplified */
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

        .gpu-card {
            background: #1f2335;
            border-radius: 14px;
            padding: 16px;
            border: 1px solid rgba(122,162,247,0.08);
        }

        progressbar trough { background: #1a1b26; border-radius: 4px; min-height: 6px; }
        progressbar progress { background: #7aa2f7; border-radius: 4px; }

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

        scale trough { background: #1a1b26; min-height: 6px; border-radius: 3px; }
        scale highlight { background: #7aa2f7; border-radius: 3px; }
        scale slider {
            background: #7aa2f7; min-width: 16px; min-height: 16px;
            border-radius: 50%; box-shadow: 0 0 8px rgba(122,162,247,0.3);
        }

        dropdown {
            background: #1a1b26; border-radius: 8px;
            border: 1px solid rgba(255,255,255,0.06);
        }

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

        /* Benchmark progress bar */
        .bench-progress { margin-top: 6px; }

        /* Scroll fix: prevent scroll from changing sliders */
        scale { margin-top: 4px; margin-bottom: 4px; }

        separator { background: rgba(255,255,255,0.04); }

        /* Setup Wizard styling */
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
            padding: 10px 16px;
            border-radius: 10px;
            margin-top: 8px;
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
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.build_ui()
        self.refresh()
        GLib.timeout_add(1500, self.refresh)

    def build_ui(self):
        # Main vertical box for the whole window
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        self.set_content(main_box)
        main_box.append(header)

        # Adw.ViewSwitcher + Adw.ViewStack for tabbed interface
        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

        # --- Dashboard page ---
        dashboard_page = self.view_stack.add_titled(
            self._build_dashboard_content(), "dashboard", "Dashboard")

        # --- Setup Wizard page ---
        self.setup_wizard = SetupWizard()
        wizard_page = self.view_stack.add_titled(
            self.setup_wizard, "wizard", "Setup Wizard")

        # ViewSwitcher bar (in header area, below headerbar)
        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self.view_stack)
        switcher_bar.set_reveal(True)

        main_box.append(self.view_stack)
        main_box.append(switcher_bar)

    def _build_dashboard_content(self):
        """Build the existing Dashboard UI (formerly build_ui content)."""
        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_propagate_natural_height(False)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.set_margin_start(6)
        content.set_margin_end(6)
        content.set_margin_bottom(20)
        clamp.set_child(content)
        scroll.set_child(clamp)

        # CPU name
        cpu_name = self.topo.get_cpu_name()
        name_lbl = Gtk.Label(label=cpu_name)
        name_lbl.set_markup(f"<span size='16' weight='bold' color='#c0caf5'>{cpu_name}</span>")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_margin_top(8)
        content.append(name_lbl)

        # Status banner
        self.banner = Gtk.Label(label="")
        self.banner.set_halign(Gtk.Align.START)
        content.append(self.banner)

        # CPU stats
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        stats.set_margin_top(8)
        stats.set_homogeneous(True)
        self.lbl_threads = self._stat_tile(stats, "Cores", "24", "blue")
        self.lbl_freq = self._stat_tile(stats, "Clock", "---", "orange")
        self.lbl_temp = self._stat_tile(stats, "Temp", "--°", "green")
        content.append(stats)

        # CCD section
        content.append(self._section_header("CPU CCDs"))
        self.ccd_cards = {}
        for ccd_id in self.topo.get_all_ccd_ids():
            card = self._build_ccd_card(ccd_id)
            content.append(card)
            self.ccd_cards[ccd_id] = card

        # Game Mode button
        self.gm_btn = Gtk.Button(label="🎮 Enable Game Mode")
        self.gm_btn.set_margin_top(8)
        self.gm_btn.add_css_class("btn-game-on")
        self.gm_btn.connect("clicked", self.on_toggle_gm)
        content.append(self.gm_btn)

        # Benchmark
        self.bench_btn = Gtk.Button(label="⚡ Run CCD Benchmark")
        self.bench_btn.set_margin_top(4)
        self.bench_btn.add_css_class("btn-bench")
        self.bench_btn.connect("clicked", self.on_benchmark)
        content.append(self.bench_btn)

        # Benchmark progress bar (hidden by default)
        self.bench_progress = Gtk.ProgressBar()
        self.bench_progress.set_margin_top(4)
        self.bench_progress.set_visible(False)
        content.append(self.bench_progress)

        self.bench_label = Gtk.Label(label="")
        self.bench_label.set_wrap(True)
        self.bench_label.set_margin_top(4)
        content.append(self.bench_label)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # GPU section
        content.append(self._section_header("🎨 NVIDIA GPU"))
        content.append(self._build_gpu_card())

        return scroll

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

    def _build_ccd_card(self, ccd_id):
        """Simplified CCD card: just name, badge, core dots — no per-core freq labels"""
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

        count_lbl = Gtk.Label(label=f"{len(cpus) // 2} Cores")
        count_lbl.add_css_class("stat-label")
        title_row.append(count_lbl)
        card.append(title_row)

        # Core dots — clean, no freq numbers
        cores_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cores_box.set_margin_top(2)
        for cpu in sorted(cpus):
            dot = Gtk.Box()
            dot.set_size_request(12, 12)
            dot.add_css_class("core-dot")
            dot.set_tooltip_text(f"CPU {cpu}")
            cores_box.append(dot)
        card.append(cores_box)

        # Single avg freq line (not per-core)
        avg_freq_lbl = Gtk.Label(label="---")
        avg_freq_lbl.set_halign(Gtk.Align.START)
        avg_freq_lbl.add_css_class("stat-label")
        card.append(avg_freq_lbl)

        card._badge = badge
        card._cores = cores_box
        card._avg_freq = avg_freq_lbl
        return card

    def _build_gpu_card(self):
        gpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        gpu_card.add_css_class("gpu-card")
        gpu_card.set_margin_top(4)

        self.gpu_name_lbl = Gtk.Label(label=self.gpu.name)
        self.gpu_name_lbl.set_markup(f"<span size='14' weight='bold' color='#c0caf5'>{self.gpu.name}</span>")
        self.gpu_name_lbl.set_halign(Gtk.Align.START)
        gpu_card.append(self.gpu_name_lbl)

        gpu_stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        gpu_stats.set_homogeneous(True)
        gpu_stats.set_margin_top(6)
        self.gpu_clock_lbl = self._stat_tile(gpu_stats, "Core", "---", "blue")
        self.gpu_mem_lbl = self._stat_tile(gpu_stats, "Memory", "---", "")
        self.gpu_power_lbl = self._stat_tile(gpu_stats, "Power", "---", "orange")
        self.gpu_temp_lbl = self._stat_tile(gpu_stats, "Temp", "---", "green")
        gpu_card.append(gpu_stats)

        self.gpu_info_lbl = Gtk.Label(label="")
        self.gpu_info_lbl.set_halign(Gtk.Align.START)
        self.gpu_info_lbl.add_css_class("stat-label")
        self.gpu_info_lbl.set_margin_top(4)
        gpu_card.append(self.gpu_info_lbl)

        self.gpu_clock_bar = Gtk.ProgressBar()
        self.gpu_clock_bar.set_margin_top(6)
        gpu_card.append(self.gpu_clock_bar)

        # OC Section
        oc_title = Gtk.Label(label="Overclocking")
        oc_title.set_markup("<span weight='bold' color='#7aa2f7' size='12'>⚡ Overclocking</span>")
        oc_title.set_halign(Gtk.Align.START)
        oc_title.set_margin_top(10)
        gpu_card.append(oc_title)

        # Core offset
        gr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gr_row.set_margin_top(6)
        gr_lbl = Gtk.Label(label="Core")
        gr_lbl.set_markup("<span color='#c0caf5'>Core</span>")
        gr_row.append(gr_lbl)
        self.gr_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.gr_slider.set_hexpand(True)
        self.gr_slider.set_value(0)
        self.gr_slider.connect("value-changed", self.on_gr_slider)
        # FIX: block mouse wheel from changing slider
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

        # VRAM offset
        mem_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mem_row.set_margin_top(4)
        mem_lbl = Gtk.Label(label="VRAM")
        mem_lbl.set_markup("<span color='#c0caf5'>VRAM</span>")
        mem_row.append(mem_lbl)
        self.mem_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.mem_slider.set_hexpand(True)
        self.mem_slider.set_value(0)
        self.mem_slider.connect("value-changed", self.on_mem_slider)
        # FIX: block mouse wheel from changing slider
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

        # PowerMizer
        pm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pm_row.set_margin_top(6)
        pm_lbl = Gtk.Label(label="PowerMizer")
        pm_lbl.set_markup("<span color='#c0caf5'>PowerMizer</span>")
        pm_row.append(pm_lbl)
        self.pm_combo = Gtk.DropDown.new_from_strings(["Adaptive", "Max Performance", "Auto"])
        self.pm_combo.set_selected(0)
        pm_row.append(self.pm_combo)
        gpu_card.append(pm_row)

        # Apply button
        self.oc_btn = Gtk.Button(label="⚡ Apply OC")
        self.oc_btn.set_margin_top(8)
        self.oc_btn.add_css_class("btn-apply")
        self.oc_btn.connect("clicked", self.on_apply_oc)
        gpu_card.append(self.oc_btn)

        self.oc_status_lbl = Gtk.Label(label="")
        self.oc_status_lbl.set_margin_top(4)
        self.oc_status_lbl.set_halign(Gtk.Align.START)
        gpu_card.append(self.oc_status_lbl)

        return gpu_card

    def refresh(self):
        self.lbl_threads.set_label(str(self.topo.get_online_count()))
        freq = self.topo.get_cpu_freq(0)
        self.lbl_freq.set_label(f"{freq}")
        temp = self.topo.get_temp()
        self.lbl_temp.set_label(f"{temp:.0f}°")

        gm = self.topo.get_game_mode()
        if gm:
            self.banner.set_markup(
                "<span color='#e0af68' weight='600'>🎮 GAME MODE — CCD1 parked, 6 cores active</span>")
            self.banner.add_css_class("banner-gaming")
            self.banner.remove_css_class("banner-normal")
            self.gm_btn.set_label("🟢 Disable Game Mode")
            self.gm_btn.remove_css_class("btn-game-on")
            self.gm_btn.add_css_class("btn-game-off")
        else:
            self.banner.set_markup(
                f"<span color='#9ece6a' weight='600'>🟢 NORMAL — {self.topo.get_online_count()} cores active</span>")
            self.banner.add_css_class("banner-normal")
            self.banner.remove_css_class("banner-gaming")
            self.gm_btn.set_label("🎮 Enable Game Mode")
            self.gm_btn.add_css_class("btn-game-on")
            self.gm_btn.remove_css_class("btn-game-off")

        # CCD cards — simplified, no per-core freq spam
        for ccd_id, card in self.ccd_cards.items():
            cpus = self.topo.get_ccd_cpus(ccd_id)
            online = sum(1 for c in cpus if self.topo.is_cpu_online(c))

            card.remove_css_class("ccd-card-active")
            card.remove_css_class("ccd-card-parked")
            if online == len(cpus):
                card.add_css_class("ccd-card-active")
                card._badge.set_label("ACTIVE")
                card._badge.add_css_class("badge-active")
                card._badge.remove_css_class("badge-parked")
            else:
                card.add_css_class("ccd-card-parked")
                card._badge.set_label("PARKED")
                card._badge.add_css_class("badge-parked")
                card._badge.remove_css_class("badge-active")

            # Dots
            i = 0
            child = card._cores.get_first_child()
            while child:
                cpu = sorted(cpus)[i]
                child.remove_css_class("core-on")
                child.remove_css_class("core-off")
                child.remove_css_class("core-dot-boost")
                if self.topo.is_cpu_online(cpu):
                    f = self.topo.get_cpu_freq(cpu)
                    if f > 4200:
                        child.add_css_class("core-dot-boost")
                    else:
                        child.add_css_class("core-on")
                else:
                    child.add_css_class("core-off")
                child = child.get_next_sibling()
                i += 1

            # Single avg freq (not per-core)
            if online > 0:
                freqs = [self.topo.get_cpu_freq(c) for c in cpus if self.topo.is_cpu_online(c)]
                avg = sum(freqs) // len(freqs) if freqs else 0
                card._avg_freq.set_label(f"Ø {avg} MHz")
            else:
                card._avg_freq.set_label("parked")

        # GPU
        self.gpu.update()
        self.gpu_name_lbl.set_markup(f"<span size='14' weight='bold' color='#c0caf5'>{self.gpu.name}</span>")
        self.gpu_clock_lbl.set_label(str(self.gpu.clock_gr))
        self.gpu_mem_lbl.set_label(str(self.gpu.clock_mem))
        self.gpu_power_lbl.set_label(f"{self.gpu.power_draw:.0f}W")
        self.gpu_temp_lbl.set_label(f"{self.gpu.temp:.0f}°")
        self.gpu_info_lbl.set_label(
            f"P-State: {self.gpu.pstate}  |  VRAM: {self.gpu.vram_used}/{self.gpu.vram_total} MB  |  "
            f"Util: {self.gpu.util}%  |  Power Limit: {self.gpu.power_limit:.0f}W"
        )
        if self.gpu.max_clock_gr > 0:
            self.gpu_clock_bar.set_fraction(min(self.gpu.clock_gr / self.gpu.max_clock_gr, 1.0))

        # OC
        self.gr_slider.set_value(self.gpu.gr_offset)
        self.mem_slider.set_value(self.gpu.mem_offset)
        self.gr_value_lbl.set_label(f"{self.gpu.gr_offset:+d} MHz")
        self.mem_value_lbl.set_label(f"{self.gpu.mem_offset:+d} MHz")
        self.pm_combo.set_selected(self.gpu.powermizer)

        return True

    def on_toggle_gm(self, btn):
        gm = self.topo.get_game_mode()
        self.gm_btn.set_sensitive(False)
        self.gm_btn.set_label("⏳ Please wait...")

        def run_in_thread():
            if gm:
                CCDController.unpark()
                # Wait for CPUs to come back online
                time.sleep(1.5)
            else:
                CCDController.park()
                time.sleep(0.5)

            def update_ui():
                self.gm_btn.set_sensitive(True)
                self.refresh()

            GLib.idle_add(update_ui)

        threading.Thread(target=run_in_thread, daemon=True).start()

    def on_gr_slider(self, slider):
        v = int(slider.get_value())
        self.gr_value_lbl.set_label(f"{v:+d} MHz")

    def on_mem_slider(self, slider):
        v = int(slider.get_value())
        self.mem_value_lbl.set_label(f"{v:+d} MHz")

    def on_apply_oc(self, btn):
        gr_off = int(self.gr_slider.get_value())
        mem_off = int(self.mem_slider.get_value())
        pm = self.pm_combo.get_selected()

        ok = True
        if gr_off != self.gpu.gr_offset:
            ok = GPUController.set_gr_offset(gr_off)
        if mem_off != self.gpu.mem_offset:
            ok = ok and GPUController.set_mem_offset(mem_off)
        GPUController.set_powermizer(pm)

        if ok:
            self.oc_status_lbl.set_markup(
                f"<span color='#9ece6a'>✅ Core {gr_off:+d} MHz | VRAM {mem_off:+d} MHz | "
                f"PM: {['Adaptive','Max Perf','Auto'][pm]}</span>")
        else:
            self.oc_status_lbl.set_markup(
                "<span color='#f7768e'>❌ Error — Coolbits enabled?</span>")
        GLib.timeout_add(1000, self.refresh)

    def on_benchmark(self, btn):
        if self.benching: return
        self.benching = True
        self.bench_btn.set_label("⚡ Benchmark running...")
        self.bench_btn.set_sensitive(False)
        self.bench_progress.set_visible(True)
        self.bench_progress.set_fraction(0.0)
        self.bench_label.set_markup(
            "<span color='#7aa2f7' weight='bold'>📊 Benchmark starting...</span>\n"
            "<span color='#565f89'>Testing each core individually (~25 seconds)</span>")

        # Collect all physical cores across CCDs
        # SMT pairs: CPU 0+12, 1+13, 2+14, etc. — physical core = the lower CPU number
        # For CCD0: cpus=[0-5,12-17] → physical=[0,1,2,3,4,5]
        # For CCD1: cpus=[6-11,18-23] → physical=[6,7,8,9,10,11]
        all_cores = []
        for ccd_id in self.topo.get_all_ccd_ids():
            cpus = sorted(self.topo.get_ccd_cpus(ccd_id))
            # Physical cores = CPUs that are < first SMT sibling offset
            # In a 24-thread CPU, threads 12-23 are SMT siblings of 0-11
            # So physical = CPUs where cpu < total_threads / 2 (i.e. < 12 for 24-thread)
            # But per-CCD: physical = first half of the CCD's CPU list
            mid = len(cpus) // 2
            physical = cpus[:mid]
            # Only include cores that are actually online
            online_physical = [c for c in physical if self.topo.is_cpu_online(c)]
            if online_physical:
                all_cores.append((ccd_id, online_physical))

        # If CCD1 disappeared from topology (parked), add it manually
        if len(all_cores) < 2:
            # Check if CPU 6 exists but is offline (parked CCD1)
            try:
                with open("/sys/devices/system/cpu/cpu6/online") as f:
                    if f.read().strip() == "0":
                        # CCD1 physical cores = CPUs 6-11
                        all_cores.append((1, [6, 7, 8, 9, 10, 11]))
            except:
                pass

        total_cores = sum(len(cores) for _, cores in all_cores)
        if total_cores == 0:
            self.benching = False
            self.bench_btn.set_label("⚡ Run CCD Benchmark")
            self.bench_btn.set_sensitive(True)
            self.bench_progress.set_visible(False)
            self.bench_label.set_markup("<span color='#f7768e'>No cores available for benchmark</span>")
            return

        cores_done = [0]
        all_results = {}

        def run_benchmark():
            """Run benchmark in background thread — updates UI via GLib.idle_add"""
            for ccd_id, physical in all_cores:
                results = {}
                for i, cpu in enumerate(physical):
                    # Update UI before each core
                    GLib.idle_add(lambda c=cpu, ccd=ccd_id, i=i, n=len(physical): self.bench_label.set_markup(
                        f"<span color='#7aa2f7' weight='bold'>📊 Benchmark: CCD{ccd_id} — "
                        f"Core {i+1}/{n} (CPU {c})</span>\n"
                        f"<span color='#565f89'>{cores_done[0]}/{total_cores} cores tested</span>"))
                    GLib.idle_add(lambda: self.bench_progress.set_fraction(cores_done[0] / total_cores))

                    try:
                        r = subprocess.run(
                            ["taskset", "-c", str(cpu), "openssl", "speed", "-elapsed",
                             "-seconds", "2", "aes-256-cbc"],
                            capture_output=True, text=True, timeout=15)
                        last = r.stdout.strip().split("\n")[-1] if r.stdout else ""
                        parts = last.split()
                        raw = parts[5] if len(parts) >= 6 else "0"
                        val_str = raw.rstrip("k").rstrip("K")
                        try:
                            results[cpu] = float(val_str)
                        except:
                            results[cpu] = 0.0
                    except:
                        results[cpu] = 0.0
                    cores_done[0] += 1

                all_results[ccd_id] = results

            # Done — update UI
            GLib.idle_add(lambda: self.bench_progress.set_fraction(1.0))
            GLib.idle_add(lambda: self._finish_benchmark(all_results))

        threading.Thread(target=run_benchmark, daemon=True).start()

    def _finish_benchmark(self, all_results):
        self.benching = False
        self.bench_btn.set_label("⚡ Run CCD Benchmark")
        self.bench_btn.set_sensitive(True)
        self.bench_progress.set_visible(False)
        self._show_bench(all_results)

    def _show_bench(self, all_results):
        text = "<span color='#7aa2f7' weight='bold'>📊 CCD Benchmark Results</span>\n\n"
        best_ccd = None
        best_avg = 0
        for ccd_id in sorted(all_results.keys()):
            results = all_results[ccd_id]
            if results:
                avg = sum(results.values()) / len(results)
                if avg > best_avg:
                    best_avg = avg
                    best_ccd = ccd_id

        for ccd_id in sorted(all_results.keys()):
            results = all_results[ccd_id]
            if results:
                avg = sum(results.values()) / len(results)
                marker = "🏆 " if ccd_id == best_ccd else "   "
                color = "#e0af68" if ccd_id == best_ccd else "#c0caf5"
                bar = "█" * int(avg / 50000) + "░" * (20 - int(avg / 50000))
                text += f"<span color='{color}'>{marker}CCD{ccd_id}: {bar} {avg:.0f} kB/s</span>\n"

        if best_ccd is not None:
            text += f"\n<span color='#e0af68' weight='bold'>🏆 CCD{best_ccd} is faster → keep for gaming!</span>"

        self.bench_label.set_markup(text)
        self.best_ccd = best_ccd

        for ccd_id, card in self.ccd_cards.items():
            if ccd_id == best_ccd:
                card.add_css_class("ccd-card-best")
                card._badge.set_label("BEST")
                card._badge.add_css_class("badge-best")
            else:
                card.remove_css_class("ccd-card-best")
                card._badge.remove_css_class("badge-best")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.gaming.commandcenter")

    def do_activate(self):
        win = CommandCenter(self)
        win.present()


if __name__ == "__main__":
    app = App()
    app.run(None)