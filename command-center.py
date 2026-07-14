#!/usr/bin/env python3
"""
Gaming Command Center — Kommandozentrale für AMD Ryzen + NVIDIA GPU
Sidebar-based layout with Dashboard, Game Doctor, Benchmark, and Settings pages.
Dark themed GUI with CCD-Parking, GPU-OC, Live Monitoring, and System Scanner.
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
        # Query max graphics clock for progress bar
        try:
            r2 = subprocess.run(["nvidia-smi", "--query-gpu=clocks.max.gr", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=2)
            val = r2.stdout.strip()
            if val and val != "[N/A]":
                self.max_clock_gr = int(float(val))
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
# Game Doctor Page (formerly Setup Wizard)
# ============================================================
class GameDoctorPage(Gtk.Box):
    """Game Doctor page — runs system_scanner.scan_system() and displays results."""

    def __init__(self, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)

        # Scan button at top
        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.set_margin_start(16)
        top_bar.set_margin_end(16)
        top_bar.set_margin_top(12)
        top_bar.set_margin_bottom(8)

        self.scan_btn = Gtk.Button(label="Scan System")
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
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(700)

        self.results_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.results_box.set_margin_start(10)
        self.results_box.set_margin_end(10)
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
            fix_btn.connect("clicked", self.on_fix_clicked, check)
            row.append(fix_btn)

        return row

    def on_fix_clicked(self, btn, check):
        """Apply the actual fix based on which check triggered it."""
        btn.set_label("Applying...")
        btn.set_sensitive(False)

        def apply_in_thread():
            ok = False
            msg = ""

            if check.name == "CPU Governor":
                try:
                    subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "governor"],
                                 capture_output=True, text=True, timeout=10)
                    for cpu in range(128):
                        try:
                            with open(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor", "w") as f:
                                f.write("powersave")
                        except:
                            break
                    ok = True
                    msg = "Governor set to powersave"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "CCD / Game Mode":
                try:
                    r = subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "on"],
                                     capture_output=True, text=True, timeout=30)
                    ok = "DONE_ON" in r.stdout
                    msg = "Game Mode activated! CCD1 parked" if ok else "Failed to park CCD1"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "Audio Power Save":
                try:
                    subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "audio"],
                                 capture_output=True, text=True, timeout=10)
                    ok = True
                    msg = "Audio Power Save enabled"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "GameMode":
                try:
                    subprocess.run(["pacman", "-S", "--noconfirm", "gamemode"],
                                 capture_output=True, text=True, timeout=60)
                    ok = True
                    msg = "GameMode installed"
                except:
                    msg = "Install manually: pacman -S gamemode"

            elif check.name == "gamescope":
                try:
                    subprocess.run(["pacman", "-S", "--noconfirm", "gamescope"],
                                 capture_output=True, text=True, timeout=60)
                    ok = True
                    msg = "gamescope installed"
                except:
                    msg = "Install manually: pacman -S gamescope"

            elif check.name == "SATA Link Power":
                try:
                    for i in range(4):
                        path = f"/sys/class/scsi_host/host{i}/link_power_management_policy"
                        import os as _os
                        if _os.exists(path):
                            subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "sata"],
                                         capture_output=True, text=True, timeout=10)
                            break
                    ok = True
                    msg = "SATA Link Power set to med_power_with_dipm"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "GameMode Config":
                try:
                    config_path = os.path.expanduser("~/.config/gamemode.ini")
                    config = """[general]
desiredgov=performance
renice=0
ioprio=0

[cpu]
park_cores=6-11,18-23
pin_cores=0-5,12-17

