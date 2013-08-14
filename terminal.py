import ctypes
import fcntl
import select
import signal
import struct
import sys
import termios
import threading
import tty

ESC = '\033'
CSI = ESC + '['

CURSOR_UP    = 'A'
CURSOR_DOWN  = 'B'
CURSOR_RIGHT = 'C'
CURSOR_LEFT  = 'D'
CURSOR_POS   = 'H'

SGR = 'm'

HARD_CLEAR = 'J', 2

DSR = 'n'
CPR = 'R'

RESET = 0
BOLD = 1
FAINT = 2
ITALIC = 3
UNDERLINE = 4
BLINK_SLOW = 5
BLINK_FAST = 6
REVERSE_VIDEO = 7
CONCEAL = 8
STRIKE_OUT = 9
FRAKTUR = 20
FG_OFFSET = 30
BG_OFFSET = 40

ATTR_FORMAT = BOLD, FAINT, ITALIC, UNDERLINE, BLINK_SLOW, \
    BLINK_FAST, REVERSE_VIDEO, CONCEAL, STRIKE_OUT, FRAKTUR, \
    FG_OFFSET, BG_OFFSET,

def attr_index(attr):
    return ATTR_FORMAT.index(attr)

BOLD_FAINT_OFF = 22
ITALIC_FRAKTUR_OFF = 23
UNDERLINE_OFF = 24
BLINK_OFF = 25
REVERSE_VIDEO_OFF = 27
CONCEAL_OFF = 28
STRIKE_OUT_OFF = 29
DEFAULT_FG = 39
DEFAULT_BG = 49

def attr_off(attr):
    if attr in (BOLD, FAINT):
        return BOLD_FAINT_OFF
    elif attr in (ITALIC, FRAKTUR):
        return ITALIC_FRAKTUR_OFF
    elif attr == UNDERLINE:
        return UNDERLINE_OFF
    elif attr in (BLINK_SLOW, BLINK_FAST):
        return BLINK_OFF
    elif attr == REVERSE_VIDEO:
        return REVERSE_VIDEO_OFF
    elif attr == CONCEAL:
        return CONCEAL_OFF
    elif attr == STRIKE_OUT:
        return STRIKE_OUT_OFF
    elif attr == FG_OFFSET:
        return DEFAULT_FG
    elif attr == BG_OFFSET:
        return DEFAULT_BG

def syscolor(n, bright=False):
    return n + 8 * bright

def rgb(r, g, b):
    r, g, b = [int(c * 5) for c in (r, g, b)]
    return r*36 + g*6 + b + 16
    
def grayscale(n):
    return 232 + n

def color_attr(offset, color):
    if color < 8:
        return str(offset + color)
    else:
        return '%d;5;%d' % (offset + 8, color)

def term_size():
    size = fcntl.ioctl(0, termios.TIOCGWINSZ, '\0'*8)
    return struct.unpack('hhhh', size)[:2]

class InputWrapper:

    def __init__(self, infile, callback):
        self.infile = infile
        self.callback = callback
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.buf = ''
        self.start()

    def fileno(self):
        return self.infile.fileno()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.loop)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify()

    def read(self, n=None):
        with self.lock:
            string = self.buf[:n]
            self.buf = self.buf[n:]
        return string

    def getchar(self):
        while True:
            ch = self.read(1)
            if ch:
                return ch
            with self.cond:
                self.cond.wait(1)

    def getstring(self):
        buf = ''
        while True:
            ch = self.getchar()
            if ch == '\n':
                return buf
            buf += ch

    def loop(self):
        self.gen = AnsiEscapeParser(self.callback)
        self.gen.next()
        while self.running:
            if select.select([self.infile], [], [])[0]:
                char = self.infile.read(1)
                while char:
                    char = self.gen.send(char)
                    if char is None:
                        char = self.infile.read(1)
                        continue
                    else:
                        with self.lock:
                            self.buf += char
                        with self.cond:
                            self.cond.notify()
                        break

def AnsiEscapeParser(callback):
    ch = (yield)
    while True:
        if ch == ESC:
            ch = (yield)
            if ESC + ch == CSI:
                params, code = '', ''
                ch = (yield)
                while 48 <= ord(ch) < 64:
                    params += ch
                    ch = (yield)
                while 32 <= ord(ch) < 48:
                    code += ch
                    ch = (yield)
                callback(params, code + ch)
                ch = (yield)
            else:
                ch = ord(ch)
                mesg = 'ESC %02d/%02d' % (ch >> 4, ch & 0xf)
                raise NotImplementedError, mesg
        else:
            ch = (yield ch)

