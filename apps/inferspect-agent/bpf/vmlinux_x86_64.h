/* Minimal vmlinux.h subset for the inferspect-agent BPF program.
 *
 * The canonical libbpf-CO-RE workflow generates this file from
 * /sys/kernel/btf/vmlinux on the build host. Building inside a Docker image
 * without host BTF access means we ship the minimum needed — struct pt_regs
 * (referenced by bpf_tracing.h's PT_REGS_PARM* macros) plus basic typedefs.
 *
 * The register field names mirror what libbpf's bpf/bpf_tracing.h expects
 * when ``__TARGET_ARCH_x86`` resolves to "user-mode" — i.e. the r-prefixed
 * names (rax, rdi, rsi, ...). This is the case when no in-tree
 * <asm/ptrace.h> is on the include path and we're not pulling vmlinux.h
 * from BTF.
 *
 * x86_64 only. Add other arches as runtime coverage expands.
 */

#ifndef __INFERSPECT_VMLINUX_X86_64_H__
#define __INFERSPECT_VMLINUX_X86_64_H__

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

struct pt_regs {
    unsigned long r15;
    unsigned long r14;
    unsigned long r13;
    unsigned long r12;
    unsigned long rbp;
    unsigned long rbx;
    unsigned long r11;
    unsigned long r10;
    unsigned long r9;
    unsigned long r8;
    unsigned long rax;
    unsigned long rcx;
    unsigned long rdx;
    unsigned long rsi;
    unsigned long rdi;
    unsigned long orig_rax;
    unsigned long rip;
    unsigned long cs;
    unsigned long eflags;
    unsigned long rsp;
    unsigned long ss;
};

#endif /* __INFERSPECT_VMLINUX_X86_64_H__ */