[gpu]
apply_gpu_optimisations=accept-responsibility
nv_powermizer_mode=1
"""
                    with open(config_path, "w") as f:
                        f.write(config)
                    ok = True
                    msg = "gamemode.ini created with CCD config"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "NVIDIA Modprobe":
                try:
                    subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "modprobe"],
                                 capture_output=True, text=True, timeout=10)
                    ok = True
                    msg = "NVIDIA modprobe config created"
                except Exception as e:
                    msg = f"Failed: {e}"

            elif check.name == "Coolbits / GPU-OC":
                try:
                    subprocess.run(["pkexec", "/usr/local/bin/gaming-ccd-helper", "coolbits"],
                                 capture_output=True, text=True, timeout=10)
                    ok = True
                    msg = "Coolbits enabled - restart X/Wayland to apply"
                except Exception as e:
                    msg = f"Failed: {e}"

            else:
                msg = "Fix not implemented yet for this check"

            def update_ui():
                if ok:
                    btn.set_label("Done")
                    btn.remove_css_class("btn-apply")
                    btn.add_css_class("btn-game-off")
                else:
                    btn.set_label("Failed")
                GLib.timeout_add(3000, lambda: [btn.set_label("Apply Fix"), btn.set_sensitive(True),
                                                 btn.remove_css_class("btn-game-off"), btn.add_css_class("btn-apply"),
                                                 False][2])

            GLib.idle_add(update_ui)

        threading.Thread(target=apply_in_thread, daemon=True).start()


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
        GLib.timeout_add(1500, self.refresh)

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

        # Game Doctor page
        self.game_doctor = GameDoctorPage()
        self.view_stack.add_titled(
            self.game_doctor, "doctor", "Game Doctor")

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
            ("Game Doctor", "doctor"),
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

        # CCD section
        left_col.append(self._section_header("CPU CCDs"))
        self.ccd_cards = {}
        for ccd_id in self.topo.get_all_ccd_ids():
            card = self._build_ccd_card(ccd_id)
            left_col.append(card)
            self.ccd_cards[ccd_id] = card

        # Game Mode button
        self.gm_btn = Gtk.Button(label="Enable Game Mode")
        self.gm_btn.set_margin_top(8)
        self.gm_btn.add_css_class("btn-game-on")
        self.gm_btn.connect("clicked", self.on_toggle_gm)
        left_col.append(self.gm_btn)

        content.append(left_col)

        # --- Right column: GPU monitoring + Overclocking ---
        right_col = self._build_gpu_panel()
        content.append(right_col)

        scroll.set_child(content)
        page.append(scroll)

        return page

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

        # Progress bar
        self.bench_progress = Gtk.ProgressBar()
        self.bench_progress.set_margin_top(8)
        self.bench_progress.set_visible(False)
        content.append(self.bench_progress)

        # Results label
        self.bench_label = Gtk.Label(label="")
        self.bench_label.set_wrap(True)
        self.bench_label.set_halign(Gtk.Align.START)
        self.bench_label.set_xalign(0)
        self.bench_label.set_margin_top(8)
        content.append(self.bench_label)

        scroll.set_child(content)
        page.append(scroll)

        return page

    # ============================================================
    # Settings Page
    # ============================================================
    def _build_settings_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        page.append(self._page_header("SETTINGS"))
        page.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Placeholder
        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        placeholder.set_valign(Gtk.Align.CENTER)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_vexpand(True)

        icon = Gtk.Label(label="")
        icon.set_markup("<span size='48000' color='#565f89'>\u2699</span>")
        placeholder.append(icon)

        coming = Gtk.Label(label="Coming soon")
        coming.set_markup(
            "<span size='16' weight='bold' color='#565f89'>Coming soon</span>")
        placeholder.append(coming)

        desc = Gtk.Label(label="")
        desc.set_markup(
            "<span size='11' color='#565f89'>Settings will be available in a future update</span>")
        placeholder.append(desc)

        page.append(placeholder)

        return page

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

        count_lbl = Gtk.Label(label=f"{len(cpus) // 2} Cores")
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
    def refresh(self):
        # CPU stats
        self.lbl_threads.set_label(str(self.topo.get_online_count()))
        freq = self.topo.get_cpu_freq(0)
        self.lbl_freq.set_label(f"{freq}")
        temp = self.topo.get_temp()
        self.lbl_temp.set_label(f"{temp:.0f}")
        gov = self.topo.get_governor()
        self.lbl_governor.set_label(gov)

        # Game mode banner + button
        gm = self.topo.get_game_mode()
        if gm:
            self.banner.set_markup(
                "<span color='#e0af68' weight='600'>GAME MODE - CCD1 parked, 6 cores active</span>")
            self.banner.add_css_class("banner-gaming")
            self.banner.remove_css_class("banner-normal")
            self.gm_btn.set_label("Disable Game Mode")
            self.gm_btn.remove_css_class("btn-game-on")
            self.gm_btn.add_css_class("btn-game-off")
            self.status_footer_lbl.set_markup(
                "<span color='#e0af68'>  Game Mode ON</span>")
        else:
            self.banner.set_markup(
                f"<span color='#9ece6a' weight='600'>NORMAL - {self.topo.get_online_count()} cores active</span>")
            self.banner.add_css_class("banner-normal")
            self.banner.remove_css_class("banner-gaming")
            self.gm_btn.set_label("Enable Game Mode")
            self.gm_btn.add_css_class("btn-game-on")
            self.gm_btn.remove_css_class("btn-game-off")
            self.status_footer_lbl.set_markup(
                f"<span color='#9ece6a'>  {self.topo.get_online_count()} cores active</span>")

        # CCD cards
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

            # Avg freq
            if online > 0:
                freqs = [self.topo.get_cpu_freq(c) for c in cpus if self.topo.is_cpu_online(c)]
                avg = sum(freqs) // len(freqs) if freqs else 0
                card._avg_freq.set_label(f"Avg {avg} MHz")
            else:
                card._avg_freq.set_label("parked")

        # GPU
        self.gpu.update()
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

        # OC
        self.gr_slider.set_value(self.gpu.gr_offset)
        self.mem_slider.set_value(self.gpu.mem_offset)
        self.gr_value_lbl.set_label(f"{self.gpu.gr_offset:+d} MHz")
        self.mem_value_lbl.set_label(f"{self.gpu.mem_offset:+d} MHz")
        self.pm_combo.set_selected(self.gpu.powermizer)

        return True

    # ============================================================
    # Game Mode Toggle
    # ============================================================
    def on_toggle_gm(self, btn):
        gm = self.topo.get_game_mode()
        self.gm_btn.set_sensitive(False)
        self.gm_btn.set_label("Please wait...")

        def run_in_thread():
            if gm:
                CCDController.unpark()
                time.sleep(1.5)
            else:
                CCDController.park()
                time.sleep(0.5)

            def update_ui():
                self.gm_btn.set_sensitive(True)
                self.refresh()

            GLib.idle_add(update_ui)

        threading.Thread(target=run_in_thread, daemon=True).start()

    # ============================================================
    # GPU Overclocking
    # ============================================================
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
                f"<span color='#9ece6a'>Core {gr_off:+d} MHz | VRAM {mem_off:+d} MHz | "
                f"PM: {['Adaptive','Max Perf','Auto'][pm]}</span>")
        else:
            self.oc_status_lbl.set_markup(
                "<span color='#f7768e'>Error - Coolbits enabled?</span>")
        GLib.timeout_add(1000, self.refresh)

    # ============================================================
    # CCD Benchmark
    # ============================================================
    def on_benchmark(self, btn):
        if self.benching:
            return
        self.benching = True
        self.bench_btn.set_label("Benchmark running...")
        self.bench_btn.set_sensitive(False)
        self.bench_progress.set_visible(True)
        self.bench_progress.set_fraction(0.0)
        self.bench_label.set_markup(
            "<span color='#7aa2f7' weight='bold'>Benchmark starting...</span>\n"
            "<span color='#565f89'>Testing each core individually (~25 seconds)</span>")

        # Collect all physical cores across CCDs
        all_cores = []
        for ccd_id in self.topo.get_all_ccd_ids():
            cpus = sorted(self.topo.get_ccd_cpus(ccd_id))
            mid = len(cpus) // 2
            physical = cpus[:mid]
            online_physical = [c for c in physical if self.topo.is_cpu_online(c)]
            if online_physical:
                all_cores.append((ccd_id, online_physical))

        # If CCD1 disappeared from topology (parked), add it manually
        if len(all_cores) < 2:
            try:
                with open("/sys/devices/system/cpu/cpu6/online") as f:
                    if f.read().strip() == "0":
                        all_cores.append((1, [6, 7, 8, 9, 10, 11]))
            except:
                pass

        total_cores = sum(len(cores) for _, cores in all_cores)
        if total_cores == 0:
            self.benching = False
            self.bench_btn.set_label("Run CCD Benchmark")
            self.bench_btn.set_sensitive(True)
            self.bench_progress.set_visible(False)
            self.bench_label.set_markup(
                "<span color='#f7768e'>No cores available for benchmark</span>")
            return

        cores_done = [0]
        all_results = {}

        def run_benchmark():
            """Run benchmark in background thread - updates UI via GLib.idle_add"""
            for ccd_id, physical in all_cores:
                results = {}
                for i, cpu in enumerate(physical):
                    GLib.idle_add(lambda c=cpu, ccd=ccd_id, i=i, n=len(physical):
                        self.bench_label.set_markup(
                            f"<span color='#7aa2f7' weight='bold'>Benchmark: CCD{ccd_id} - "
                            f"Core {i+1}/{n} (CPU {c})</span>\n"
                            f"<span color='#565f89'>{cores_done[0]}/{total_cores} cores tested</span>"))
                    GLib.idle_add(lambda: self.bench_progress.set_fraction(
                        cores_done[0] / total_cores))

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

            # Done
            GLib.idle_add(lambda: self.bench_progress.set_fraction(1.0))
            GLib.idle_add(lambda: self._finish_benchmark(all_results))

        threading.Thread(target=run_benchmark, daemon=True).start()

    def _finish_benchmark(self, all_results):
        self.benching = False
        self.bench_btn.set_label("Run CCD Benchmark")
        self.bench_btn.set_sensitive(True)
        self.bench_progress.set_visible(False)
        self._show_bench(all_results)

    def _show_bench(self, all_results):
        """Display benchmark results with clear comparison."""
        # Calculate averages and find best
        best_ccd = None
        best_avg = 0
        ccd_avgs = {}

        for ccd_id in sorted(all_results.keys()):
            results = all_results[ccd_id]
            if results:
                avg = sum(results.values()) / len(results)
                ccd_avgs[ccd_id] = avg
                if avg > best_avg:
                    best_avg = avg
                    best_ccd = ccd_id

        if not ccd_avgs:
            self.bench_label.set_markup(
                "<span color='#f7768e' weight='bold'>No results — benchmark failed</span>")
            return

        # Build result text — show kB/s AND relative comparison
        text = "<span color='#7aa2f7' weight='bold' size='14'>Benchmark Results</span>\n\n"

        # Find max for bar scaling
        max_avg = max(ccd_avgs.values()) if ccd_avgs else 1

        for ccd_id in sorted(ccd_avgs.keys()):
            avg = ccd_avgs[ccd_id]
            is_best = ccd_id == best_ccd
            color = "#e0af68" if is_best else "#c0caf5"
            icon = "🏆" if is_best else "  "

            # Bar: 20 chars, proportional to max
            bar_len = int((avg / max_avg) * 20) if max_avg > 0 else 0
            bar = "█" * bar_len + "░" * (20 - bar_len)

            # Show as GB/s (more readable than kB/s)
            gb_s = avg / 1000000
            pct = (avg / max_avg) * 100 if max_avg > 0 else 0

            text += f"<span color='{color}'>{icon} <b>CCD{ccd_id}</b>  {bar}  {gb_s:.2f} GB/s ({pct:.0f}%)</span>\n"

        if best_ccd is not None:
            # Calculate how much faster
            if len(ccd_avgs) > 1:
                other_avg = [v for k, v in ccd_avgs.items() if k != best_ccd]
                if other_avg:
                    diff_pct = ((best_avg - other_avg[0]) / other_avg[0]) * 100
                    text += f"\n<span color='#e0af68' weight='bold'>🏆 CCD{best_ccd} is {diff_pct:.1f}% faster → keep for gaming!</span>"
                else:
                    text += f"\n<span color='#e0af68' weight='bold'>🏆 CCD{best_ccd} is best → keep for gaming!</span>"
            else:
                text += f"\n<span color='#e0af68' weight='bold'>🏆 CCD{best_ccd} is best → keep for gaming!</span>"

        self.bench_label.set_markup(text)
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