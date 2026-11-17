# emu.s: table-driven rcpps/rsqrtps per lane (tables/amd or tables/intel, picked by gen.py). in %eax, out %eax; clobbers %ecx %edx %r8 and flags.
	.text
	.globl emu_rcp, emu_rsq, T_RCP, T_RSQ
emu_rcp:
	mov %eax,%ecx; and $0x80000000,%ecx
	mov %eax,%edx; shr $23,%edx; and $0xff,%edx
	test %edx,%edx; jz 1f
	cmp $0xff,%edx; je 2f
	mov %eax,%r8d; and $0x7fffff,%r8d; shr $11,%r8d
	lea T_RCP(%rip),%rax; mov (%rax,%r8,4),%eax
	mov %eax,%r8d; shr $23,%r8d
	sub %edx,%r8d; add $127,%r8d
	jle 3f
	shl $23,%r8d; and $0x7fffff,%eax; or %r8d,%eax; or %ecx,%eax; ret
1:	mov %ecx,%eax; or $0x7f800000,%eax; ret
2:	test $0x7fffff,%eax; jz 3f; or $0x400000,%eax; ret
3:	mov %ecx,%eax; ret
emu_rsq:
	mov %eax,%edx; shr $23,%edx; and $0xff,%edx
	test %edx,%edx; jz 1f
	cmp $0xff,%edx; je 2f
	test %eax,%eax; js 4f
	sub $127,%edx
	mov %edx,%ecx; and $1,%ecx; shl $12,%ecx
	mov %eax,%r8d; and $0x7fffff,%r8d; shr $11,%r8d; or %ecx,%r8d
	lea T_RSQ(%rip),%rax; mov (%rax,%r8,4),%eax
	sar $1,%edx; shl $23,%edx; sub %edx,%eax; ret
1:	and $0x80000000,%eax; or $0x7f800000,%eax; ret
2:	test $0x7fffff,%eax; jnz 5f; test %eax,%eax; js 4f; xor %eax,%eax; ret
5:	or $0x400000,%eax; ret
4:	mov $0xffc00000,%eax; ret
	.balign 16
T_RCP:	.incbin "rcp4096.bin"
T_RSQ:	.incbin "rsqrt8192.bin"
