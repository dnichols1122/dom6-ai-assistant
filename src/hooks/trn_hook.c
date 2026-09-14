/*
 * trn_hook.c — LD_PRELOAD hook for dom6_amd64
 *
 * Intercepts fopen/fwrite/fclose.  When the game writes a .trn file, the
 * complete buffer is captured and forwarded via a named pipe so Python can
 * receive it the instant end-turn completes.
 *
 * Outputs per .trn write:
 *   /tmp/dom6_trn_<nation>.bin   — raw bytes (same as the .trn file on disk)
 *   /tmp/dom6_trn_<nation>.log   — one line per fwrite with offset/caller_rva
 *
 * Named pipe (created if absent):
 *   /tmp/dom6_trn_hook.pipe
 *   Protocol (all little-endian):
 *     [4 bytes]  magic  "TRN!"
 *     [2 bytes]  nation name length  (uint16)
 *     [N bytes]  nation name (no null)
 *     [4 bytes]  data length  (uint32)
 *     [M bytes]  raw .trn bytes
 *
 * Build:
 *   gcc -shared -fPIC -O2 -o trn_hook.so trn_hook.c -ldl -lpthread
 *
 * Run (without Steam):
 *   LD_PRELOAD=/path/to/trn_hook.so dom6_amd64 --nosound
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define PIPE_PATH "/tmp/dom6_trn_hook.pipe"

/* ------------------------------------------------------------------ */
/* Real libc function pointers                                         */
/* ------------------------------------------------------------------ */

static FILE  *(*real_fopen  )(const char *, const char *) = NULL;
static FILE  *(*real_fopen64)(const char *, const char *) = NULL;
static size_t (*real_fwrite )(const void *, size_t, size_t, FILE *) = NULL;
static int   (*real_fclose  )(FILE *) = NULL;

/* ------------------------------------------------------------------ */
/* Per-file capture state                                              */
/* ------------------------------------------------------------------ */

#define MAX_TRACKED 8

typedef struct {
    FILE    *trn_fp;        /* game's FILE* for this .trn */
    int      bin_fd;        /* raw capture file fd */
    FILE    *log_fp;        /* write-log FILE* */
    uint64_t offset;        /* bytes written so far */
    char     nation[128];   /* stem of the .trn filename */

    /* accumulation buffer for pipe forwarding */
    uint8_t *buf;
    size_t   buf_cap;
    size_t   buf_len;
} Track;

static Track    tracks[MAX_TRACKED];
static int      n_tracks = 0;
static uint64_t load_base = 0;

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

static uint64_t find_load_base(void)
{
    FILE *maps = fopen("/proc/self/maps", "r");
    if (!maps) return 0;
    char line[512];
    uint64_t base = 0;
    while (fgets(line, sizeof(line), maps)) {
        if (strstr(line, "dom6_amd64") && strchr(line, 'x')) {
            sscanf(line, "%lx", &base);
            break;
        }
    }
    fclose(maps);
    return base;
}

static void nation_stem(const char *path, char *out, size_t outsz)
{
    const char *slash = strrchr(path, '/');
    const char *base  = slash ? slash + 1 : path;
    const char *dot   = strrchr(base, '.');
    size_t len = dot ? (size_t)(dot - base) : strlen(base);
    if (len >= outsz) len = outsz - 1;
    memcpy(out, base, len);
    out[len] = '\0';
}

static Track *find_track(FILE *fp)
{
    for (int i = 0; i < n_tracks; i++)
        if (tracks[i].trn_fp == fp) return &tracks[i];
    return NULL;
}

static void buf_append(Track *t, const void *data, size_t n)
{
    if (t->buf_len + n > t->buf_cap) {
        size_t newcap = t->buf_cap ? t->buf_cap * 2 : (1 << 18); /* 256 KiB */
        while (newcap < t->buf_len + n) newcap *= 2;
        uint8_t *nb = realloc(t->buf, newcap);
        if (!nb) return;
        t->buf     = nb;
        t->buf_cap = newcap;
    }
    memcpy(t->buf + t->buf_len, data, n);
    t->buf_len += n;
}

/* Forward the accumulated buffer to the named pipe (non-blocking open).
 * If no reader is listening, the open returns ENXIO and we skip silently. */
static void forward_to_pipe(Track *t)
{
    /* Ensure the pipe exists */
    mkfifo(PIPE_PATH, 0666);

    int pfd = open(PIPE_PATH, O_WRONLY | O_NONBLOCK);
    if (pfd < 0) return;   /* no reader — skip */

    /* Switch to blocking for the actual write so we don't partial-write */
    int flags = fcntl(pfd, F_GETFL);
    fcntl(pfd, F_SETFL, flags & ~O_NONBLOCK);

    uint16_t nlen  = (uint16_t)strlen(t->nation);
    uint32_t dlen  = (uint32_t)t->buf_len;

    write(pfd, "TRN!", 4);
    write(pfd, &nlen, 2);
    write(pfd, t->nation, nlen);
    write(pfd, &dlen, 4);
    write(pfd, t->buf, t->buf_len);

    close(pfd);
}

/* ------------------------------------------------------------------ */
/* Constructor                                                         */
/* ------------------------------------------------------------------ */

