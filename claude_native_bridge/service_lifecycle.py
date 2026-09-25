"""Client-aware retirement for the shared local API service.

Hermes processes identify themselves as ``pid:create_time`` on authenticated
requests, and name the bridge client (owner) the request belongs to. The
service stays up while any live process holds an open client or any request
is in flight, and exits once it has been unused for the idle period with no
open client left. A long-lived process (dashboard, gateway) that has closed
its clients does not pin the service; its next client starts it again.
Exiting releases its runtime files and native sessions, so a finished Hermes
session does not leave a server holding files an update needs.
"""

from __future__ import annotations

import math
import time

import psutil

IDLE_CHECK_SECONDS = 5.0
DRAIN_TIMEOUT_SECONDS = 20.0
MAX_CLIENTS = 256
MAX_OPEN_PER_CLIENT = 1024
MAX_OWNER_LENGTH = 512
# create_time is computed the same way by client and server on one host;
# the tolerance only absorbs float formatting, not a reused PID.
_CREATE_TIME_TOLERANCE = 0.5


def parse_identity(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    pid, separator, created = value.partition(":")
    try:
        pid, created = int(pid), float(created)
    except ValueError:
        return None
    if not separator or pid <= 0 or not math.isfinite(created) or created <= 0:
        return None
    return pid, created


def process_alive(pid, created):
    try:
        actual = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True  # Cannot inspect it: keep serving rather than strand a client.
    return abs(actual - created) < _CREATE_TIME_TOLERANCE


class ServiceActivity:
    def __init__(self, idle_seconds, *, clock=time.monotonic, alive=process_alive):
        self.idle_seconds = idle_seconds
        self._clock = clock
        self._alive = alive
        self.clients: dict[int, float] = {}
        self.open: dict[int, set[str]] = {}
        self.inflight = 0
        self.last = clock()
        self.draining = False

    def note_client(self, header, owner=None):
        parsed = parse_identity(header)
        if parsed is None:
            return
        pid, created = parsed
        if pid not in self.clients and len(self.clients) >= MAX_CLIENTS:
            self.prune()
            if len(self.clients) >= MAX_CLIENTS:
                return
        if self.clients.get(pid) != created:
            self.open.pop(pid, None)  # A reused PID replaces the dead identity.
        self.clients[pid] = created
        if isinstance(owner, str) and 0 < len(owner) <= MAX_OWNER_LENGTH:
            owned = self.open.setdefault(pid, set())
            if len(owned) < MAX_OPEN_PER_CLIENT:
                owned.add(owner)

    def release_client(self, header, owner):
        parsed = parse_identity(header)
        if parsed is None or self.clients.get(parsed[0]) != parsed[1]:
            return
        owned = self.open.get(parsed[0])
        if owned is not None:
            owned.discard(owner)
            if not owned:
                del self.open[parsed[0]]

    def enter(self):
        self.inflight += 1
        self.last = self._clock()

    def exit(self):
        self.inflight -= 1
        self.last = self._clock()

    def prune(self):
        for pid, created in list(self.clients.items()):
            if not self._alive(pid, created):
                del self.clients[pid]
                self.open.pop(pid, None)

    def should_retire(self, busy_owners):
        if self.draining or not self.idle_seconds:
            return False
        if self.inflight or busy_owners:
            return False
        if self._clock() - self.last < self.idle_seconds:
            return False
        self.prune()
        return not self.open
