#!/usr/bin/env python3
"""Generate Hermes' x86_64 Bubblewrap seccomp cBPF artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_RET_K = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
AUDIT_ARCH_X86_64 = 0xC000003E
EPERM = 1

# x86_64 syscall numbers. Network syscalls are isolated independently by a
# private network namespace; these deny namespace escape and kernel control.
DENIED_SYSCALLS = {
    "ptrace": 101,
    "mount": 165,
    "umount2": 166,
    "pivot_root": 155,
    "swapon": 167,
    "swapoff": 168,
    "reboot": 169,
    "iopl": 172,
    "ioperm": 173,
    "init_module": 175,
    "delete_module": 176,
    "kexec_load": 246,
    "keyctl": 250,
    "add_key": 248,
    "request_key": 249,
    "unshare": 272,
    "move_pages": 279,
    "perf_event_open": 298,
    "setns": 308,
    "kcmp": 312,
    "finit_module": 313,
    "bpf": 321,
    "userfaultfd": 323,
    "kexec_file_load": 320,
    "open_by_handle_at": 304,
}


def instruction(code: int, jt: int, jf: int, value: int) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, value)


def program() -> bytes:
    parts = [
        instruction(BPF_LD_W_ABS, 0, 0, 4),
        instruction(BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        instruction(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        instruction(BPF_LD_W_ABS, 0, 0, 0),
    ]
    for number in sorted(DENIED_SYSCALLS.values()):
        parts.extend((
            instruction(BPF_JMP_JEQ_K, 0, 1, number),
            instruction(BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | EPERM),
        ))
    parts.append(instruction(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    return b"".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    payload = program()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": 1,
        "architecture": "x86_64",
        "policy_id": "executor-local-v1",
        "artifact_sha256": digest,
        "denied_syscalls": sorted(DENIED_SYSCALLS),
        "generator": "deploy/sandbox/generate_seccomp.py",
    }
    args.manifest.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
