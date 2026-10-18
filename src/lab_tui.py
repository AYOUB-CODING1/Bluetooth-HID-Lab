#!/usr/bin/env python3
"""
lab_tui.py - Terminal dashboard for the Bluetooth HID lab.

It is ONLY a front-end: it runs the exact same commands you already use, in the
same order, and shows all of their output live so you can still see every log
line if something goes wrong.

    sudo scripts/restore.sh
    sudo scripts/setup.sh --forget AA:BB:CC:DD:EE:FF -u "https://example.com"

The logic is untouched - this just wraps those two commands in a TUI.

Usage:
    sudo python3 src/lab_tui.py          # or: sudo scripts/gui.sh
    sudo python3 src/lab_tui.py -u URL

Keys:
    Tab        switch field (Website / Target MAC / RUN)
    Enter      start the sequence (on RUN) - or restart after it finishes
    Ctrl+R     start / restart
    Esc        stop the running sequence (kills it)
    Up/Down    scroll the log      PgUp/PgDn  page the log
    End        jump to the newest log line
    q          quit
"""

import argparse
import curses
import locale
import os
import pty
import select
import signal
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
LOGFILE = "/tmp/hid-lab-tui.log"

DEFAULT_MAC = "AA:BB:CC:DD:EE:FF"
DEFAULT_URL = "https://example.com"

# curses colour pair ids
C_DIM, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_MAG = 1, 2, 3, 4, 5, 6
C_TITLE, C_BORDER, C_SEL = 7, 8, 9


