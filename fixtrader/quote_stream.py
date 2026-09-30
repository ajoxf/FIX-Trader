"""Small, read-only quote channel between the engine and Flask processes."""
import copy
import json
import logging
import os
import threading
import time

from . import atomicfile


class _DirectoryWatcher:
    """Wake an SSE reader on a Windows file change; poll elsewhere."""

    def __init__(self, path):
        self.handle = None
        self.kernel = None
        if os.name != 'nt':
            return
        try:
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.FindFirstChangeNotificationW.argtypes = (ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint)
            kernel.FindFirstChangeNotificationW.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            kernel.WaitForSingleObject.restype = ctypes.c_uint
            kernel.FindNextChangeNotification.argtypes = (ctypes.c_void_p,)
            kernel.FindNextChangeNotification.restype = ctypes.c_int
            kernel.FindCloseChangeNotification.argtypes = (ctypes.c_void_p,)
            kernel.FindCloseChangeNotification.restype = ctypes.c_int
            # Atomic replacement changes the directory entry; ordinary
            # writes change its last-write time.
            handle = kernel.FindFirstChangeNotificationW(
                os.path.dirname(os.path.abspath(path)), 0, 0x00000001 | 0x00000010)
            if handle and handle != ctypes.c_void_p(-1).value:
                self.kernel, self.handle = kernel, handle
        except (OSError, AttributeError):
            pass

    def wait(self):
        if self.handle is None:
            time.sleep(0.005)
            return
        # Bound the wait so an idle stream can still send a heartbeat.
        if self.kernel.WaitForSingleObject(self.handle, 1000) == 0:
            self.kernel.FindNextChangeNotification(self.handle)

    def close(self):
        if self.handle is not None:
            self.kernel.FindCloseChangeNotification(self.handle)
            self.handle = None


class QuotePublisher:
    def __init__(self, terminal, status_path):
        self.terminal = terminal
        self.path = str(status_path) + '.quotes.json'
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name='quote-publisher', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.terminal.quote_changed.set()
        self.thread.join(timeout=2)

    def publish(self):
        with self.terminal.lock:
            md = self.terminal.gateway._sessions.get('Market Data')
            payload = {'source': 'TT_FIX_UAT', 'simulated': False,
                       'connected': bool(md and md.state.status == 'CONNECTED' and md.is_running()),
                       'quotes': copy.deepcopy({k: {field: value for field, value in book.items() if field != 'entries'}
                                  for k, book in self.terminal.books.items() if k in self.terminal.watch})}
        payload['published_ms'] = time.time_ns() / 1_000_000
        # This is a disposable latest-value frame, rebuilt after restart.
        # Orders, fills, configuration and the regular status snapshot remain
        # durable; forcing the physical disk to flush for every market tick
        # only adds latency and write pressure on Windows.
        atomicfile.write_json(self.path, payload, indent=None, durable=False)

    def run(self):
        failures = 0
        while not self.stopped.is_set():
            self.terminal.quote_changed.clear()
            try:
                self.publish()
                failures = 0
            except Exception:
                failures += 1
                if failures == 1:
                    logging.getLogger(__name__).exception('Quote stream publish failed; regular snapshots remain available')
            # The FIX receiver signals the event after updating the book.
            # Publish on that signal rather than delaying every tick by a
            # fixed sleep. The file still carries only the latest book.
            self.terminal.quote_changed.wait(1)


def events(path):
    """Latest-state stream: slow clients skip intermediate frames, never queue them."""
    previous = None
    previous_stamp = None
    heartbeat = time.monotonic()
    watcher = _DirectoryWatcher(path)
    yield 'retry: 250\n\n'
    try:
        while True:
            try:
                stamp = os.stat(path).st_mtime_ns
            except OSError:
                stamp = None
            if stamp == previous_stamp:
                payload = None
            else:
                try:
                    payload = atomicfile.read_json(path, default=None)
                    previous_stamp = stamp
                except (OSError, ValueError):
                    payload = None  # retry the same stamp after a read error
            if payload and payload.get('published_ms') != previous:
                previous = payload['published_ms']
                payload['server_sent_ms'] = time.time_ns() / 1_000_000
                yield 'data: ' + json.dumps(payload, separators=(',', ':')) + '\n\n'
                heartbeat = time.monotonic()
            elif time.monotonic() - heartbeat > 1:
                yield ': heartbeat\n\n'
                heartbeat = time.monotonic()
            watcher.wait()
    finally:
        watcher.close()