class CellType(ctypes.Structure):

    _pack_ = 1
    _fields_ = \
        ('ch', ctypes.c_wchar), \
        ('fg', ctypes.c_ubyte), \
        ('bg', ctypes.c_ubyte), \
        ('attr', ctypes.c_short)

    def __eq__(self, other):
        return buffer(self) == buffer(other)

    def __ne__(self, other):
        return buffer(self) != buffer(other)

    def has_attr(self, attr):
        attr = ATTR_FORMAT.index(attr)
        return bool(self.attr & (1 << attr))

    def attr_on(self, attr):
        attr = ATTR_FORMAT.index(attr)
        self.attr |= (1 << attr)

    def attr_off(self, attr):
        attr = ATTR_FORMAT.index(attr)
        self.attr &= ~(1 << attr)
        if i == FG_OFFSET:
            self._fg = 0
        elif i == BG_OFFSET:
            self._bg = 0

    def attr_changed(self, old):
        return not (self.fg == old.fg and \
            self.bg == old.bg and \
            self.attr == old.attr)

class Window:

    def __init__(self, rows, cols, ch=' ', fg=0, bg=0, attr=0):
        self.buf = (CellType * (rows * cols))()
        self.rows = rows
        self.cols = cols
        self.fill(ch, fg, bg, attr)

    def __getitem__(self, rc):
        r, c = rc
        return self.buf[r * self.cols + c]

    def __setitem__(self, rc, cell):
        r, c = rc
        if cell.fg != 0:
            cell.attr |= 1 << ATTR_FORMAT.index(FG_OFFSET)
        if cell.bg != 0:
            cell.attr |= 1 << ATTR_FORMAT.index(BG_OFFSET)
        self.buf[r * self.cols + c] = cell

    def fill(self, ch=' ', fg=0, bg=0, attr=0):
        for cell in self.buf:
            cell.ch = ch
            cell.fg = fg
            cell.bg = bg
            cell.attr = attr

    def clear(self):
        self.fill()        

    #def copy(self, rows=None, cols=None):
    #    if rows is None:
    #        rows = self.rows
    #    if cols is None:
    #        cols = self.cols
    #    win = Window(rows, cols)
    #    dst = ctypes.addressof(win.buf)
    #    src = ctypes.addressof(self.buf)
    #    minrow = min(win.rows, self.rows)
    #    mincol = min(win.cols, self.cols)
    #    size = ctypes.sizeof(CellType)
    #    for row in range(minrow):
    #        dstoff = row * win.cols * size
    #        srcoff = row * self.cols * size
    #        ctypes.memmove(dst + dstoff, src + srcoff, mincol * size)
    #    return win

