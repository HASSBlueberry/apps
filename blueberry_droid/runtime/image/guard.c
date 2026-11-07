#include <stdio.h>
#include <unistd.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    char label[4096] = {0};
    FILE *current = fopen("/proc/self/attr/current", "r");
    if (current) {
        if (!fgets(label, sizeof(label), current)) label[0] = 0;
        fclose(current);
    }
    int separators = 0;
    for (size_t i = 0; label[i]; ++i) if (label[i] == ':') ++separators;
    if (separators >= 2 || access("/sys/fs/selinux/enforce", F_OK) == 0) {
        fputs("Blueberry Android cannot run on this SELinux host. Use a separate Linux VM. Host security policy was not changed.\n", stderr);
        return 78;
    }
    if (getpid() != 1) {
        fputs("Android init must be container PID 1.\n", stderr);
        return 78;
    }
    const char *bridge[] = {
        "ro.dalvik.vm.native.bridge=libndk_translation.so",
        "ro.enable.native.bridge.exec=1",
        "ro.vendor.enable.native.bridge.exec=1",
        "ro.product.cpu.abilist=x86_64,arm64-v8a",
        "ro.product.cpu.abilist64=x86_64,arm64-v8a",
        "ro.dalvik.vm.isa.arm64=x86_64"
    };
    size_t count = 0;
#ifdef __x86_64__
    if (access("/system/lib64/libndk_translation.so", R_OK) == 0)
        count = sizeof(bridge) / sizeof(bridge[0]);
#endif
    char **args = calloc((size_t)argc + count + 1, sizeof(char *));
    if (!args) return 71;
    args[0] = "/init";
    for (int i = 1; i < argc; ++i) args[i] = argv[i];
    for (size_t i = 0; i < count; ++i) args[argc + i] = (char *)bridge[i];
    execv(args[0], args);
    fprintf(stderr, "Android init failed to execute, %s\n", strerror(errno));
    return 71;
}
