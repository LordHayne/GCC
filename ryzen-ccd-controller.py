#!/usr/bin/env python3
"""
Ryzen CCD Controller — Graphisches Tool für AMD Ryzen CCD-Parking
Zeigt CPU-Topologie, live Frequenzen, Temperaturen und erlaubt
CCD-Parking mit einem Klick (Game Mode wie Ryzen Master).
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, GObject
import subprocess
import os
import re
import time
import threading

# ============================================================
# CPU Topology Detection
# ============================================================
class CPUTopology:
    def __init__(self):
        self.ccds = {}  # ccd_id -> list of cpu numbers
        self.detect()

    def detect(self):
        """Erkennt CCD/CCX Layout über L3 Cache shared_cpu_list"""
        cache_groups = {}
        for cpu in range(64):
            path = f"/sys/devices/system/cpu/cpu{cpu}/cache/index3/shared_cpu_list"
            try:
                with open(path) as f:
                    shared = f.read().strip()
                if shared and shared not in cache_groups:
                    cache_groups[shared] = []
                if shared:
                    cache_groups[shared].append(cpu)
            except (FileNotFoundError, OSError):
                continue

        # Jede L3-Cache-Gruppe = ein CCX, CCDs zusammenfassen
        # Bei 3900X: 4 CCX groups → 2 CCDs (je 2 CCX)
        # Gruppiere nach cache_id
        cache_ids = {}
        for cpu in range(64):
            path = f"/sys/devices/system/cpu/cpu{cpu}/cache/index3/id"
            try:
                with open(path) as f:
                    cid = int(f.read().strip())
                if cid not in cache_ids:
                    cache_ids[cid] = []
                cache_ids[cid].append(cpu)
            except (FileNotFoundError, OSError):
                continue

        # CCD = Gruppe von CCXs, die benachbarte cache_ids haben
        # Bei 3900X: cache_ids 0,1 = CCD0; 2,3 = CCD1
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
            return True  # CPU 0 is always online

    def get_cpu_freq(self, cpu):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq") as f:
                return int(f.read().strip()) // 1000
        except:
            return 0

    def get_cpu_max_freq(self, cpu):
        try:
            with open(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_max_freq") as f:
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
            result = subprocess.run(["sensors"], capture_output=True, text=True, timeout=2)
            for line in result.stdout.split("\n"):
                if "Tctl:" in line or "Tdie:" in line:
                    m = re.search(r'([+-]?[\d.]+)°C', line)
                    if m:
                        return float(m.group(1))
        except:
            pass
        return 0.0

    def get_game_mode(self):
        """Prüft ob CCD1 geparkt ist = Game Mode"""
        if self.ccd_count() < 2:
            return False
        ccd1_cpus = self.get_ccd_cpus(1)
        for cpu in ccd1_cpus:
            if not self.is_cpu_online(cpu):
                return True
        return False

    def get_online_count(self):
        count = 0
        for ccd in self.ccds.values():
            for cpu in ccd:
                if self.is_cpu_online(cpu):
                    count += 1
        return count


# ============================================================
# CCD Parking (root operations via pkexec)
# ============================================================
class CCDController:
    HELPER_PATH = os.path.expanduser("~/.local/bin/gaming-ccd-helper")

    @staticmethod
    def park_ccd(ccd_cpus):
        """Schaltet CCD ab (braucht root)"""
        helper = CCDController.HELPER_PATH
        if not os.path.exists(helper):
            return False, "Helper script not found"
        cpu_str = " ".join(str(c) for c in ccd_cpus)
        try:
            # Write a temp helper with the right CPUs
            result = subprocess.run(
                ["pkexec", helper, "on"],
                capture_output=True, text=True, timeout=30
            )
            if "DONE_ON" in result.stdout:
                return True, "CCD geparkt"
            return False, result.stderr or "Unbekannter Fehler"
        except subprocess.TimeoutExpired:
            return False, "Zeitüberschreitung"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def unpark_ccd(ccd_cpus):
        """Schaltet CCD wieder an (braucht root)"""
        helper = CCDController.HELPER_PATH
        if not os.path.exists(helper):
            return False, "Helper script not found"
        try:
            result = subprocess.run(
                ["pkexec", helper, "off"],
                capture_output=True, text=True, timeout=30
            )
            if "DONE_OFF" in result.stdout:
                return True, "CCD aktiviert"
            return False, result.stderr or "Unbekannter Fehler"
        except subprocess.TimeoutExpired:
            return False, "Zeitüberschreitung"
        except Exception as e:
            return False, str(e)


# ============================================================
# Benchmark
# ============================================================
class CCDBenchmark:
    @staticmethod
    def benchmark_ccd(ccd_cpus, callback):
        """Benchmarkt ein CCD in einem Thread"""
        def run():
            results = {}
            for cpu in ccd_cpus:
                try:
                    proc = subprocess.run(
                        ["taskset", "-c", str(cpu), "openssl", "speed", "-elapsed",
                         "-seconds", "2", "aes-256-cbc"],
                        capture_output=True, text=True, timeout=10
                    )
                    last_line = proc.stdout.strip().split("\n")[-1] if proc.stdout else ""
                    parts = last_line.split()
                    if len(parts) >= 2:
                        results[cpu] = float(parts[1])
                    else:
                        results[cpu] = 0.0
                except:
                    results[cpu] = 0.0
            callback(results)

        t = threading.Thread(target=run, daemon=True)
        t.start()


# ============================================================
# Main Window
# ============================================================
class RyzenCCDWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Ryzen CCD Controller")
        self.set_default_size(620, 580)
        self.topo = CPUTopology()
        self.benchmarking = False
        self.bench_results = {}

        # CSS
        css = """
        .ccd-card { padding: 16px; border-radius: 12px; margin: 6px; }
        .ccd-on { background: rgba(46,204,113,0.12); }
        .ccd-off { background: rgba(231,76,60,0.12); }
        .ccd-best { border: 2px solid #2ecc71; }
        .freq-label { font-size: 11px; opacity: 0.6; }
        .stat-value { font-size: 22px; font-weight: bold; }
        .stat-label { font-size: 11px; opacity: 0.6; }
        .game-mode-on { background: linear-gradient(135deg, #e74c3c22, #f39c1222); }
        .game-mode-off { background: rgba(46,204,113,0.08); }
        .core-dot { border-radius: 50%; min-width: 8px; min-height: 8px; }
        .core-on { background: #2ecc71; }
        .core-off { background: #e74c3c; opacity: 0.4; }
        .bench-winner { color: #2ecc71; font-weight: bold; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.build_ui()
        self.update_status()
        # Auto-refresh
        GLib.timeout_add(1000, self.update_status)

    def build_ui(self):
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header bar
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        self.set_content(main_box)
        main_box.append(header)

        # Scrollable content
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_bottom(20)
        scroll.set_child(content)
        main_box.append(scroll)

        # === Game Mode Banner ===
        self.gm_banner = Adw.Banner()
        self.gm_banner.set_revealed(False)
        content.append(self.gm_banner)

        # === Status Row ===
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        status_box.set_margin_top(12)

        # Threads
        self.threads_label = Gtk.Label(label="24")
        self.threads_label.add_css_class("stat-value")
        threads_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        threads_box.append(self.threads_label)
        tl = Gtk.Label(label="Threads")
        tl.add_css_class("stat-label")
        threads_box.append(tl)
        status_box.append(threads_box)

        # Frequency
        self.freq_label = Gtk.Label(label="---")
        self.freq_label.add_css_class("stat-value")
        freq_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        freq_box.append(self.freq_label)
        fl = Gtk.Label(label="MHz (Core 0)")
        fl.add_css_class("stat-label")
        freq_box.append(fl)
        status_box.append(freq_box)

        # Temperature
        self.temp_label = Gtk.Label(label="--°")
        self.temp_label.add_css_class("stat-value")
        temp_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        temp_box.append(self.temp_label)
        tl2 = Gtk.Label(label="Temp")
        tl2.add_css_class("stat-label")
        temp_box.append(tl2)
        status_box.append(temp_box)

        # Governor
        self.gov_label = Gtk.Label(label="---")
        self.gov_label.add_css_class("stat-value")
        gov_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        gov_box.append(self.gov_label)
        gl = Gtk.Label(label="Governor")
        gl.add_css_class("stat-label")
        gov_box.append(gl)
        status_box.append(gov_box)

        content.append(status_box)

        # Separator
        content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # === CCD Cards ===
        self.ccd_cards = {}
        for ccd_id in self.topo.get_all_ccd_ids():
            card = self.build_ccd_card(ccd_id)
            content.append(card)
            self.ccd_cards[ccd_id] = card

        # === Game Mode Button ===
        self.gm_button = Gtk.Button(label="🎮 Game Mode: AN")
        self.gm_button.set_margin_top(16)
        self.gm_button.set_margin_bottom(8)
        self.gm_button.add_css_class("suggested-action")
        self.gm_button.connect("clicked", self.on_toggle_game_mode)
        content.append(self.gm_button)

        # === Benchmark Button ===
        self.bench_button = Gtk.Button(label="⚡ CCD-Performance testen")
        self.bench_button.set_margin_bottom(16)
        self.bench_button.connect("clicked", self.on_benchmark)
        content.append(self.bench_button)

        # === Benchmark Results ===
        self.bench_label = Gtk.Label(label="")
        self.bench_label.set_wrap(True)
        self.bench_label.set_markup("")
        content.append(self.bench_label)

    def build_ccd_card(self, ccd_id):
        cpus = self.topo.get_ccd_cpus(ccd_id)
        physical = [c for c in cpus if c < 24]  # SMT pairs: physical < thread count/2

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("ccd-card")

        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=f"CCD{ccd_id}")
        title.set_markup(f"<b>CCD{ccd_id}</b>  —  {len(cpus)} Threads")
        title.set_halign(Gtk.Align.START)
        header_box.append(title)

        # Best CCD badge
        best_label = Gtk.Label(label="")
        best_label.set_name(f"best-badge-{ccd_id}")
        header_box.append(best_label)
        box.append(header_box)

        # Core dots
        cores_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cores_box.set_margin_top(4)
        for cpu in sorted(cpus):
            dot = Gtk.Box()
            dot.set_size_request(10, 10)
            dot.add_css_class("core-dot")
            dot.set_tooltip_text(f"CPU {cpu}")
            cores_box.append(dot)
        box.append(cores_box)

        # Freq display
        freq_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        freq_box.set_margin_top(4)
        self_freqs = []
        for cpu in sorted(cpus):
            lbl = Gtk.Label(label=f"{cpu}:---")
            lbl.add_css_class("freq-label")
            freq_box.append(lbl)
            self_freqs.append((cpu, lbl))
        box.append(freq_box)

        box._freq_labels = self_freqs
        box._best_label = best_label
        box._cores = cores_box

        return box

    def update_status(self):
        # Update stats
        online = self.topo.get_online_count()
        self.threads_label.set_label(str(online))

        freq = self.topo.get_cpu_freq(0)
        self.freq_label.set_label(f"{freq}")

        temp = self.topo.get_temp()
        self.temp_label.set_label(f"{temp:.0f}°")

        gov = self.topo.get_governor()
        self.gov_label.set_label(gov)

        # Game mode status
        game_mode = self.topo.get_game_mode()
        if game_mode:
            self.gm_banner.set_title("🎮 Game Mode aktiv — CCD1 geparkt, 6 Kerne für Gaming")
            self.gm_banner.set_revealed(True)
            self.gm_banner.set_button_label("Deaktivieren")
            self.gm_button.set_label("🟢 Game Mode: AUS")
            self.gm_button.remove_css_class("suggested-action")
            self.gm_button.add_css_class("destructive-action")
        else:
            self.gm_banner.set_title(f"🟢 Normal Mode — {online} Threads aktiv")
            self.gm_banner.set_revealed(True)
            self.gm_banner.set_button_label("Aktivieren")
            self.gm_button.set_label("🎮 Game Mode: AN")
            self.gm_button.remove_css_class("destructive-action")
            self.gm_button.add_css_class("suggested-action")

        # Update CCD cards
        for ccd_id, card in self.ccd_cards.items():
            cpus = self.topo.get_ccd_cpus(ccd_id)
            online_count = sum(1 for c in cpus if self.topo.is_cpu_online(c))
            if online_count == len(cpus):
                card.remove_css_class("ccd-off")
                card.add_css_class("ccd-on")
            else:
                card.remove_css_class("ccd-on")
                card.add_css_class("ccd-off")

            # Core dots
            cores = card._cores
            for i, cpu in enumerate(sorted(cpus)):
                dot = cores.get_first_child()
                for _ in range(i):
                    dot = dot.get_next_sibling()
                if dot:
                    dot.remove_css_class("core-on")
                    dot.remove_css_class("core-off")
                    if self.topo.is_cpu_online(cpu):
                        dot.add_css_class("core-on")
                    else:
                        dot.add_css_class("core-off")

            # Freq labels
            for cpu, lbl in card._freq_labels:
                f = self.topo.get_cpu_freq(cpu)
                max_f = self.topo.get_cpu_max_freq(cpu)
                if f > 0:
                    lbl.set_markup(f"<span color=\"#2ecc71\">{cpu}:{f}</span>")
                else:
                    lbl.set_markup(f"<span color=\"#e74c3c\">{cpu}:off</span>")

        return True  # keep timer alive

    def on_toggle_game_mode(self, btn):
        game_mode = self.topo.get_game_mode()
        if game_mode:
            # Deactivate
            ccd1_cpus = self.topo.get_ccd_cpus(1)
            ok, msg = CCDController.unpark_ccd(ccd1_cpus)
        else:
            # Activate
            ccd1_cpus = self.topo.get_ccd_cpus(1)
            ok, msg = CCDController.park_ccd(ccd1_cpus)

        GLib.timeout_add(500, self.update_status)

    def on_benchmark(self, btn):
        if self.benchmarking:
            return
        self.benchmarking = True
        self.bench_button.set_label("⚡ Benchmark läuft...")
        self.bench_button.set_sensitive(False)

        all_results = {}
        ccd_count = self.topo.ccd_count()

        def benchmark_next(ccd_id):
            if ccd_id >= ccd_count:
                # Done - show results
                self.benchmarking = False
                self.bench_button.set_label("⚡ CCD-Performance testen")
                self.bench_button.set_sensitive(True)
                self.show_bench_results(all_results)
                return

            cpus = self.topo.get_ccd_cpus(ccd_id)
            # Only benchmark physical cores (first half)
            physical = [c for c in cpus if c < len(cpus) // 2 + 1]

            def done(results):
                all_results[ccd_id] = results
                GLib.idle_add(lambda: benchmark_next(ccd_id + 1))

            CCDBenchmark.benchmark_ccd(physical, done)

        benchmark_next(0)

    def show_bench_results(self, all_results):
        # Calculate averages
        text = "<b>📊 CCD Benchmark Ergebnisse</b>\n\n"
        best_ccd = None
        best_avg = 0
        for ccd_id in sorted(all_results.keys()):
            results = all_results[ccd_id]
            if results:
                avg = sum(results.values()) / len(results)
                text += f"<b>CCD{ccd_id}</b>: {avg:.0f} kB/s avg\n"
                if avg > best_avg:
                    best_avg = avg
                    best_ccd = ccd_id
            else:
                text += f"<b>CCD{ccd_id}</b>: kein Ergebnis\n"

        if best_ccd is not None:
            text += f"\n🏆 <b>CCD{best_ccd} ist schneller!</b> → Für Gaming behalten, anderen CCD parken"

        self.bench_label.set_markup(text)

        # Mark best CCD
        for ccd_id, card in self.ccd_cards.items():
            if ccd_id == best_ccd:
                card._best_label.set_markup(" 🏆")
            else:
                card._best_label.set_markup("")


# ============================================================
# App
# ============================================================
class RyzenCCDApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.ryzen.ccdcontroller")

    def do_activate(self):
        win = RyzenCCDWindow(self)
        win.present()


if __name__ == "__main__":
    app = RyzenCCDApp()
    app.run(None)