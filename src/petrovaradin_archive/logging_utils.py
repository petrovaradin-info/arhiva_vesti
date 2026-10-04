"""Console diagnostics with local wall time and monotonic operation durations."""
import builtins
from datetime import datetime
from threading import RLock
from time import perf_counter

_lock = RLock()


def log(*values, sep=' ', end='\n', file=None, flush=True):
    # Prefix every physical line, including multiline Selenium errors.
    message = sep.join(str(value) for value in values)
    with _lock:
        stamp = datetime.now().astimezone().isoformat(sep=' ', timespec='milliseconds')
        lines = message.splitlines() or ['']
        builtins.print('\n'.join(f'[{stamp}] {line}' for line in lines),
                       end=end, file=file, flush=flush)


def elapsed(start):
    return f'trajanje={perf_counter() - start:.3f}s'
