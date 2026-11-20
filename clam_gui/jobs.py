"""Run a scan and report preflight/storage failures through the same result API."""
from .results import ResultKind, ScanStatus


def execute_scan(plan, store, cancel=None, *, scanner=None, failure=None, **kwargs):
    from .core import scan, record_scan_failure, ScanBusyError
    from .execution import ScanAlreadyQueued
    try:
        return (scanner or scan)(plan, store, cancel, **kwargs)
    except (ScanBusyError, ScanAlreadyQueued):
        raise  # Automatic jobs retry contention; manual/scheduled callers record it.
    except Exception as error:
        try:
            return (failure or record_scan_failure)(store, plan, error, kwargs.get('profile_id'))
        except Exception as storage_error:
            # Even a malformed plan or a failing failure reporter must reach completion.
            status = ScanStatus('検査不完了：' + str(error) + '\n検査履歴も保存できませんでした：' + str(storage_error), ResultKind.INCOMPLETE)
            return status, str(error) + '\n履歴保存エラー：' + str(storage_error)


def start_worker(target, failed):
    """Report thread construction/start failures synchronously on the UI thread."""
    import threading
    try:
        threading.Thread(target=target, daemon=True).start()
    except Exception as error:
        failed(error)
        return False
    return True
