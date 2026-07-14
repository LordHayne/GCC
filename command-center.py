#!/usr/bin/env python3
"""
Ryzen Gaming Command Center — Kommandozentrale für AMD Ryzen + NVIDIA GPU
Zeigt CPU-Topologie, CCD-Parking (Game Mode), GPU-Infos und OC-Controls.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, GObject
import subprocess
import os
import re
import threading
import time

# ============================================================
# CPU Topology
# ============================================================
class CPUTopology:
    def __init__(self):
        self.ccds = {}
        self.detect()

    def detect(self):
        cache_ids = {}
        for cpu in range(64):
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

    def ccd_count(self):
        return len(self.ccds)

    def get_all_ccd_ids(self):
        return sorted(self.ccds.keys())

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

    def get_epp(self):
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference") as f:
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
        if self.ccd_count() < 2:
            return False
        for cpu in self.get_ccd_cpus(1):
            if not self.is_cpu_online(cpu):
                return True
        return False

    def get_online_count(self):
        return sum(1 for ccd in self.ccds.values() for cpu in ccd if self.is_cpu_online(cpu))

    def get_cpu_name(self):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
        except:
            pass
        return "Unknown CPU"


# ============================================================
# GPU Info (NVIDIA)
# ============================================================
class GPUInfo:
    def __init__(self):
        self.name = ""
        self.vram_total = 0
        self.vram_used = 0
        self.power_draw = 0.0
        self.power_limit = 0.0
        self.temp = 0.0
        self.clock_gr = 0
        self.clock_mem = 0
        self.max_clock_gr = 0
        self.max_clock_mem = 0
        self.pstate = ""
        self.util = 0
        self.gr_offset = 0
        self.mem_offset = 0
        self.powermizer = 0
        self.update()

    def update(self):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,power.draw,power.limit,temperature.gpu,clocks.gr,clocks.mem,pstate,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3
            )
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
        except:
            pass

        # Max clocks
        try:
            r = subprocess.run(["nvidia-smi", "-q", "-d", "CLOCK"], capture_output=True, text=True, timeout=3)
            gr_match = re.search(r'Graphics\s*:\s*(\d+)\s*MHz', r.stdout)
            mem_match = re.search(r'Memory\s*:\s*(\d+)\s*MHz', r.stdout)
            max_gr = re.search(r'Max Clocks.*?Graphics\s*:\s*(\d+)\s*MHz', r.stdout, re.DOTALL)
            max_mem = re.search(r'Max Clocks.*?Memory\s*:\s*(\d+)\s*MHz', r.stdout, re.DOTALL)
            if max_gr: self.max_clock_gr = int(max_gr.group(1))
            if max_mem: self.max_clock_mem = int(max_mem.group(1))
        except:
            pass

        # OC offsets
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
# CCD Parking
# ============================================================
class CCDController:
    HELPER = os.path.expanduser("~/.local/bin/gaming-ccd-helper")

    @staticmethod
    def park(ccd_cpus):
        try:
            r = subprocess.run(["pkexec", CCDController.HELPER, "on"],
                              capture_output=True, text=True, timeout=30)
            return "DONE_ON" in r.stdout
        except:
            return False

    @staticmethod
    def unpark(ccd_cpus):
        try:
            r = subprocess.run(["pkexec", CCDController.HELPER, "off"],
                              capture_output=True, text=True, timeout=30)
            return "DONE_OFF" in r.stdout
        except:
            return False


# ============================================================
# GPU Overclock
# ============================================================
class GPUController:
    @staticmethod
    def set_gr_offset(offset):
        try:
            r = subprocess.run(
                ["nvidia-settings", "-a", f"GPUGraphicsClockOffsetAllPerformanceLevels={offset}"],
                capture_output=True, text=True, timeout=3
            )
            return True
        except:
            return False

    @staticmethod
    def set_mem_offset(offset):
        try:
            r = subprocess.run(
                ["nvidia-settings", "-a", f"GPUMemoryTransferRateOffsetAllPerformanceLevels={offset}"],
                capture_output=True, text=True, timeout=3
            )
            return True
        except:
            return False

    @staticmethod
    def set_powermizer(mode):
        """0=Adaptive, 1=Prefer Max Performance, 2=Auto"""
        try:
            r = subprocess.run(
                ["nvidia-settings", "-a", f"GPUPowerMizerMode={mode}"],
                capture_output=True, text=True, timeout=3
            )
            return True
        except:
            return False


# ============================================================
# Main Window
# ============================================================
class CommandCenter(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Gaming Command Center")
        self.set_default_size(720, 780)
        self.topo = CPUTopology()
        self.gpu = GPUInfo()
        self.benching = False
        self.bench_results = {}

        css = """
        .ccd-card { padding: 14px; border-radius: 12px; margin: 4px; }
        .ccd-on { background: rgba(46,204,113,0.10); }
        .ccd-off { background: rgba(231,76,60,0.10); }
        .stat-value { font-size: 24px; font-weight: bold; }
        .stat-label { font-size: 11px; opacity: 0.55; }
        .freq-label { font-size: 11px; opacity: 0.6; }
        .core-dot { border-radius: 50%; min-width: 10px; min-height: 10px; }
        .core-on { background: #2ecc71; }
        .core-off { background: #e74c3c; opacity: 0.35; }
        .section-title { font-size: 14px; font-weight: bold; opacity: 0.7; margin-top: 10px; }
        .gpu-card { padding: 14px; border-radius: 12px; margin: 4px; background: rgba(52,152,219,0.08); }
        .slider-box { padding: 8px 0; }
        .preset-btn { padding: 4px 12px; }
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
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        self.set_content(main_box)
        main_box.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.set_margin_bottom(18)
        scroll.set_child(content)
        main_box.append(scroll)

        # CPU name
        cpu_name = self.topo.get_cpu_name()
        name_label = Gtk.Label(label=cpu_name)
        name_label.set_markup(f"<b>{cpu_name}</b>")
        name_label.set_halign(Gtk.Align.START)
        name_label.set_margin_top(8)
        content.append(name_label)

        # === CPU Stats Bar ===
        stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        stats.set_margin_top(10)

        self.lbl_threads = self._stat(stats, "Threads", "24")
        self.lbl_freq = self._stat(stats, "MHz", "---")
        self.lbl_temp = self._stat(stats, "Temp", "--°")
        self.lbl_gov = self._stat(stats, "Governor", "---")

        content.append(stats)
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # === CCD Section ===
        ccd_title = Gtk.Label(label="CPU CCDs")
        ccd_title.set_markup("<b>CPU CCDs</b>")
        ccd_title.set_halign(Gtk.Align.START)
        ccd_title.add_css_class("section-title")
        content.append(ccd_title)

        self.ccd_cards = {}
        for ccd_id in self.topo.get_all_ccd_ids():
            card = self._build_ccd_card(ccd_id)
            content.append(card)
            self.ccd_cards[ccd_id] = card

        # Game Mode button
        self.gm_btn = Gtk.Button(label="🎮 Game Mode: AN")
        self.gm_btn.set_margin_top(8)
        self.gm_btn.add_css_class("suggested-action")
        self.gm_btn.connect("clicked", self.on_toggle_gm)
        content.append(self.gm_btn)

        # Benchmark button
        self.bench_btn = Gtk.Button(label="⚡ CCD-Performance testen")
        self.bench_btn.set_margin_top(4)
        self.bench_btn.connect("clicked", self.on_benchmark)
        content.append(self.bench_btn)

        self.bench_label = Gtk.Label(label="")
        self.bench_label.set_wrap(True)
        content.append(self.bench_label)

        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # === GPU Section ===
        gpu_title = Gtk.Label(label="GPU")
        gpu_title.set_markup("<b>🎨 NVIDIA GPU</b>")
        gpu_title.set_halign(Gtk.Align.START)
        gpu_title.add_css_class("section-title")
        content.append(gpu_title)

        gpu_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gpu_card.add_css_class("gpu-card")
        gpu_card.set_margin_top(4)

        # GPU name + stats
        gpu_name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.gpu_name_lbl = Gtk.Label(label=self.gpu.name)
        self.gpu_name_lbl.set_markup(f"<b>{self.gpu.name}</b>")
        self.gpu_name_lbl.set_halign(Gtk.Align.START)
        gpu_name_box.append(self.gpu_name_lbl)
        gpu_card.append(gpu_name_box)

        # GPU stats row
        gpu_stats = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        gpu_stats.set_margin_top(6)
        self.gpu_clock_lbl = self._stat(gpu_stats, "Core", "---")
        self.gpu_mem_lbl = self._stat(gpu_stats, "Memory", "---")
        self.gpu_power_lbl = self._stat(gpu_stats, "Power", "---")
        self.gpu_temp_lbl = self._stat(gpu_stats, "Temp", "---")
        self.gpu_vram_lbl = self._stat(gpu_stats, "VRAM", "---")
        gpu_card.append(gpu_stats)

        # P-State + Util
        self.gpu_pstate_lbl = Gtk.Label(label="")
        self.gpu_pstate_lbl.set_halign(Gtk.Align.START)
        self.gpu_pstate_lbl.add_css_class("freq-label")
        gpu_card.append(self.gpu_pstate_lbl)

        # GPU clock progress bar
        self.gpu_clock_bar = Gtk.ProgressBar()
        self.gpu_clock_bar.set_margin_top(6)
        gpu_card.append(self.gpu_clock_bar)

        # === OC Controls ===
        oc_label = Gtk.Label(label="Übertaktung")
        oc_label.set_markup("<b>OC Offset (MHz)</b>")
        oc_label.set_halign(Gtk.Align.START)
        oc_label.set_margin_top(8)
        gpu_card.append(oc_label)

        # GPU Core offset
        gr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        gr_box.set_margin_top(4)
        gr_box.append(Gtk.Label(label="Core:"))
        self.gr_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.gr_slider.set_hexpand(True)
        self.gr_slider.set_value(0)
        self.gr_slider.connect("value-changed", self.on_gr_slider)
        gr_box.append(self.gr_slider)
        self.gr_value_lbl = Gtk.Label(label="0 MHz")
        self.gr_value_lbl.set_min_width_chars(8)
        gr_box.append(self.gr_value_lbl)
        gpu_card.append(gr_box)

        # GPU Memory offset
        mem_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mem_box.set_margin_top(4)
        mem_box.append(Gtk.Label(label="VRAM:"))
        self.mem_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -500, 500, 5)
        self.mem_slider.set_hexpand(True)
        self.mem_slider.set_value(0)
        self.mem_slider.connect("value-changed", self.on_mem_slider)
        mem_box.append(self.mem_slider)
        self.mem_value_lbl = Gtk.Label(label="0 MHz")
        self.mem_value_lbl.set_min_width_chars(8)
        mem_box.append(self.mem_value_lbl)
        gpu_card.append(mem_box)

        # PowerMizer
        pm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pm_box.set_margin_top(8)
        pm_box.append(Gtk.Label(label="PowerMizer:"))
        self.pm_combo = Gtk.DropDown.new_from_strings(["Adaptive", "Max Performance", "Auto"])
        self.pm_combo.set_selected(0)
        self.pm_combo.connect("notify::selected", self.on_pm_changed)
        pm_box.append(self.pm_combo)
        gpu_card.append(pm_box)

        # Apply OC button
        self.oc_btn = Gtk.Button(label="OC anwenden")
        self.oc_btn.set_margin_top(8)
        self.oc_btn.add_css_class("suggested-action")
        self.oc_btn.connect("clicked", self.on_apply_oc)
        gpu_card.append(self.oc_btn)

        # OC status
        self.oc_status_lbl = Gtk.Label(label="")
        self.oc_status_lbl.set_margin_top(4)
        gpu_card.append(self.oc_status_lbl)

        content.append(gpu_card)

    def _stat(self, parent, label, value):
        v = Gtk.Label(label=value)
        v.add_css_class("stat-value")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.append(v)
        l = Gtk.Label(label=label)
        l.add_css_class("stat-label")
        box.append(l)
        parent.append(box)
        return v

    def _build_ccd_card(self, ccd_id):
        cpus = self.topo.get_ccd_cpus(ccd_id)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("ccd-card")
        box.set_margin_top(4)

        # Title
        title = Gtk.Label(label=f"CCD{ccd_id} — {len(cpus)} Threads")
        title.set_markup(f"<b>CCD{ccd_id}</b> — {len(cpus)} Threads")
        title.set_halign(Gtk.Align.START)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title_box.append(title)
        best = Gtk.Label(label="")
        title_box.append(best)
        box.append(title_box)

        # Core dots
        cores_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        cores_box.set_margin_top(2)
        for cpu in sorted(cpus):
            dot = Gtk.Box()
            dot.set_size_request(10, 10)
            dot.add_css_class("core-dot")
            dot.set_tooltip_text(f"CPU {cpu}")
            cores_box.append(dot)
        box.append(cores_box)

        # Freq labels
        freq_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        freq_box.set_margin_top(2)
        freq_labels = []
        for cpu in sorted(cpus):
            lbl = Gtk.Label(label=f"{cpu}:---")
            lbl.add_css_class("freq-label")
            freq_box.append(lbl)
            freq_labels.append((cpu, lbl))
        box.append(freq_box)

        box._freq_labels = freq_labels
        box._best_label = best
        box._cores = cores_box
        return box

    def refresh(self):
        # CPU stats
        self.lbl_threads.set_label(str(self.topo.get_online_count()))
        self.lbl_freq.set_label(str(self.topo.get_cpu_freq(0)))
        self.lbl_temp.set_label(f"{self.topo.get_temp():.0f}°")
        self.lbl_gov.set_label(self.topo.get_governor())

        # Game mode
        gm = self.topo.get_game_mode()
        if gm:
            self.gm_btn.set_label("🟢 Game Mode: AUS")
            self.gm_btn.remove_css_class("suggested-action")
            self.gm_btn.add_css_class("destructive-action")
        else:
            self.gm_btn.set_label("🎮 Game Mode: AN")
            self.gm_btn.remove_css_class("destructive-action")
            self.gm_btn.add_css_class("suggested-action")

        # CCD cards
        for ccd_id, card in self.ccd_cards.items():
            cpus = self.topo.get_ccd_cpus(ccd_id)
            online = sum(1 for c in cpus if self.topo.is_cpu_online(c))
            card.remove_css_class("ccd-on" if online == len(cpus) else "ccd-off")
            card.add_css_class("ccd-on" if online == len(cpus) else "ccd-off")

            # dots
            i = 0
            child = card._cores.get_first_child()
            while child:
                cpu = sorted(cpus)[i]
                child.remove_css_class("core-on")
                child.remove_css_class("core-off")
                child.add_css_class("core-on" if self.topo.is_cpu_online(cpu) else "core-off")
                child = child.get_next_sibling()
                i += 1

            # freqs
            for cpu, lbl in card._freq_labels:
                f = self.topo.get_cpu_freq(cpu)
                if f > 0:
                    lbl.set_markup(f"<span color='#2ecc71'>{cpu}:{f}</span>")
                else:
                    lbl.set_markup(f"<span color='#e74c3c'>{cpu}:off</span>")

        # GPU stats
        self.gpu.update()
        self.gpu_name_lbl.set_markup(f"<b>{self.gpu.name}</b>")
        self.gpu_clock_lbl.set_label(str(self.gpu.clock_gr))
        self.gpu_mem_lbl.set_label(str(self.gpu.clock_mem))
        self.gpu_power_lbl.set_label(f"{self.gpu.power_draw:.0f}W")
        self.gpu_temp_lbl.set_label(f"{self.gpu.temp:.0f}°")
        self.gpu_vram_lbl.set_label(f"{self.gpu.vram_used}/{self.gpu.vram_total}M")
        self.gpu_pstate_lbl.set_label(
            f"P-State: {self.gpu.pstate}  |  GPU-Util: {self.gpu.util}%  |  "
            f"Power Limit: {self.gpu.power_limit:.0f}W  |  "
            f"Max: {self.gpu.max_clock_gr} MHz Core / {self.gpu.max_clock_mem} MHz Mem"
        )
        if self.gpu.max_clock_gr > 0:
            self.gpu_clock_bar.set_fraction(self.gpu.clock_gr / self.gpu.max_clock_gr)

        # OC offsets
        self.gr_slider.set_value(self.gpu.gr_offset)
        self.mem_slider.set_value(self.gpu.mem_offset)
        self.gr_value_lbl.set_label(f"{self.gpu.gr_offset} MHz")
        self.mem_value_lbl.set_label(f"{self.gpu.mem_offset} MHz")
        self.pm_combo.set_selected(self.gpu.powermizer)

        return True

    def on_toggle_gm(self, btn):
        gm = self.topo.get_game_mode()
        if gm:
            # Game Mode aus → CCD1 reaktivieren
            ok = CCDController.unpark(self.topo.get_ccd_cpus(1))
        else:
            # Game Mode an → CCD1 parken
            ok = CCDController.park(self.topo.get_ccd_cpus(1))

        # Warte kurz und aktualisiere Status
        def delayed_refresh():
            self.topo.detect()  # Topologie neu erkennen
            self.refresh()
        GLib.timeout_add(800, delayed_refresh)

    def on_gr_slider(self, slider):
        v = int(slider.get_value())
        self.gr_value_lbl.set_label(f"{v} MHz")

    def on_mem_slider(self, slider):
        v = int(slider.get_value())
        self.mem_value_lbl.set_label(f"{v} MHz")

    def on_pm_changed(self, combo, _):
        pass  # Applied on button click

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
                f"<span color='#2ecc71'>✅ OC angewendet: Core +{gr_off} MHz, VRAM +{mem_off} MHz, "
                f"PowerMizer={['Adaptive','Max Perf','Auto'][pm]}</span>"
            )
        else:
            self.oc_status_lbl.set_markup(
                "<span color='#e74c3c'>❌ Fehler beim Anwenden. Coolbits aktiviert?</span>"
            )
        GLib.timeout_add(1000, self.refresh)

    def on_benchmark(self, btn):
        if self.benching: return
        self.benching = True
        self.bench_btn.set_label("⚡ Benchmark läuft...")
        self.bench_btn.set_sensitive(False)
        all_results = {}

        def bench_next(ccd_id):
            if ccd_id >= self.topo.ccd_count():
                self.benching = False
                self.bench_btn.set_label("⚡ CCD-Performance testen")
                self.bench_btn.set_sensitive(True)
                self._show_bench(all_results)
                return

            cpus = self.topo.get_ccd_cpus(ccd_id)
            physical = [c for c in cpus if c < len(cpus) // 2 + 1]

            def done(results):
                all_results[ccd_id] = results
                GLib.idle_add(lambda: bench_next(ccd_id + 1))

            def run():
                results = {}
                for cpu in physical:
                    try:
                        r = subprocess.run(
                            ["taskset", "-c", str(cpu), "openssl", "speed", "-elapsed",
                             "-seconds", "2", "aes-256-cbc"],
                            capture_output=True, text=True, timeout=10)
                        last = r.stdout.strip().split("\n")[-1] if r.stdout else ""
                        parts = last.split()
                        results[cpu] = float(parts[1]) if len(parts) >= 2 else 0
                    except:
                        results[cpu] = 0
                done(results)

            t = threading.Thread(target=run, daemon=True)
            t.start()

        bench_next(0)

    def _show_bench(self, all_results):
        text = "<b>📊 CCD Benchmark</b>\n"
        best_ccd = None
        best_avg = 0
        for ccd_id in sorted(all_results.keys()):
            results = all_results[ccd_id]
            if results:
                avg = sum(results.values()) / len(results)
                text += f"CCD{ccd_id}: {avg:.0f} kB/s avg\n"
                if avg > best_avg:
                    best_avg = avg
                    best_ccd = ccd_id
        if best_ccd is not None:
            text += f"\n🏆 CCD{best_ccd} ist schneller → für Gaming behalten!"
        self.bench_label.set_markup(text)

        for ccd_id, card in self.ccd_cards.items():
            card._best_label.set_markup(" 🏆" if ccd_id == best_ccd else "")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.gaming.commandcenter")

    def do_activate(self):
        win = CommandCenter(self)
        win.present()


if __name__ == "__main__":
    app = App()
    app.run(None)