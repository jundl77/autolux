"""Windows process ownership. No handles are inherited by child processes."""
import ctypes
from ctypes import wintypes as w


class _Limits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', w.DWORD), ('min_working_set', ctypes.c_size_t),
                ('max_working_set', ctypes.c_size_t), ('active_processes', w.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _Limits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


def _kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, arguments, result in (
        ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ('SetInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        ('AssignProcessToJobObject', [w.HANDLE, w.HANDLE], w.BOOL),
        ('OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ('CloseHandle', [w.HANDLE], w.BOOL),
        ('CreateMutexW', [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
    ):
        fn = getattr(api, name)
        fn.argtypes, fn.restype = arguments, result
    return api


class Job:
    """Kill owned children if Python exits, including forced termination.

    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    def __init__(self):
        self.api = _kernel()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def add(self, pid):
        process = self.api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class RoleLock:
    def __init__(self, role):
        self.api = _kernel()
        self.handle = self.api.CreateMutexW(None, False, f'Global\\Autolux-{role}')
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS; applies across checkouts and RDP sessions
            self.close()
            raise RuntimeError(f'An Autolux {role} is already running.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
