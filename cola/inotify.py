# Copyright (c) 2008 David Aguilar
"""Provides an inotify plugin for Linux and other systems with pyinotify"""
from __future__ import division, absolute_import, unicode_literals

import os
from threading import Timer
from threading import Lock

try:
    import pyinotify
    from pyinotify import ProcessEvent
    from pyinotify import WatchManager
    from pyinotify import Notifier
    from pyinotify import EventsCodes
    AVAILABLE = True
except ImportError:
    ProcessEvent = object
    AVAILABLE = False

from cola import utils
if utils.is_win32():
    try:
        import win32file
        import win32con
        import pywintypes
        import win32event
        AVAILABLE = True
    except ImportError:
        ProcessEvent = object
        AVAILABLE = False

from PyQt4 import QtCore

from cola import gitcfg
from cola import cmds
from cola import core
from cola.git import STDOUT
from cola.i18n import N_
from cola.interaction import Interaction
from cola.models import main


_thread = None

def start():
    global _thread

    cfg = gitcfg.instance()
    if not cfg.get('cola.inotify', True):
        msg = N_('inotify is disabled because "cola.inotify" is false')
        Interaction.log(msg)
        return

    if not AVAILABLE:
        if utils.is_win32():
            msg = N_('file notification: disabled\n'
                     'Note: install pywin32 to enable.\n')
        elif utils.is_linux():
            msg = N_('inotify: disabled\n'
                     'Note: install python-pyinotify to enable inotify.\n')
        else:
            return

        if utils.is_debian():
            msg += N_('On Debian systems '
                      'try: sudo aptitude install python-pyinotify')
        Interaction.log(msg)
        return

    # Start the notification thread
    _thread = GitNotifier()
    _thread.start()
    if utils.is_win32():
        msg = N_('File notification enabled.')
    else:
        msg = N_('inotify enabled.')
    Interaction.log(msg)


def stop():
    if not has_inotify():
        return
    _thread.stop(True)
    _thread.wait()


def has_inotify():
    """Return True if pyinotify is available."""
    return AVAILABLE and _thread and _thread.isRunning()


class Handler():
    """Queues filesystem events for broadcast"""

    def __init__(self):
        """Create an event handler"""
        ## Timer used to prevent notification floods
        self._timer = None
        ## Lock to protect files and timer from threading issues
        self._lock = Lock()

    def broadcast(self):
        """Broadcasts a list of all files touched since last broadcast"""
        with self._lock:
            cmds.do(cmds.UpdateFileStatus)
            self._timer = None

    def handle(self, path):
        """Queues up filesystem events for broadcast"""
        with self._lock:
            if self._timer is None:
                self._timer = Timer(0.888, self.broadcast)
                self._timer.start()


class FileSysEvent(ProcessEvent):
    """Generated by GitNotifier in response to inotify events"""

    def __init__(self):
        """Maintain event state"""
        ProcessEvent.__init__(self)
        ## Takes care of Queueing events for broadcast
        self._handler = Handler()

    def process_default(self, event):
        """Queues up inotify events for broadcast"""
        if not event.name:
            return
        path = os.path.join(event.path, event.name)
        if os.path.exists(path):
            path = os.path.relpath(path)
            self._handler.handle(path)


class GitNotifier(QtCore.QThread):
    """Polls inotify for changes and generates FileSysEvents"""

    def __init__(self, timeout=333):
        """Set up the pyinotify thread"""
        QtCore.QThread.__init__(self)
        ## Git command object
        self._git = main.model().git
        ## pyinotify timeout
        self._timeout = timeout
        ## Path to monitor
        self._path = self._git.worktree()
        ## Signals thread termination
        self._running = True
        ## Directories to watching
        self._dirs_seen = set()
        ## The inotify watch manager instantiated in run()
        self._wmgr = None
        ## Events to capture
        if utils.is_linux():
            self._mask = (EventsCodes.ALL_FLAGS['IN_ATTRIB'] |
                          EventsCodes.ALL_FLAGS['IN_CLOSE_WRITE'] |
                          EventsCodes.ALL_FLAGS['IN_DELETE'] |
                          EventsCodes.ALL_FLAGS['IN_MODIFY'] |
                          EventsCodes.ALL_FLAGS['IN_MOVED_TO'])

    def stop(self, stopped):
        """Tells the GitNotifier to stop"""
        self._timeout = 0
        self._running = not stopped

    def _watch_directory(self, directory):
        """Set up a directory for monitoring by inotify"""
        if not self._wmgr:
            return
        directory = core.realpath(directory)
        if not core.exists(directory):
            return
        if directory not in self._dirs_seen:
            self._wmgr.add_watch(directory, self._mask)
            self._dirs_seen.add(directory)

    def _is_pyinotify_08x(self):
        """Is this pyinotify 0.8.x?

        The pyinotify API changed between 0.7.x and 0.8.x.
        This allows us to maintain backwards compatibility.
        """
        if hasattr(pyinotify, '__version__'):
            if pyinotify.__version__[:3] == '0.8':
                return True
        return False

    def run(self):
        """Create the inotify WatchManager and generate FileSysEvents"""

        if utils.is_win32():
            self.run_win32()
            return

        # Only capture events that git cares about
        self._wmgr = WatchManager()
        if self._is_pyinotify_08x():
            notifier = Notifier(self._wmgr, FileSysEvent(),
                                timeout=self._timeout)
        else:
            notifier = Notifier(self._wmgr, FileSysEvent())

        self._watch_directory(self._path)

        # Register files/directories known to git
        for filename in self._git.ls_files()[STDOUT].splitlines():
            filename = core.realpath(filename)
            directory = os.path.dirname(filename)
            self._watch_directory(directory)

        # self._running signals app termination.  The timeout is a tradeoff
        # between fast notification response and waiting too long to exit.
        while self._running:
            if self._is_pyinotify_08x():
                check = notifier.check_events()
            else:
                check = notifier.check_events(timeout=self._timeout)
            if not self._running:
                break
            if check:
                notifier.read_events()
                notifier.process_events()
        notifier.stop()

    def run_win32(self):
        """Generate notifications using pywin32"""

        hdir = win32file.CreateFile(
                self._path,
                0x0001,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS |
                win32con.FILE_FLAG_OVERLAPPED,
                None)

        flags = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME |
                 win32con.FILE_NOTIFY_CHANGE_DIR_NAME |
                 win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES |
                 win32con.FILE_NOTIFY_CHANGE_SIZE |
                 win32con.FILE_NOTIFY_CHANGE_LAST_WRITE |
                 win32con.FILE_NOTIFY_CHANGE_SECURITY)

        buf = win32file.AllocateReadBuffer(8192)
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, 0, 0, None)

        handler = Handler()
        while self._running:
            win32file.ReadDirectoryChangesW(hdir, buf, True, flags, overlapped)

            rc = win32event.WaitForSingleObject(overlapped.hEvent,
                                                self._timeout)
            if rc != win32event.WAIT_OBJECT_0:
                continue
            nbytes = win32file.GetOverlappedResult(hdir, overlapped, True)
            if not nbytes:
                continue
            results = win32file.FILE_NOTIFY_INFORMATION(buf, nbytes)
            for action, path in results:
                if not self._running:
                    break
                path = path.replace('\\', '/')
                if (not path.startswith('.git/') and
                        '/.git/' not in path and os.path.isfile(path)):
                    handler.handle(path)
