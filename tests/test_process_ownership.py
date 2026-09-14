import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows managed runtime lifetime')
def test_parent_termination_ends_only_its_owned_child(tmp_path):
    import ctypes
    from ctypes import wintypes
    code = '''
import subprocess, sys, time
from backend.process_ownership import ChildJob
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                         creationflags=subprocess.CREATE_NO_WINDOW)
job = ChildJob(child)
print(child.pid, flush=True)
time.sleep(60)
'''
    owner = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             cwd=str(Path(__file__).resolve().parents[1]),
                             creationflags=subprocess.CREATE_NO_WINDOW)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    child_handle = None
    try:
        line = owner.stdout.readline().strip()
        assert line, owner.stderr.read()
        child_handle = kernel.OpenProcess(0x00100000, False, int(line))  # SYNCHRONIZE only
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # still running
        owner.terminate()
        owner.wait(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if child_handle:
            kernel.CloseHandle(child_handle)