def elevate():
    if os.geteuid() != 0:
        sys.stderr.write("[*] root is required - restarting with sudo...\n")
        os.execvp("sudo", ["sudo", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


# --------------------------------------------------------------------------
#  Process runner (streams stdout/stderr from a pty)
# --------------------------------------------------------------------------
class Runner:
    def __init__(self, cwd):
        self.cwd = cwd
        self.cmds = []          # list of (label, argv)
        self.idx = 0
        self.master = None
        self.proc = None
        self.running = False
        self.buf = b""
        self.exit_codes = []
        self._logf = None

    def set_commands(self, cmds):
        self.cmds = cmds
        self.idx = 0
        self.exit_codes = []
        self.buf = b""

    def start(self, lines_out):
        if self._logf is None:
            try:
                self._logf = open(LOGFILE, "wb", buffering=0)
            except OSError:
                self._logf = False
        self._spawn(lines_out)

    def _spawn(self, lines_out):
        if self.idx >= len(self.cmds):
            self.running = False
            self.master = None
            return
        label, argv = self.cmds[self.idx]
        lines_out(f"\n===== {label} =====\n")
        self.master, slave = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                argv, cwd=self.cwd, stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=os.setsid, close_fds=True)
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.running = True

    def pump(self, lines_out):
        if not self.running or self.master is None:
            return
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, OSError):
            data = b""
        if data:
            if self._logf:
                self._logf.write(data)
            self.buf += data
            text = self.buf.decode("utf-8", errors="replace")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            *complete, self.buf = text.split("\n")
            for ln in complete:
                lines_out(ln)
            self.buf = self.buf.encode("utf-8", errors="replace")
        else:
            # EOF: did the child finish?
            if self.proc is not None and self.proc.poll() is not None:
                self._finish(lines_out)

    def _finish(self, lines_out):
        code = self.proc.returncode
        self.exit_codes.append(code)
        lines_out(f"----- exit code: {code} -----\n")
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
        self.master = None
        self.proc = None
        self.idx += 1
        self._spawn(lines_out)

    def stop(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
                time.sleep(0.3)
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                pass
        self.running = False
        self.master = None
        self.proc = None

    @property
    def phase(self):
        if not self.cmds:
            return "idle"
        if self.idx >= len(self.cmds):
            return "done"
        return self.cmds[self.idx][0]


# --------------------------------------------------------------------------
#  UI
# --------------------------------------------------------------------------
class UI:
    def __init__(self, scr, url, mac, restore):
        self.scr = scr
        self.url = url
        self.mac = mac
        self.restore = restore
        self.focus = 0            # 0 url, 1 mac, 2 restore toggle, 3 run
        self.lines = []
        self.scroll = 0
        self.autofollow = True
        self.problem = False
        self.status = "Ready."
        self.runner = Runner(SCRIPTS_DIR)
        self.spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self.spin_i = 0
        self.start_t = None
        self.max_lines = 20000

    # ---- log sink ----
    def add(self, text):
        text = text.rstrip("\n")
        if not text:
            self.lines.append("")
        else:
            for ln in text.split("\n"):
                self.lines.append(ln)
        low = text.lower()
        if ("[x]" in low or "error" in low or "traceback" in low
                or "failed" in low or "denied" in low):
            self.problem = True
        if len(self.lines) > self.max_lines:
            del self.lines[:len(self.lines) - self.max_lines]
        if self.autofollow:
            self.scroll = 10 ** 9

    # ---- run ----
    def build_commands(self):
        cmds = []
        if self.restore:
            cmds.append(("1/2  restore.sh", [os.path.join(SCRIPTS_DIR, "restore.sh")]))
        cmds.append(("2/2  setup.sh",
                     [os.path.join(SCRIPTS_DIR, "setup.sh"),
                      "--forget", self.mac, "-u", self.url]))
        return cmds

    def start_run(self):
        self.lines = []
        self.scroll = 0
        self.autofollow = True
        self.problem = False
        self.start_t = time.time()
        self.status = "Running..."
        self.runner.set_commands(self.build_commands())
        self.runner.start(self.add)

    def cancel_run(self):
        self.runner.stop()
        self.status = "Cancelled by user."
        self.problem = True
        self.add("[x] sequence cancelled\n")

    # ---- colours ----
    @staticmethod
    def colour_for(line):
        l = line.lower()
        if "[x]" in l or "traceback" in l or "error" in l or "failed" in l:
            return C_RED
        if "[!]" in l or "warn" in l:
            return C_YELLOW
        if "[+]" in l or l.startswith("ok") or "ready" in l:
            return C_GREEN
        if "[*]" in l or "====" in l or "-----" in l:
            return C_CYAN
        return 0

    # ---- drawing ----
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 12 or w < 50:
            scr.addstr(0, 0, "terminal too small")
            scr.refresh()
            return

        # title
        title = " Bluetooth HID Lab  —  Control Deck "
        bar = "═" * (w - 2)
        self.put(0, 0, "╔" + bar + "╗", C_BORDER)
        self.put(0, max(0, (w - len(title)) // 2), title, C_TITLE | curses.A_BOLD)
        self.put(1, 0, "╚" + bar + "╝", C_BORDER)

        # fields
        y = 2
        self.draw_field(y, "Website (-u)", self.url, self.focus == 0)
        self.draw_field(y + 1, "Target MAC   ", self.mac, self.focus == 1)
        toggle = "[x] run restore.sh first" if self.restore else "[ ] run restore.sh first"
        sel = curses.A_REVERSE if self.focus == 2 else 0
        self.put(y + 2, 2, f" {toggle} ", C_MAG | sel)

        # run button
        label = "  ▶  RUN  " if not self.runner.running else "  ■  RUNNING  "
        sel = curses.A_REVERSE if self.focus == 3 else 0
        self.put(y + 2, max(30, w - 16), label, C_GREEN | curses.A_BOLD | sel)

        # log box
        log_top = y + 4
        footer_h = 2
        log_h = h - log_top - footer_h
        self.put(log_top - 1, 0, "┌" + "─" * (w - 2) + "┐", C_BORDER)
        usable = log_h
        total = len(self.lines)
        if self.autofollow:
            self.scroll = max(0, total - usable)
        self.scroll = max(0, min(self.scroll, max(0, total - usable)))
        view = self.lines[self.scroll:self.scroll + usable]
        for i in range(usable):
            self.put(log_top + i, 0, "│", C_BORDER)
            if i < len(view):
                self.put(log_top + i, 2, view[i][:w - 4], self.colour_for(view[i]))
            self.put(log_top + i, w - 1, "│", C_BORDER)
        self.put(log_top + usable, 0, "└" + "─" * (w - 2) + "┘", C_BORDER)

        # status + help
        elapsed = int(time.time() - self.start_t) if self.start_t else 0
        if self.runner.running:
            self.spin_i = (self.spin_i + 1) % len(self.spinner)
            run = f"{self.spinner[self.spin_i]} {self.runner.phase}  {elapsed}s"
            col = C_CYAN
        elif self.problem:
            run = "● finished with warnings - check the log above"
            col = C_RED
        elif self.start_t:
            run = f"● finished in {elapsed}s"
            col = C_GREEN
        else:
            run = "● idle"
            col = C_DIM
        self.put(h - 2, 0, (" " + run).ljust(w - 1), col | curses.A_BOLD)
        help_txt = " Tab field · Enter/Ctrl+R run · Esc stop · ↑↓/PgUp scroll · End=latest · q/Ctrl+C quit "
        self.put(h - 1, 0, help_txt.ljust(w - 1), C_DIM)
        scr.refresh()

    def draw_field(self, y, label, value, focused):
        h, w = self.scr.getmaxyx()
        self.put(y, 2, f"{label}: ", C_DIM)
        box_x = 2 + len(label) + 2
        inner = max(10, w - box_x - 3)
        val = value[-(inner - 1):].ljust(inner)
        attr = curses.A_REVERSE if focused else 0
        self.put(y, box_x, " " + val + " ", C_CYAN | attr)

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addstr(y, x, text[:w - x - 1] if x > 0 else text[:w - 1], attr)
        except curses.error:
            pass

    # ---- input ----
    def handle(self, ch):
        if ch == -1:
            return True
        # navigation
        if ch == 9:                       # Tab
            self.focus = (self.focus + 1) % 4
        elif ch == curses.KEY_BTAB:
            self.focus = (self.focus - 1) % 4
        elif ch == curses.KEY_UP:
            self.autofollow = False
            self.scroll = max(0, self.scroll - 1)
        elif ch == curses.KEY_DOWN:
            self.scroll += 1
        elif ch == curses.KEY_NPAGE:
            self.autofollow = False
            self.scroll += 10
        elif ch == curses.KEY_PPAGE:
            self.autofollow = False
            self.scroll = max(0, self.scroll - 10)
        elif ch == curses.KEY_END:
            self.autofollow = True
        elif ch in (10, 13):
            if self.focus == 3 and not self.runner.running:
                self.start_run()
            elif not self.runner.running and self.focus in (0, 1):
                self.focus = 3
        elif ch == 18:                    # Ctrl+R
            if not self.runner.running:
                self.start_run()
        elif ch == 27:                    # Esc
            if self.runner.running:
                self.cancel_run()
        elif ch == 3:                     # Ctrl+C
            if self.runner.running:
                self.cancel_run()
            return False
        elif ch == ord("q") and self.focus in (2, 3):
            return False
        elif ch == ord(" ") and self.focus == 2:
            self.restore = not self.restore
        elif ch == curses.KEY_RESIZE:
            pass
        elif self.focus in (0, 1) and not self.runner.running:
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.focus == 0:
                    self.url = self.url[:-1]
                else:
                    self.mac = self.mac[:-1]
            elif 32 <= ch < 127:
                if self.focus == 0:
                    self.url += chr(ch)
                else:
                    self.mac += chr(ch)
        return True


def curses_main(scr, url, mac, smoke):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    scr.nodelay(True)
    scr.timeout(60)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_DIM, curses.COLOR_WHITE, -1)
    curses.init_pair(C_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_RED, curses.COLOR_RED, -1)
    curses.init_pair(C_MAG, curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_TITLE, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_BORDER, curses.COLOR_BLUE, -1)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)

    ui = UI(scr, url, mac, restore=True)
    ui.add("[*] Bluetooth HID lab control deck ready.\n")
    ui.add(f"[*] It will run, in order:\n")
    ui.add(f"      sudo scripts/restore.sh\n")
    ui.add(f"      sudo scripts/setup.sh --forget {mac} -u \"{url}\"\n")
    ui.add("[*] Press Enter (or Ctrl+R) on RUN to begin. All logs appear below.\n")

    ticks = 0
    while True:
        ui.runner.pump(ui.add)
        ui.draw()
        ch = scr.getch()
        if not ui.handle(ch):
            break
        if smoke and ticks > 12:
            break
        ticks += 1
    ui.runner.stop()


def main():
    ap = argparse.ArgumentParser(description="Bluetooth HID lab terminal dashboard")
    ap.add_argument("-u", "--url", default=DEFAULT_URL, help="website to open")
    ap.add_argument("--mac", default=DEFAULT_MAC, help="target MAC to forget")
    ap.add_argument("--no-elevate", action="store_true", help="do not auto-sudo")
    ap.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.no_elevate:
        elevate()
    os.chdir(SCRIPTS_DIR)
    try:
        curses.wrapper(curses_main, args.url, args.mac, args.smoke)
    except KeyboardInterrupt:
        pass
    print(f"[*] full log saved to {LOGFILE}")


if __name__ == "__main__":
    main()
