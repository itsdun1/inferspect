/* Minimal vmlinux.h subset for ARM64 (aarch64).
 *
 * Same purpose as vmlinux_x86_64.h — provide ``struct pt_regs`` so libbpf's
 * BPF_KPROBE / PT_REGS_PARM* macros expand correctly. ARM64's struct is
 * very different from x86_64's: 31 general-purpose registers in an array,
 * then sp/pc/pstate. libbpf's bpf_tracing.h uses ``regs[0]`` through
 * ``regs[7]`` for PARM1–PARM8.
 *
 * Aligned with arch/arm64/include/uapi/asm/ptrace.h's ``user_pt_regs``
 * (the userspace-visible portion). The kernel struct has extra trailing
 * fields (orig_x0, syscallno, etc.) that uprobes don't touch.
 */

#ifndef __INFERSPECT_VMLINUX_AARCH64_H__
#define __INFERSPECT_VMLINUX_AARCH64_H__

typedef unsigned char __u8;
typedef short __s16;
typedef unsigned short __u16;
typedef int __s32;
typedef unsigned int __u32;
typedef long long __s64;
typedef unsigned long long __u64;
typedef long s64;
typedef unsigned long u64;

#ifndef NULL
#define NULL ((void *)0)
#endif

/* On ARM64, libbpf's bpf_tracing.h aliases PT_REGS_ARM64 to
 * ``struct user_pt_regs`` (not the kernel-internal ``struct pt_regs``).
 * Provide both names — the BPF program's own SEC handlers still take a
 * ``struct pt_regs *ctx`` argument (libbpf treats it as opaque). */
struct user_pt_regs {
    unsigned long regs[31];
    unsigned long sp;
    unsigned long pc;
    unsigned long pstate;
};

struct pt_regs {
    union {
        struct user_pt_regs user_regs;
        struct {
            unsigned long regs[31];
            unsigned long sp;
            unsigned long pc;
            unsigned long pstate;
        };
    };
};

#endif /* __INFERSPECT_VMLINUX_AARCH64_H__ */
