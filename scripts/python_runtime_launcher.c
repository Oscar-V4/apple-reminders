#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern char **environ;

/* This launcher only locates the bundled interpreter and removes inherited
 * Python configuration. It never downloads code or requests Reminders access. */
int main(int argc, char **argv) {
    uint32_t capacity = 0;
    (void)_NSGetExecutablePath(NULL, &capacity);
    char *candidate = malloc(capacity);
    if (!candidate || _NSGetExecutablePath(candidate, &capacity) != 0) {
        fputs("Apple Reminders: could not locate bundled Python launcher.\n", stderr);
        free(candidate);
        return 78;
    }
    char executable[PATH_MAX];
    if (!realpath(candidate, executable)) {
        free(candidate);
        fputs("Apple Reminders: could not resolve bundled Python launcher.\n", stderr);
        return 78;
    }
    free(candidate);
    char *separator = strrchr(executable, '/');
    if (!separator) return 78;
    *separator = '\0';
    char interpreter[PATH_MAX];
    int length = snprintf(interpreter, sizeof(interpreter),
                          "%s/../Resources/python/bin/python3.13", executable);
    if (length < 0 || (size_t)length >= sizeof(interpreter)) return 78;

    /* unsetenv changes environ, so resume at the same index after removal. */
    for (size_t index = 0; environ[index];) {
        if (strncmp(environ[index], "PYTHON", 6) != 0) {
            index++;
            continue;
        }
        char *equals = strchr(environ[index], '=');
        if (!equals) { index++; continue; }
        char *name = strndup(environ[index], (size_t)(equals - environ[index]));
        if (!name) return 78;
        int result = unsetenv(name);
        free(name);
        if (result != 0) return 78;
    }
    /* macOS Python also consumes this non-PYTHON-prefixed venv variable. */
    if (unsetenv("__PYVENV_LAUNCHER__") != 0 ||
        setenv("PYTHONNOUSERSITE", "1", 1) != 0 ||
        setenv("PYTHONDONTWRITEBYTECODE", "1", 1) != 0 ||
        setenv("PYTHONUTF8", "1", 1) != 0) return 78;

    char **forwarded = calloc((size_t)argc + 1, sizeof(char *));
    if (!forwarded) return 78;
    forwarded[0] = interpreter;
    for (int index = 1; index < argc; index++) forwarded[index] = argv[index];
    execv(interpreter, forwarded);
    int failure = errno;
    free(forwarded);
    fprintf(stderr, "Apple Reminders: bundled Python could not start (%d).\n", failure);
    return 78;
}