class Terminal:

    def __init__(self, infile=sys.stdin, outfile=sys.stdout):
        self.infile = InputWrapper(infile, self.handle_escape)
        self.getchar = self.infile.getchar
        self.getstring = self.infile.getstring
        self.outfile = outfile
        self.escape_handlers = { CPR: self.cpr_callback }
        self.dsr_cond = threading.Condition()
        self.hard_clear()
        self.cursor_pos = -1, -1
        self.cell = CellType()
        self.rows, self.cols = size = term_size()
        self.stdscr = Window(*size)
        self.curscr = Window(*size)

    def __enter__(self):
        self.mode = termios.tcgetattr(self.infile)
        self.sigwinch = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self.resize)
        return self

    def __exit__(self, *args):
        self.escape(SGR, 0)
        self.infile.stop()
        termios.tcsetattr(self.infile, termios.TCSAFLUSH, self.mode)
        signal.signal(signal.SIGWINCH, self.sigwinch)

    def escape(self, code, *params):
        params = ';'.join(map(str, params))
        self.outfile.write(CSI + params + code)
        self.outfile.flush()

    def handle_escape(self, params, code):
        if code in self.escape_handlers:
            params = map(int, params.split(';'))
            self.escape_handlers[code](*params)

    def register_escape(self, code, func):
        self.escape_handlers[code] = func

    def resize(self, *args):
        #self.rows, self.cols = size = term_size()
        raise NotImplementedError, 'resize'

    def setraw(self):
        tty.setraw(self.infile)

    def setcbreak(self):
        tty.setcbreak(self.infile)

    def get_cursor(self, wait=True):
        self.cursor_pos = None
        self.escape(DSR, 6)
        if wait:
            with self.dsr_cond:
                while self.cursor_pos is None:
                    self.dsr_cond.wait()
                return self.cursor_pos

    def cpr_callback(self, row=1, col=1):
        with self.dsr_cond:
            self.cursor_pos = [row, col]
            self.dsr_cond.notify()

    def move_cursor(self, row, col):
        row = max(row, 1)
        row = min(row, self.rows)
        col = max(col, 1)
        col = min(col, self.cols)
        dr = row - self.cursor_pos[0]
        dc = col - self.cursor_pos[1]
        if dr != 0 and dc != 0:
            self.escape(CURSOR_POS, row, col)
        elif dr > 0:
            self.escape(CURSOR_DOWN, dr)
        elif dr < 0:
            self.escape(CURSOR_UP, -dr)
        elif dc > 0:
            self.escape(CURSOR_RIGHT, dc)
        elif dc < 0:
            self.escape(CURSOR_LEFT, -dc)   
        self.cursor_pos = [row, col]

    def hard_clear(self):
        self.escape(*HARD_CLEAR)

    def increment_cursor(self):
        self.cursor_pos[1] += 1
        if self.cursor_pos[1] == self.cols:
            self.cursor_pos[0] += 1
            self.cursor_pos[1] = 0

    def refresh(self):
        for r in range(self.rows):
            for c in range(self.cols):
                old = self.curscr[r, c]
                new = self.stdscr[r, c]
                if new != old:
                    if self.cursor_pos != [r + 1, c + 1]:
                        self.move_cursor(r + 1, c + 1)
                    if new.attr_changed(old):
                        args = []
                        for attr in ATTR_FORMAT:
                            if attr in (FG_OFFSET, BG_OFFSET):
                                if new.has_attr(attr):
                                    if attr == FG_OFFSET and \
                                            (not old.has_attr(attr) or \
                                            new.fg != old.fg):
                                        args.append(color_attr(attr, new.fg))
                                    elif attr == BG_OFFSET and \
                                            (not old.has_attr(attr) or \
                                            new.bg != old.bg):
                                        args.append(color_attr(attr, new.bg))
                            elif new.has_attr(attr) and not old.has_attr(attr):
                                args.append(attr)
                            elif not new.has_attr(attr) and old.has_attr(attr):
                                args.append(attr_off(attr))
                        self.escape(SGR, *args)
                    self.outfile.write(new.ch)
                    self.curscr[r, c] = new
                    self.increment_cursor()
        self.outfile.flush()

    def set_screen(self, scr):
        assert scr.rows == self.curscr.rows and scr.cols == self.curscr.cols
        self.stdscr = scr
        self.clear = scr.clear
        self.fill = scr.fill

    def __getitem__(self, key):
        return self.stdscr.__getitem__(key)

    def __setitem__(self, key, value):
        if len(value) == 0:
            value = [0]
        self.stdscr.__setitem__(key, CellType(*value))

    def fg(self, fg):
        self.cell.fg = fg

    def bg(self, bg):
        self.cell.bg = bg

    def attr_on(self, *attrs):
        if RESET in attrs:
            self.cell = CellType()
        elif FG_OFFSET in attrs or BG_OFFSET in attrs:
            raise RuntimeError, 'use fg() and bg() to set colors'
        else:
            for attr in attrs:
                self.cell.attr_on(attr)
    
    def attr_off(self, *attrs):
        if not attrs:
            self.cell = CellType()
        else:
            for attr in ATTR_FORMAT:
                 self.cell.attr_off(i)

if __name__ == '__main__':
    with Terminal() as t:
        t.setcbreak()
        t.attr_on(REVERSE_VIDEO)
        for row in range(t.rows):
            for col in range(t.cols):
                if row < t.rows / 2. and col < t.cols / 2.:
                    star = col % 2 == 1 and row % 2 == 1
                    ch = '*' if star else '&'
                    fg = rgb(1, 1, 1) if star else rgb(0, 0, 1)
                else:
                    w = t.rows / (13/2.)
                    red = row % w < w/2
                    ch = '^'
                    fg = rgb(1, 0, 0) if red else rgb(1, 1, 1)
                t[row, col] = (ch, fg)
        t.getchar()
        t.refresh()
        t.getchar()
        t.refresh()
        t.getchar()