static void __attribute__((constructor)) hook_init(void)
{
    real_fopen   = dlsym(RTLD_NEXT, "fopen");
    real_fopen64 = dlsym(RTLD_NEXT, "fopen64");
    real_fwrite  = dlsym(RTLD_NEXT, "fwrite");
    real_fclose  = dlsym(RTLD_NEXT, "fclose");
    load_base    = find_load_base();
    mkfifo(PIPE_PATH, 0666);   /* pre-create; ignore EEXIST */
}

/* ------------------------------------------------------------------ */
/* fopen / fopen64                                                     */
/* ------------------------------------------------------------------ */

static FILE *hook_fopen_impl(const char *path, const char *mode,
                              FILE *(*real_fn)(const char *, const char *))
{
    FILE *f = real_fn(path, mode);
    if (!f || !path || !mode) return f;

    const char *ext = strrchr(path, '.');
    if (!ext || strcmp(ext, ".trn") != 0) return f;
    if (mode[0] != 'w') return f;

    pthread_mutex_lock(&lock);
    if (load_base == 0) load_base = find_load_base();

    if (n_tracks < MAX_TRACKED) {
        char stem[128];
        nation_stem(path, stem, sizeof(stem));

        char bin_path[256], log_path[256];
        snprintf(bin_path, sizeof(bin_path), "/tmp/dom6_trn_%s.bin", stem);
        snprintf(log_path, sizeof(log_path), "/tmp/dom6_trn_%s.log", stem);

        Track *t   = &tracks[n_tracks++];
        t->trn_fp  = f;
        t->offset  = 0;
        t->buf     = NULL;
        t->buf_cap = 0;
        t->buf_len = 0;
        memcpy(t->nation, stem, strlen(stem) < sizeof(t->nation) - 1
                                    ? strlen(stem) : sizeof(t->nation) - 1);
        t->nation[sizeof(t->nation) - 1] = '\0';

        t->bin_fd = open(bin_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        t->log_fp = real_fopen(log_path, "w");

        if (t->log_fp) {
            fprintf(t->log_fp,
                    "# dom6 .trn hook capture\n"
                    "# path=%s\n"
                    "# load_base=0x%lx\n"
                    "# format: off=HEX  len=DEC  caller_rva=HEX  data=HEX...\n",
                    path, load_base);
            fflush(t->log_fp);
        }
    }
    pthread_mutex_unlock(&lock);
    return f;
}

FILE *fopen(const char *path, const char *mode)
{
    if (!real_fopen) real_fopen = dlsym(RTLD_NEXT, "fopen");
    return hook_fopen_impl(path, mode, real_fopen);
}

FILE *fopen64(const char *path, const char *mode)
{
    if (!real_fopen64) real_fopen64 = dlsym(RTLD_NEXT, "fopen64");
    return hook_fopen_impl(path, mode, real_fopen64);
}

/* ------------------------------------------------------------------ */
/* fwrite                                                              */
/* ------------------------------------------------------------------ */

size_t fwrite(const void *buf, size_t size, size_t count, FILE *stream)
{
    size_t ret = real_fwrite(buf, size, count, stream);
    if (ret == 0) return ret;

    pthread_mutex_lock(&lock);
    Track *t = find_track(stream);
    if (t) {
        size_t nbytes = ret * size;

        /* raw file copy */
        if (t->bin_fd >= 0)
            write(t->bin_fd, buf, nbytes);

        /* accumulate for pipe forwarding */
        buf_append(t, buf, nbytes);

        /* log entry */
        if (t->log_fp) {
            void    *caller = __builtin_return_address(0);
            uint64_t rva    = (uint64_t)caller - load_base;

            fprintf(t->log_fp,
                    "off=0x%06lx  len=%4zu  caller_rva=0x%06lx  data=",
                    t->offset, nbytes, rva);
            const unsigned char *b = (const unsigned char *)buf;
            size_t show = nbytes < 16 ? nbytes : 16;
            for (size_t i = 0; i < show; i++)
                fprintf(t->log_fp, "%02x ", b[i]);
            fprintf(t->log_fp, "\n");
            fflush(t->log_fp);
        }

        t->offset += nbytes;
    }
    pthread_mutex_unlock(&lock);
    return ret;
}

/* ------------------------------------------------------------------ */
/* fclose — forward accumulated buffer to the pipe                    */
/* ------------------------------------------------------------------ */

int fclose(FILE *stream)
{
    pthread_mutex_lock(&lock);
    Track *t = find_track(stream);
    if (t) {
        if (t->log_fp) {
            fprintf(t->log_fp, "# total bytes written: %lu\n", t->offset);
            real_fclose(t->log_fp);
            t->log_fp = NULL;
        }
        if (t->bin_fd >= 0) {
            close(t->bin_fd);
            t->bin_fd = -1;
        }

        /* forward to pipe (must happen before we free buf) */
        forward_to_pipe(t);

        free(t->buf);
        t->buf     = NULL;
        t->buf_cap = 0;
        t->buf_len = 0;

        /* remove from table */
        int idx = (int)(t - tracks);
        tracks[idx] = tracks[--n_tracks];
    }
    pthread_mutex_unlock(&lock);
    return real_fclose(stream);
}
