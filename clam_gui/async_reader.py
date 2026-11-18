"""One background reader, one replaceable pending request, one UI delivery."""
import threading


class LatestReader:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.guard = threading.Lock()
        self.version = 0
        self.pending = self.ready = None
        self.active = self.scheduled = self.closed = False

    @property
    def busy(self):
        with self.guard:
            return self.active or self.pending is not None or self.ready is not None or self.scheduled

    def submit(self, load, done):
        with self.guard:
            if self.closed:
                return
            self.version += 1
            self.pending = self.version, load, done
            if self.active:
                return
            self.active = True
        try:
            threading.Thread(target=self._run, daemon=True).start()
        except Exception:
            with self.guard:
                self.active = False
                self.pending = None
            raise

    def _run(self):
        while True:
            with self.guard:
                if self.closed or self.pending is None:
                    self.active = False
                    return
                version, load, done = self.pending
                self.pending = None
            try:
                result, error = load(), None
            except Exception as caught:
                result, error = None, caught
            dispatch = False
            with self.guard:
                if not self.closed and version == self.version:
                    self.ready = version, done, result, error
                    if not self.scheduled:
                        self.scheduled = dispatch = True
            if dispatch:
                self.dispatch(self._deliver)

    def _deliver(self):
        with self.guard:
            value, self.ready = self.ready, None
            self.scheduled = False
            valid = value is not None and not self.closed and value[0] == self.version
        if valid:
            _, done, result, error = value
            done(result, error)
        return False

    def close(self):
        with self.guard:
            self.closed = True
            self.pending = self.ready = None
