/* Darwin 原生投影与独立无网络探针。旧 v1 CLI 继续在 ES 创建前拒绝。
 * owned-v2 有显式生产接线；boot、mute 与尾屏障待现场，编译不授运行资格。
 * 同一源码生成两个固定目标；探针目标不导入、不链接 EndpointSecurity。
 */

#import <Foundation/Foundation.h>
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef CLAUDE_PROBE_FIXTURE
#define CLAUDE_PROBE_FIXTURE 0
#endif

static const size_t maximumContextBytes = 65536;
#if CLAUDE_PROBE_FIXTURE
static int ownedFixtureMain(int argc, const char *argv[]);
#else
static int ownedCollectorMain(int argc, const char *argv[]);
#endif

static bool privatePipe(int fd) {
    struct stat info;
    return fd > 2 && fstat(fd, &info) == 0 && S_ISFIFO(info.st_mode) && info.st_uid == geteuid();
}

static bool parseFD(const char *text, int *out) {
    if (text == NULL || text[0] == '\0') return false;
    for (const char *at = text; *at; ++at) if (*at < '0' || *at > '9') return false;
    errno = 0;
    char *end = NULL;
    long value = strtol(text, &end, 10);
    if (errno || end == NULL || *end || value < 3 || value > INT_MAX) return false;
    *out = (int)value;
    return privatePipe(*out);
}

static bool writeAll(int fd, NSData *data) {
    const uint8_t *bytes = data.bytes;
    size_t left = data.length;
    while (left > 0) {
        ssize_t count = write(fd, bytes, left);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return false;
        bytes += (size_t)count;
        left -= (size_t)count;
    }
    return true;
}

static NSData *canonicalJSON(id value) {
    return [NSJSONSerialization dataWithJSONObject:value
        options:(NSJSONWritingSortedKeys | NSJSONWritingWithoutEscapingSlashes) error:NULL];
}

static bool writeRecord(int fd, NSDictionary *value) {
    NSData *raw = canonicalJSON(value);
    if (raw == nil || raw.length > maximumContextBytes) return false;
    NSMutableData *line = [raw mutableCopy];
    [line appendBytes:"\n" length:1];
    return writeAll(fd, line);
}

static NSData *readLine(int fd, size_t bound) {
    NSMutableData *raw = [NSMutableData data];
    while (raw.length < bound) {
        struct pollfd item = {.fd = fd, .events = POLLIN, .revents = 0};
        int available = poll(&item, 1, 15000);
        if (available < 0 && errno == EINTR) continue;
        if (available <= 0 || !(item.revents & POLLIN)) return nil;
        char byte = 0;
        ssize_t count = read(fd, &byte, 1);
        if (count < 0 && errno == EINTR) continue;
        if (count != 1) return nil;
        if (byte == '\n') return raw;
        [raw appendBytes:&byte length:1];
    }
    return nil;
}

#if CLAUDE_PROBE_FIXTURE

static bool ownImage(char *path) {
    uint32_t size = PATH_MAX;
    char original[PATH_MAX];
    struct stat info;
    return _NSGetExecutablePath(original, &size) == 0 && realpath(original, path) != NULL
        && lstat(path, &info) == 0 && S_ISREG(info.st_mode) && info.st_nlink == 1
        && info.st_uid == geteuid() && (info.st_mode & 0777) == 0755;
}

static int fixtureMain(int argc, const char *argv[]) {
    char image[PATH_MAX];
    if (!ownImage(image)) return 78;
    /* 多次 exec 的参数与空环境固定，不接受外部路径、argv 或用户环境。 */
    if (argc == 2 && strcmp(argv[1], "--probe-step-1") == 0) {
        char *const arguments[] = {image, "--probe-step-2", NULL};
        char *const environment[] = {NULL};
        execve(image, arguments, environment);
        return 78;
    }
    if (argc == 2 && strcmp(argv[1], "--probe-step-2") == 0) return 0;
    int controlFD = -1, receiptFD = -1;
    if (argc != 6 || strcmp(argv[1], "--owned-fixture") != 0
        || strcmp(argv[2], "--control-fd") != 0 || !parseFD(argv[3], &controlFD)
        || strcmp(argv[4], "--receipt-fd") != 0 || !parseFD(argv[5], &receiptFD)
        || controlFD == receiptFD) return 64;
    bool ran = false;
    for (;;) {
        NSData *raw = readLine(controlFD, 64);
        NSString *command = raw ? [[NSString alloc] initWithData:raw encoding:NSUTF8StringEncoding] : nil;
        if ([command isEqualToString:@"close"]) return ran ? 0 : 78;
        if (![command isEqualToString:@"run_owned_probe"] || ran) return 64;
        ran = true;
        pid_t child = fork();
        if (child < 0) return 78;
        if (child == 0) {
            char *const arguments[] = {image, "--probe-step-1", NULL};
            char *const environment[] = {NULL};
            execve(image, arguments, environment);
            _exit(78);
        }
        int status = 0;
        pid_t ended;
        do { ended = waitpid(child, &status, 0); } while (ended < 0 && errno == EINTR);
        if (ended != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0) return 78;
        if (!writeRecord(receiptFD, @{@"schema_version": @"claude-mac-owned-probe-step/internal-v1",
            @"program_id": @"fork-exec-exit-v1", @"step": @"naturally_exited",
            @"process_events_complete": @NO, @"registration_verified": @NO})) return 78;
    }
}

#else

#include <EndpointSecurity/EndpointSecurity.h>
#include <bsm/libbsm.h>

static NSString *nativeToken(es_string_token_t token, bool emptyAllowed) {
    if (token.length > 4096 || (!emptyAllowed && token.length == 0)
        || (token.length > 0 && token.data == NULL)) return nil;
    if (token.length == 0) return @"";
    if (memchr(token.data, 0, token.length) != NULL) return nil;
    NSString *value = [[NSString alloc] initWithBytes:token.data length:token.length encoding:NSUTF8StringEncoding];
    if (value == nil || [value rangeOfCharacterFromSet:[NSCharacterSet controlCharacterSet]].location != NSNotFound) return nil;
    return value;
}

static NSDictionary *nativeTime(struct timespec time) {
    if (time.tv_sec <= 0 || time.tv_nsec < 0 || time.tv_nsec >= 1000000000) return nil;
    return @{@"seconds": @(time.tv_sec), @"nanoseconds": @(time.tv_nsec)};
}

static NSDictionary *nativeProcess(const es_process_t *process, NSString *boot) {
    if (process == NULL || process->executable == NULL || boot == nil) return nil;
    const es_file_t *file = process->executable;
    NSString *path = nativeToken(file->path, false);
    NSString *signing = nativeToken(process->signing_id, true);
    NSString *team = nativeToken(process->team_id, true);
    NSDictionary *mtime = nativeTime(file->stat.st_mtimespec);
    pid_t pid = audit_token_to_pid(process->audit_token);
    pid_t parent = audit_token_to_pid(process->parent_audit_token);
    if (file->path_truncated || path == nil || ![path hasPrefix:@"/"] || signing == nil || team == nil
        || mtime == nil || pid <= 0 || file->stat.st_size < 0 || process->start_time.tv_sec <= 0
        || process->start_time.tv_usec < 0 || process->start_time.tv_usec >= 1000000) return nil;
    NSMutableString *cdhash = [NSMutableString stringWithCapacity:40];
    for (size_t at = 0; at < sizeof(process->cdhash); ++at) [cdhash appendFormat:@"%02x", process->cdhash[at]];
    id parentIdentity = parent > 0
        ? @{@"pid": @(parent), @"pidversion": @((uint32_t)audit_token_to_pidversion(process->parent_audit_token))}
        : (id)[NSNull null];
    return @{@"boot_session_uuid": boot, @"pid": @(pid),
        @"pidversion": @((uint32_t)audit_token_to_pidversion(process->audit_token)),
        @"fork_start_time": @{@"seconds": @(process->start_time.tv_sec), @"microseconds": @(process->start_time.tv_usec)},
        @"parent_audit_identity": parentIdentity,
        @"executable": @{@"path": path, @"path_truncated": @NO, @"device": @((uint64_t)file->stat.st_dev),
            @"inode": @((uint64_t)file->stat.st_ino), @"size": @((uint64_t)file->stat.st_size), @"mtime": mtime,
            @"cdhash": cdhash, @"codesigning_flags": @(process->codesigning_flags), @"signing_id": signing, @"team_id": team}};
}

/* 只投影公开字段；调用方必须先核消息版本，再读取版本字段和事件 union。
 * 旧 v1 不调用；owned-v2 仅在本实例有实际候选 boot 后投影，不宣称完整来源。
 */
NSDictionary *projectNativeMessage(const es_message_t *message, NSString *boot,
                                   NSString *collectorID, NSString *contextSHA) {
    if (message == NULL || message->version < 4 || message->version > 8
        || message->action_type != ES_ACTION_TYPE_NOTIFY || boot == nil) return nil;
    NSString *event = nil;
    const es_process_t *target = NULL;
    id exitStatus = [NSNull null];
    switch (message->event_type) {
        case ES_EVENT_TYPE_NOTIFY_FORK: event = @"notify_fork"; target = message->event.fork.child; break;
        case ES_EVENT_TYPE_NOTIFY_EXEC: event = @"notify_exec"; target = message->event.exec.target; break;
        case ES_EVENT_TYPE_NOTIFY_EXIT: event = @"notify_exit"; exitStatus = @(message->event.exit.stat); break;
        default: return nil;
    }
    NSDictionary *actor = nativeProcess(message->process, boot);
    id projectedTarget = target ? nativeProcess(target, boot) : (id)[NSNull null];
    NSDictionary *time = nativeTime(message->time);
    if (actor == nil || projectedTarget == nil || time == nil) return nil;
    return @{@"schema_version": @"claude-mac-native-process-projection/internal-v1",
        @"record_type": @"process_event", @"collector_run_id": collectorID, @"context_sha256": contextSHA,
        @"message_version": @(message->version), @"global_seq_num": @(message->global_seq_num),
        @"event_type": event, @"event_seq_num": @(message->seq_num), @"kernel_mach_time": @(message->mach_time),
        @"kernel_time": time, @"payload": @{@"actor": actor, @"target": projectedTarget, @"exit_stat": exitStatus}};
}

/* 固定 API 连接点只供后续资格化使用，本版 main 在它们之前拒绝。
 * 不订 AUTH、不解静默、不访问 exec argv／env 或 opaque，不提供可配置事件集。
 */
es_new_client_result_t createDiagnosticClient(es_client_t **client) {
    return es_new_client(client, ^(es_client_t *current, const es_message_t *message) {
        (void)current;
        (void)message;
    });
}

es_return_t subscribeFixedEvents(es_client_t *client) {
    const es_event_type_t events[] = {ES_EVENT_TYPE_NOTIFY_FORK, ES_EVENT_TYPE_NOTIFY_EXEC, ES_EVENT_TYPE_NOTIFY_EXIT};
    return es_subscribe(client, events, 3);
}

static int collectorMain(int argc, const char *argv[]) {
    int contextFD = -1, eventsFD = -1, controlFD = -1;
    bool probe = argc == 6 && strcmp(argv[1], "--probe") == 0;
    bool collect = argc == 8 && strcmp(argv[1], "--collect-owned-probe") == 0;
    if ((!probe && !collect) || strcmp(argv[2], "--context-fd") != 0 || !parseFD(argv[3], &contextFD)
        || strcmp(argv[4], "--events-fd") != 0 || !parseFD(argv[5], &eventsFD) || contextFD == eventsFD) return 64;
    if (collect && (strcmp(argv[6], "--control-fd") != 0 || !parseFD(argv[7], &controlFD)
                    || controlFD == contextFD || controlFD == eventsFD)) return 64;
    NSData *raw = readLine(contextFD, maximumContextBytes);
    if (raw == nil) return 64;
    id value = [NSJSONSerialization JSONObjectWithData:raw options:0 error:NULL];
    if (![value isKindOfClass:[NSDictionary class]]) return 64;
    NSDictionary *context = value;
    NSSet *fields = [NSSet setWithArray:@[@"task_sha256", @"task_nonce", @"producer_run_id", @"collector_run_id",
        @"deployment_id", @"tool_identity_sha256", @"build_identity_sha256", @"source_scope", @"probe_program_id", @"limits"]];
    if (![[NSSet setWithArray:context.allKeys] isEqualToSet:fields]
        || ![context[@"source_scope"] isEqual:@"internal_native_probe"]
        || ![context[@"probe_program_id"] isEqual:@"fork-exec-exit-v1"]
        || ![context[@"collector_run_id"] isKindOfClass:[NSString class]]) return 64;
    NSData *canonical = canonicalJSON(context);
    if (canonical == nil || ![canonical isEqualToData:raw]) return 64;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(canonical.bytes, (CC_LONG)canonical.length, digest);
    NSMutableString *sha = [NSMutableString stringWithCapacity:64];
    for (size_t at = 0; at < sizeof(digest); ++at) [sha appendFormat:@"%02x", digest[at]];
    /* 没有读取 boot 或创建 ES client；此记录仅说明本版入口关闭。 */
    NSDictionary *record = @{@"schema_version": @"claude-mac-native-process-projection/internal-v1",
        @"record_type": @"diagnostic", @"collector_run_id": context[@"collector_run_id"], @"context_sha256": sha,
        @"message_version": [NSNull null], @"global_seq_num": [NSNull null], @"event_type": [NSNull null],
        @"event_seq_num": [NSNull null], @"kernel_mach_time": [NSNull null], @"kernel_time": [NSNull null],
        @"payload": @{@"native_result": @"native_entry_disabled",
            @"missing_prerequisites": @[@"boot_representation_unqualified", @"mute_unqualified", @"closing_barrier_unqualified"]}};
    return writeRecord(eventsFD, record) ? 78 : 74;
}

#endif

int main(int argc, const char *argv[]) {
    @autoreleasepool {
#if CLAUDE_PROBE_FIXTURE
        if (argc > 1 && strcmp(argv[1], "--owned-stage") == 0) return ownedFixtureMain(argc, argv);
        return fixtureMain(argc, argv);
#else
        if (argc > 1 && (strcmp(argv[1], "--probe-v2") == 0 || strcmp(argv[1], "--collect-owned-probe-v2") == 0))
            return ownedCollectorMain(argc, argv);
        return collectorMain(argc, argv);
#endif
    }
}

#if CLAUDE_PROBE_FIXTURE

/* 新探针只有固定阶段与固定自 exec；外部路径、PID、环境与网络均不存在。 */
static int ownedFixtureMain(int argc, const char *argv[]) {
    char image[PATH_MAX];
    if (!ownImage(image) || argc < 3) return 64;
    if (argc == 3 && (strcmp(argv[2], "ready") == 0 || strcmp(argv[2], "closing") == 0)) return 0;
    if (argc != 5 || strcmp(argv[2], "work") != 0 || strcmp(argv[3], "--owned-step") != 0) return 64;
    if (strcmp(argv[4], "2") == 0) return 0;
    if (strcmp(argv[4], "1") == 0) {
        char *const arguments[] = {image, "--owned-stage", "work", "--owned-step", "2", NULL};
        char *const environment[] = {NULL};
        execve(image, arguments, environment);
        return 78;
    }
    if (strcmp(argv[4], "0") != 0) return 64;
    pid_t child = fork();
    if (child < 0) return 78;
    if (child == 0) {
        char *const arguments[] = {image, "--owned-stage", "work", "--owned-step", "1", NULL};
        char *const environment[] = {NULL};
        execve(image, arguments, environment);
        _exit(78);
    }
    int status = 0;
    pid_t ended;
    do { ended = waitpid(child, &status, 0); } while (ended < 0 && errno == EINTR);
    return ended == child && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : 78;
}

#else

#include <CoreFoundation/CoreFoundation.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <time.h>
#include <signal.h>

/* 回调仅复制有界公开值到队列；主线程创建、排空和删除 ES client。 */
@interface OwnedNativeState : NSObject
@property(atomic) NSDictionary *request;
@property(atomic) NSDictionary *bootObservation;
@property(atomic) NSString *boot;
@property(atomic) NSMutableArray<NSDictionary *> *queue;
@property(atomic) NSMutableDictionary<NSString *, NSDictionary *> *members;
@property(atomic) NSMutableSet<NSString *> *alive;
@property(atomic) NSMutableDictionary<NSString *, NSNumber *> *execCounts;
@property(atomic) NSMutableDictionary<NSString *, NSNumber *> *seenTypes;
@property(atomic) NSMutableDictionary<NSString *, NSNumber *> *writtenTypes;
@property(atomic) NSMutableSet<NSString *> *writtenAlive;
@property(atomic) NSString *firstError;
@property(atomic) NSNumber *firstErrorSequence;
@property(atomic) NSString *instanceSHA;
@property(atomic) NSNumber *lastGlobal;
@property(atomic) NSNumber *writtenGlobal;
@property(atomic) bool stopped;
@property(atomic) bool firstErrorWritten;
@property(atomic) bool outputFailed;
@property(atomic) bool instanceQueued;
@property(atomic) bool callbackClosed;
@property(atomic) uint64_t recordCount;
@property(atomic) uint64_t byteCount;
@property(atomic) uint64_t maxRecords;
@property(atomic) uint64_t maxBytes;
@property(atomic) uint64_t maxRecordBytes;
@property(atomic) uint64_t maxQueue;
@property(atomic) uint64_t maxRelations;
@property(atomic) uint64_t queueBytes;
@property(atomic) double deadline;
@property(atomic) int eventsFD;
@property(atomic) CC_SHA256_CTX prefixHash;
@property(atomic) NSUInteger phaseForks;
@property(atomic) NSUInteger phaseExecs;
@property(atomic) NSUInteger phaseExits;
@property(atomic) bool phaseMultiExec;
@end
@implementation OwnedNativeState
@end

static double monotonicNow(void) {
    struct timespec value;
    return clock_gettime(CLOCK_MONOTONIC, &value) == 0 ? (double)value.tv_sec + (double)value.tv_nsec / 1e9 : -1;
}

static NSString *shaData(NSData *raw) {
    if (raw == nil || raw.length > UINT32_MAX) return nil;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(raw.bytes, (CC_LONG)raw.length, digest);
    NSMutableString *value = [NSMutableString stringWithCapacity:64];
    for (size_t at = 0; at < sizeof(digest); ++at) [value appendFormat:@"%02x", digest[at]];
    return value;
}

static bool exactKeys(id value, NSArray<NSString *> *keys) {
    return [value isKindOfClass:[NSDictionary class]]
        && [[NSSet setWithArray:[value allKeys]] isEqualToSet:[NSSet setWithArray:keys]];
}

static bool uintBound(id value, uint64_t maximum, bool positive) {
    if (![value isKindOfClass:[NSNumber class]] || CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID()) return false;
    const char *type = [value objCType];
    if (!type || strcmp(type, "f") == 0 || strcmp(type, "d") == 0 || [value compare:@0] == NSOrderedAscending) return false;
    uint64_t number = [value unsignedLongLongValue];
    return number <= maximum && (!positive || number > 0);
}

static bool matches(NSString *value, NSString *pattern) {
    return [value isKindOfClass:[NSString class]]
        && [value rangeOfString:pattern options:NSRegularExpressionSearch].location != NSNotFound;
}

static NSData *readOwnedLine(int fd, size_t bound, double deadline) {
    NSMutableData *raw = [NSMutableData data];
    while (raw.length < bound) {
        double remaining = deadline - monotonicNow();
        if (remaining <= 0) return nil;
        struct pollfd item = {.fd = fd, .events = POLLIN, .revents = 0};
        int result = poll(&item, 1, (int)MIN(remaining * 1000, 1000));
        if (result < 0 && errno == EINTR) continue;
        if (result == 0) continue;
        if (result < 0 || !(item.revents & POLLIN)) return nil;
        char byte;
        ssize_t count = read(fd, &byte, 1);
        if (count < 0 && errno == EINTR) continue;
        if (count != 1) return nil;
        if (byte == '\n') return raw;
        [raw appendBytes:&byte length:1];
    }
    return nil;
}

static NSDictionary *strictOwnedJSON(NSData *raw) {
    id value = raw ? [NSJSONSerialization JSONObjectWithData:raw options:0 error:NULL] : nil;
    /* 精确规范字节还会拒绝重复键、浮点改写和额外尾部输入。 */
    return [value isKindOfClass:[NSDictionary class]] && [canonicalJSON(value) isEqual:raw] ? value : nil;
}

static bool ownedPipes(int contextFD, int eventsFD, int controlFD) {
    int fds[] = {contextFD, eventsFD, controlFD};
    struct stat previous[3];
    struct pipe_fdinfo pipes[3];
    for (size_t at = 0; at < 3; ++at) {
        if (fds[at] < 0) continue;
        if (fstat(fds[at], &previous[at]) != 0 || !S_ISFIFO(previous[at].st_mode)
            || previous[at].st_nlink != 0 || previous[at].st_uid != geteuid()) return false;
        memset(&pipes[at], 0, sizeof(pipes[at]));
        int count = proc_pidfdinfo(getpid(), fds[at], PROC_PIDFDPIPEINFO, &pipes[at], sizeof(pipes[at]));
        if (count != sizeof(pipes[at]) || pipes[at].pfi.fi_type != PROX_FDTYPE_PIPE
            || pipes[at].pipeinfo.pipe_handle == 0
            || (at > 0 && pipes[at].pipeinfo.pipe_peerhandle == 0)
            || pipes[at].pipeinfo.pipe_handle == pipes[at].pipeinfo.pipe_peerhandle) return false;
        for (size_t before = 0; before < at; ++before) {
            if (fds[before] < 0) continue;
            uint64_t current = pipes[at].pipeinfo.pipe_handle;
            uint64_t peer = pipes[at].pipeinfo.pipe_peerhandle;
            uint64_t prior = pipes[before].pipeinfo.pipe_handle;
            uint64_t priorPeer = pipes[before].pipeinfo.pipe_peerhandle;
            if ((previous[before].st_dev == previous[at].st_dev && previous[before].st_ino == previous[at].st_ino)
                || current == prior || current == priorPeer
                || (peer != 0 && (peer == prior || peer == priorPeer))) return false;
        }
    }
    /* 自有 FD 在此检查期间保持打开，且还未创建 ES 回调线程。
     * 同一管道的两端会出现互换的 handle/peerhandle，同端 dup 的 handle 相同；
     * 比较全部非零句柄，可拒绝两种混用。context 写端已自然关闭时 peer 合法为 0，
     * 但其相反端若仍在本进程的待核 FD 中，就不会消失。查询失败一律拒绝。
     * 这些值只证明当前管道实例相互隔离，不授予创建归属或来源 authority。
     */
    return true;
}

static bool boundFile(NSDictionary *role, NSString *expectedRole, NSString *expectedPath) {
    if (!exactKeys(role, @[@"role", @"material", @"mode", @"architecture", @"code_signature_present"])
        || ![role[@"role"] isEqual:expectedRole] || ![role[@"architecture"] isEqual:@"arm64"]
        || !uintBound(role[@"mode"], 0755, true) || [role[@"mode"] unsignedIntValue] != 0755
        || CFGetTypeID((__bridge CFTypeRef)role[@"code_signature_present"]) != CFBooleanGetTypeID()) return false;
    NSDictionary *material = role[@"material"];
    if (!exactKeys(material, @[@"path", @"sha256", @"bytes"])
        || ![material[@"path"] isEqual:expectedPath] || !matches(material[@"sha256"], @"^[0-9a-f]{64}$")
        || !uintBound(material[@"bytes"], 67108864, true)) return false;
    struct stat before, after, current;
    int fd = open(expectedPath.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return false;
    bool valid = fstat(fd, &before) == 0 && S_ISREG(before.st_mode) && before.st_uid == geteuid()
        && before.st_nlink == 1 && (before.st_mode & 0777) == 0755
        && before.st_size == [material[@"bytes"] longLongValue];
    CC_SHA256_CTX hash;
    CC_SHA256_Init(&hash);
    uint8_t buffer[16384];
    uint64_t size = 0;
    while (valid) {
        ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { valid = false; break; }
        if (count == 0) break;
        size += (uint64_t)count;
        if (size > 67108864) { valid = false; break; }
        CC_SHA256_Update(&hash, buffer, (CC_LONG)count);
    }
    valid = valid && fstat(fd, &after) == 0 && lstat(expectedPath.fileSystemRepresentation, &current) == 0
        && before.st_dev == after.st_dev && before.st_ino == after.st_ino
        && before.st_size == after.st_size && before.st_mode == after.st_mode
        && before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec
        && before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec
        && current.st_dev == after.st_dev && current.st_ino == after.st_ino && current.st_nlink == 1
        && current.st_size == after.st_size && size == (uint64_t)before.st_size;
    close(fd);
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &hash);
    NSMutableString *sha = [NSMutableString stringWithCapacity:64];
    for (size_t at = 0; at < sizeof(digest); ++at) [sha appendFormat:@"%02x", digest[at]];
    return valid && [sha isEqual:material[@"sha256"]];
}

static NSDictionary *readBoot(void) {
    uint8_t buffer[64] = {0};
    size_t length = sizeof(buffer);
    errno = 0;
    int result = sysctlbyname("kern.bootsessionuuid", buffer, &length, NULL, 0);
    int savedErrno = result == 0 ? 0 : errno;
    /* 失败时不把预填零缓冲视作实际内核返回。 */
    id raw = result == 0 && length <= sizeof(buffer)
        ? [[NSData dataWithBytes:buffer length:length] base64EncodedStringWithOptions:0] : (id)[NSNull null];
    NSString *boot = nil;
    if (result == 0 && length == 37 && buffer[36] == 0) {
        NSString *candidate = [[NSString alloc] initWithBytes:buffer length:36 encoding:NSASCIIStringEncoding];
        if (matches(candidate, @"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
            && ![candidate.lowercaseString isEqual:@"00000000-0000-0000-0000-000000000000"]) boot = candidate.lowercaseString;
    }
    return @{@"attempts": @[@{@"returncode": @(result), @"errno": @(savedErrno),
        @"returned_bytes": @(length), @"raw_base64": raw}],
        @"representation": boot ? @"canonical_uuid_string" : @"unknown", @"boot_session_uuid": boot ?: (id)[NSNull null]};
}

static NSDictionary *ownedFrame(OwnedNativeState *state, NSString *kind, NSDictionary *payload) {
    return @{@"schema_version": @"claude-mac-owned-process-projection/internal-v2", @"record_type": kind,
        @"collector_run_id": state.request[@"context"][@"collector_run_id"], @"context_sha256": state.request[@"context_sha256"],
        @"plan_sha256": state.request[@"plan_sha256"], @"message_version": [NSNull null], @"global_seq_num": [NSNull null],
        @"event_type": [NSNull null], @"event_seq_num": [NSNull null], @"kernel_mach_time": [NSNull null],
        @"kernel_time": [NSNull null], @"payload": payload};
}

static void firstError(OwnedNativeState *state, NSString *reason, NSNumber *sequence) {
    @synchronized (state) {
        if (!state.firstError) { state.firstError = reason; state.firstErrorSequence = sequence; }
        state.stopped = true;
    }
}

static NSString *processKey(NSDictionary *process) {
    return [NSString stringWithFormat:@"%@:%@:%@:%@", process[@"pid"], process[@"pidversion"],
        process[@"fork_start_time"][@"seconds"], process[@"fork_start_time"][@"microseconds"]];
}

static bool enqueue(OwnedNativeState *state, NSDictionary *frame) {
    NSUInteger size = canonicalJSON(frame).length + 1;
    if (state.queue.count >= state.maxQueue || size > state.maxRecordBytes || state.queueBytes + size > state.maxBytes) {
        firstError(state, state.queue.count >= state.maxQueue ? @"queue_overflow" : @"byte_limit", state.lastGlobal);
        return false;
    }
    [state.queue addObject:frame];
    state.queueBytes += size;
    return true;
}

static void handleOwnedMessage(OwnedNativeState *state, const es_message_t *message) {
    @synchronized (state) {
        if (state.stopped || state.callbackClosed) return;
        if (!message || message->version < 4 || message->version > 8 || message->action_type != ES_ACTION_TYPE_NOTIFY) {
            firstError(state, @"abi_unknown", nil); return;
        }
        NSString *event = message->event_type == ES_EVENT_TYPE_NOTIFY_FORK ? @"notify_fork"
            : message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC ? @"notify_exec"
            : message->event_type == ES_EVENT_TYPE_NOTIFY_EXIT ? @"notify_exit" : nil;
        if (!event || !message->process) { firstError(state, @"abi_unknown", nil); return; }
        uint64_t global = message->global_seq_num, perType = message->seq_num;
        if ((state.lastGlobal && global != state.lastGlobal.unsignedLongLongValue + 1)
            || (state.seenTypes[event] && perType != state.seenTypes[event].unsignedLongLongValue + 1)) {
            firstError(state, state.lastGlobal && global != state.lastGlobal.unsignedLongLongValue + 1
                ? @"global_sequence_gap" : @"type_sequence_gap", @(global)); return;
        }
        state.lastGlobal = @(global);
        state.seenTypes[event] = @(perType);
        pid_t pid = audit_token_to_pid(message->process->audit_token);
        NSString *key = [NSString stringWithFormat:@"%d:%u:%ld:%d", pid,
            (uint32_t)audit_token_to_pidversion(message->process->audit_token),
            message->process->start_time.tv_sec, message->process->start_time.tv_usec];
        bool root = pid == getpid();
        bool member = root || [state.alive containsObject:key];
        if (!member) {
            pid_t parent = audit_token_to_pid(message->process->parent_audit_token);
            for (NSDictionary *known in state.members.allValues) {
                if (parent == [known[@"pid"] intValue]
                    && (uint32_t)audit_token_to_pidversion(message->process->parent_audit_token) == [known[@"pidversion"] unsignedIntValue]) {
                    firstError(state, @"relation_unknown", @(global)); return;
                }
            }
        }
        NSMutableDictionary *frame = [ownedFrame(state, member ? @"process_event" : @"counter_only", @{}) mutableCopy];
        NSDictionary *time = nativeTime(message->time);
        if (!time) { firstError(state, @"abi_unknown", @(global)); return; }
        frame[@"message_version"] = @(message->version); frame[@"global_seq_num"] = @(global);
        frame[@"event_seq_num"] = @(perType); frame[@"event_type"] = event;
        frame[@"kernel_mach_time"] = @(message->mach_time); frame[@"kernel_time"] = time;
        if (!member) { (void)enqueue(state, frame); return; }
        NSDictionary *projection = projectNativeMessage(message, state.boot,
            state.request[@"context"][@"collector_run_id"], state.request[@"context_sha256"]);
        NSDictionary *payload = projection[@"payload"];
        NSDictionary *actor = payload[@"actor"];
        if (!payload || !actor) { firstError(state, @"native_path_truncated", @(global)); return; }
        key = processKey(actor);
        if (!state.instanceQueued) {
            if (!root || message->event_type != ES_EVENT_TYPE_NOTIFY_FORK) { firstError(state, @"relation_unknown", @(global)); return; }
            NSDictionary *instance = @{@"collector_process": actor, @"helper": state.request[@"helper"],
                @"probe_fixture": state.request[@"probe_fixture"], @"boot_observation": state.bootObservation};
            state.instanceSHA = shaData(canonicalJSON(instance));
            if (!enqueue(state, ownedFrame(state, @"collector_instance", instance))) return;
            state.members[key] = actor; [state.alive addObject:key]; state.instanceQueued = true;
        }
        if (![state.alive containsObject:key]) { firstError(state, @"relation_unknown", @(global)); return; }
        NSString *actorPath = actor[@"executable"][@"path"];
        if (![actorPath isEqual:state.request[@"helper"][@"material"][@"path"]]
            && ![actorPath isEqual:state.request[@"probe_fixture"][@"material"][@"path"]]) {
            firstError(state, @"relation_unknown", @(global)); return;
        }
        if (message->event_type == ES_EVENT_TYPE_NOTIFY_FORK) {
            NSDictionary *target = payload[@"target"];
            NSString *childKey = processKey(target);
            if (state.members.count >= state.maxRelations || state.members[childKey]) {
                firstError(state, @"relation_unknown", @(global)); return;
            }
            for (NSString *knownKey in state.alive) {
                if ([state.members[knownKey][@"pid"] isEqual:target[@"pid"]]) {
                    firstError(state, @"relation_unknown", @(global)); return;
                }
            }
            state.members[childKey] = target; [state.alive addObject:childKey]; state.phaseForks++;
        } else if (message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC) {
            NSDictionary *target = payload[@"target"];
            NSString *path = target[@"executable"][@"path"];
            if (![processKey(target) isEqual:key] || ![path isEqual:state.request[@"probe_fixture"][@"material"][@"path"]]) {
                firstError(state, @"relation_unknown", @(global)); return;
            }
            state.members[key] = target;
            NSUInteger count = [state.execCounts[key] unsignedIntegerValue] + 1;
            state.execCounts[key] = @(count); state.phaseExecs++;
            if (count >= 2) state.phaseMultiExec = true;
        } else { [state.alive removeObject:key]; state.phaseExits++; }
        frame[@"payload"] = payload;
        (void)enqueue(state, frame);
    }
}

static NSDictionary *prefixBinding(OwnedNativeState *state) {
    CC_SHA256_CTX hash = state.prefixHash;
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &hash);
    NSMutableString *sha = [NSMutableString stringWithCapacity:64];
    for (size_t at = 0; at < sizeof(digest); ++at) [sha appendFormat:@"%02x", digest[at]];
    return @{@"sha256": sha, @"bytes": @(state.byteCount), @"frame_count": @(state.recordCount)};
}

static NSDictionary *typeWatermarks(OwnedNativeState *state) {
    return @{@"fork": state.writtenTypes[@"notify_fork"] ?: (id)[NSNull null],
        @"exec": state.writtenTypes[@"notify_exec"] ?: (id)[NSNull null],
        @"exit": state.writtenTypes[@"notify_exit"] ?: (id)[NSNull null]};
}

static bool emitOwned(OwnedNativeState *state, NSDictionary *frame) {
    NSData *raw = canonicalJSON(frame);
    NSMutableData *line = raw ? [raw mutableCopy] : nil;
    [line appendBytes:"\n" length:1];
    if (!line || line.length > state.maxRecordBytes || state.byteCount + line.length > state.maxBytes
        || state.recordCount >= state.maxRecords) {
        firstError(state, state.recordCount >= state.maxRecords ? @"record_limit" : @"byte_limit", state.lastGlobal);
        state.outputFailed = true; return false;
    }
    const uint8_t *at = line.bytes;
    size_t left = line.length;
    while (left) {
        double remaining = state.deadline - monotonicNow();
        if (remaining <= 0) { firstError(state, @"deadline_exceeded", state.lastGlobal); state.outputFailed = true; return false; }
        struct pollfd item = {.fd = state.eventsFD, .events = POLLOUT, .revents = 0};
        int available = poll(&item, 1, (int)MIN(remaining * 1000, 100));
        if (available < 0 && errno == EINTR) continue;
        if (available == 0) continue;
        if (available < 0 || !(item.revents & POLLOUT)) { state.outputFailed = true; return false; }
        ssize_t count = write(state.eventsFD, at, left);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count <= 0) { state.outputFailed = true; return false; }
        at += count; left -= (size_t)count;
    }
    CC_SHA256_CTX hash = state.prefixHash;
    CC_SHA256_Update(&hash, line.bytes, (CC_LONG)line.length);
    state.prefixHash = hash; state.byteCount += line.length; state.recordCount++;
    if ([frame[@"record_type"] isEqual:@"process_event"] || [frame[@"record_type"] isEqual:@"counter_only"]) {
        state.writtenTypes[frame[@"event_type"]] = frame[@"event_seq_num"]; state.writtenGlobal = frame[@"global_seq_num"];
        if ([frame[@"record_type"] isEqual:@"process_event"]) {
            NSDictionary *payload = frame[@"payload"];
            if ([frame[@"event_type"] isEqual:@"notify_fork"]) [state.writtenAlive addObject:processKey(payload[@"target"])];
            if ([frame[@"event_type"] isEqual:@"notify_exit"]) [state.writtenAlive removeObject:processKey(payload[@"actor"])];
        }
    }
    return true;
}

static bool drainOwned(OwnedNativeState *state) {
    for (;;) {
        NSDictionary *frame = nil;
        @synchronized (state) {
            if (state.queue.count) {
                frame = state.queue.firstObject;
                state.queueBytes -= canonicalJSON(frame).length + 1;
                [state.queue removeObjectAtIndex:0];
            }
        }
        if (!frame) return !state.outputFailed;
        if (!emitOwned(state, frame)) return false;
    }
}

static NSDictionary *muteTable(es_client_t *client, bool paths, NSDictionary *request) {
    es_muted_paths_t *pathTable = NULL;
    es_muted_processes_t *processTable = NULL;
    es_return_t result = paths ? es_muted_paths_events(client, &pathTable) : es_muted_processes_events(client, &processTable);
    size_t count = paths ? (pathTable ? pathTable->count : 0) : (processTable ? processTable->count : 0);
    bool valid = result == ES_RETURN_SUCCESS && (paths ? pathTable != NULL : processTable != NULL) && count < 4096;
    NSMutableArray *copied = [NSMutableArray array];
    size_t examined = 0, relevant = 0;
    for (size_t at = 0; valid && at < count; ++at) {
        const es_event_type_t *events = NULL;
        size_t eventCount = 0;
        NSMutableDictionary *entry = [NSMutableDictionary dictionary];
        bool matched = false;
        if (paths) {
            if (!pathTable->paths) { valid = false; break; }
            const es_muted_path_t *item = &pathTable->paths[at];
            events = item->events; eventCount = item->event_count;
            NSString *path = nativeToken(item->path, false);
            if (!path || item->type > ES_MUTE_PATH_TYPE_TARGET_LITERAL) { valid = false; break; }
            bool prefix = item->type == ES_MUTE_PATH_TYPE_PREFIX || item->type == ES_MUTE_PATH_TYPE_TARGET_PREFIX;
            for (NSString *role in @[@"helper", @"probe_fixture"]) {
                NSString *image = request[role][@"material"][@"path"];
                matched |= prefix ? [image hasPrefix:path] : [image isEqual:path];
            }
            entry[@"path_type"] = @(item->type);
            entry[@"path_base64"] = [[NSData dataWithBytes:item->path.data length:item->path.length] base64EncodedStringWithOptions:0];
        } else {
            if (!processTable->processes) { valid = false; break; }
            const es_muted_process_t *item = &processTable->processes[at];
            events = item->events; eventCount = item->event_count;
            NSMutableArray *token = [NSMutableArray arrayWithCapacity:8];
            for (size_t word = 0; word < 8; ++word) [token addObject:@(item->audit_token.val[word])];
            entry[@"audit_token"] = token;
            /* 有 process mute 且尚无完整本实例 audit identity 时保守拒绝，不猜 PID 等价。 */
            matched = true;
        }
        if (eventCount >= 256 || (eventCount && !events)) { valid = false; break; }
        NSMutableArray *numbers = [NSMutableArray array];
        bool subscribedMatch = false;
        for (size_t index = 0; index < eventCount; ++index) {
            if (events[index] >= ES_EVENT_TYPE_LAST) { valid = false; break; }
            [numbers addObject:@(events[index])];
            subscribedMatch |= events[index] == ES_EVENT_TYPE_NOTIFY_FORK || events[index] == ES_EVENT_TYPE_NOTIFY_EXEC
                || events[index] == ES_EVENT_TYPE_NOTIFY_EXIT;
        }
        entry[@"events"] = numbers; [copied addObject:entry]; examined++;
        if (matched && subscribedMatch) relevant++;
    }
    if (pathTable) es_release_muted_paths(pathTable);
    if (processTable) es_release_muted_processes(processTable);
    return @{@"returncode": @(result), @"reported_count": @(count), @"examined_count": @(examined),
        @"table_sha256": valid ? shaData(canonicalJSON(copied)) : (id)[NSNull null],
        @"relevant_match_count": @(relevant), @"status": valid ? @"observed_unqualified" : @"unknown"};
}

static NSDictionary *readMute(es_client_t *client, NSDictionary *request, bool *clear) {
    NSDictionary *processes = muteTable(client, false, request), *paths = muteTable(client, true, request);
    es_mute_inverted_return_t process = es_muting_inverted(client, ES_MUTE_INVERSION_TYPE_PROCESS);
    es_mute_inverted_return_t path = es_muting_inverted(client, ES_MUTE_INVERSION_TYPE_PATH);
    es_mute_inverted_return_t target = es_muting_inverted(client, ES_MUTE_INVERSION_TYPE_TARGET_PATH);
    *clear = [processes[@"status"] isEqual:@"observed_unqualified"] && [paths[@"status"] isEqual:@"observed_unqualified"]
        && [processes[@"relevant_match_count"] unsignedIntegerValue] == 0 && [paths[@"relevant_match_count"] unsignedIntegerValue] == 0
        && process == ES_MUTE_NOT_INVERTED && path == ES_MUTE_NOT_INVERTED && target == ES_MUTE_NOT_INVERTED;
    return @{@"processes": processes, @"paths": paths, @"inversion": @{@"process": @(process), @"path": @(path), @"target_path": @(target)},
        @"query_status": *clear ? @"observed_unqualified" : @"unknown"};
}

static NSString *creationResult(es_new_client_result_t result) {
    switch (result) {
        case ES_NEW_CLIENT_RESULT_SUCCESS: return @"client_creation_succeeded_no_subscription";
        case ES_NEW_CLIENT_RESULT_ERR_INTERNAL: return @"es_internal_error";
        case ES_NEW_CLIENT_RESULT_ERR_INVALID_ARGUMENT: return @"es_invalid_argument";
        case ES_NEW_CLIENT_RESULT_ERR_NOT_ENTITLED: return @"es_not_entitled";
        case ES_NEW_CLIENT_RESULT_ERR_NOT_PERMITTED: return @"es_not_permitted";
        case ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED: return @"es_not_privileged";
        case ES_NEW_CLIENT_RESULT_ERR_TOO_MANY_CLIENTS: return @"es_too_many_clients";
        default: return @"es_unknown_result";
    }
}

static bool ownedRequestValid(NSDictionary *request, bool diagnose, double *deadline) {
    if (!exactKeys(request, @[@"schema_version", @"operation", @"context", @"context_sha256", @"plan_sha256",
        @"build_identity_sha256", @"helper", @"probe_fixture"]) || ![request[@"schema_version"] isEqual:@"claude-mac-owned-process-request/internal-v2"]
        || ![request[@"operation"] isEqual:diagnose ? @"diagnose" : @"collect_owned_probe"]) return false;
    NSDictionary *context = request[@"context"];
    if (!exactKeys(context, @[@"task_sha256", @"task_nonce", @"producer_run_id", @"collector_run_id", @"deployment_id",
        @"tool_identity_sha256", @"build_identity_sha256", @"source_scope", @"probe_program_id", @"limits"])
        || ![context[@"source_scope"] isEqual:@"internal_native_probe"] || ![context[@"probe_program_id"] isEqual:@"fork-exec-exit-v1"]
        || ![shaData(canonicalJSON(context)) isEqual:request[@"context_sha256"]]
        || ![context[@"build_identity_sha256"] isEqual:request[@"build_identity_sha256"]]) return false;
    for (NSString *name in @[@"task_sha256", @"tool_identity_sha256", @"build_identity_sha256"])
        if (!matches(context[name], @"^[0-9a-f]{64}$")) return false;
    for (NSString *name in @[@"task_nonce", @"producer_run_id", @"collector_run_id", @"deployment_id"])
        if (!matches(context[name], @"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")) return false;
    if (!matches(request[@"plan_sha256"], @"^[0-9a-f]{64}$")) return false;
    NSDictionary *limits = context[@"limits"];
    NSArray *names = @[@"max_record_bytes", @"max_total_bytes", @"max_records", @"max_queue_records", @"max_pending_relations", @"deadline_utc"];
    uint64_t bounds[] = {65536, 67108864, 65536, 4096, 256};
    if (!exactKeys(limits, names)) return false;
    for (size_t at = 0; at < 5; ++at) if (!uintBound(limits[names[at]], bounds[at], true)) return false;
    if ([limits[@"max_total_bytes"] unsignedLongLongValue] < [limits[@"max_record_bytes"] unsignedLongLongValue]) return false;
    NSString *stamp = limits[@"deadline_utc"];
    if (![stamp isKindOfClass:[NSString class]] || (![stamp hasSuffix:@"Z"] && ![stamp hasSuffix:@"+00:00"])) return false;
    NSISO8601DateFormatter *formatter = [[NSISO8601DateFormatter alloc] init];
    formatter.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
    NSDate *date = [formatter dateFromString:stamp];
    if (!date) { formatter.formatOptions = NSISO8601DateFormatWithInternetDateTime; date = [formatter dateFromString:stamp]; }
    if (!date) return false;
    *deadline = monotonicNow() + [date timeIntervalSinceNow];
    char original[PATH_MAX], image[PATH_MAX];
    uint32_t size = sizeof(original);
    if (_NSGetExecutablePath(original, &size) != 0 || !realpath(original, image)) return false;
    NSString *helper = [NSString stringWithUTF8String:image];
    NSString *probe = [helper.stringByDeletingLastPathComponent stringByAppendingPathComponent:@"probe_fixture"];
    char resolved[PATH_MAX];
    if (!realpath(probe.fileSystemRepresentation, resolved) || ![probe isEqual:[NSString stringWithUTF8String:resolved]]) return false;
    return [helper.lastPathComponent isEqual:@"collector"] && boundFile(request[@"helper"], @"collector", helper)
        && boundFile(request[@"probe_fixture"], @"probe_fixture", probe);
}

static bool runOwnedStage(OwnedNativeState *state, NSString *phase, int controlFD) {
    char image[PATH_MAX];
    if (!realpath([state.request[@"probe_fixture"][@"material"][@"path"] fileSystemRepresentation], image)) {
        firstError(state, @"relation_unknown", state.lastGlobal); return false;
    }
    if (!boundFile(state.request[@"probe_fixture"], @"probe_fixture", [NSString stringWithUTF8String:image])) {
        firstError(state, @"relation_unknown", state.lastGlobal); return false;
    }
    bool work = [phase isEqual:@"work"];
    const char *stage = phase.UTF8String;
    long descriptorLimit = sysconf(_SC_OPEN_MAX);
    if (descriptorLimit <= 0 || descriptorLimit > 65536) { firstError(state, @"abi_unknown", state.lastGlobal); return false; }
    @synchronized (state) { state.phaseForks = 0; state.phaseExecs = 0; state.phaseExits = 0; state.phaseMultiExec = false; }
    pid_t child = fork();
    if (child < 0) { firstError(state, @"relation_unknown", state.lastGlobal); return false; }
    if (child == 0) {
        for (int fd = 3; fd < descriptorLimit; ++fd) close(fd);
        char *const arguments[] = {image, "--owned-stage", (char *)stage, NULL};
        char *const workArguments[] = {image, "--owned-stage", "work", "--owned-step", "0", NULL};
        char *const environment[] = {NULL};
        execve(image, work ? workArguments : arguments, environment);
        _exit(78);
    }
    bool naturallyEnded = false;
    for (;;) {
        if (!drainOwned(state)) return false;
        if (state.stopped) return false;
        if (monotonicNow() >= state.deadline) { firstError(state, @"deadline_exceeded", state.lastGlobal); return false; }
        if (!naturallyEnded) {
            int status = 0;
            pid_t ended = waitpid(child, &status, WNOHANG);
            if (ended == child) {
                naturallyEnded = WIFEXITED(status) && WEXITSTATUS(status) == 0;
                if (!naturallyEnded) { firstError(state, @"observer_terminated", state.lastGlobal); return false; }
            } else if (ended < 0 && errno != EINTR) { firstError(state, @"observer_terminated", state.lastGlobal); return false; }
        }
        @synchronized (state) {
            if (naturallyEnded && state.alive.count == 1 && state.queue.count == 0 && state.phaseForks > 0
                && state.phaseExecs > 0 && state.phaseExits > 0 && (!work || state.phaseMultiExec)) return true;
        }
        struct pollfd control = {.fd = controlFD, .events = POLLIN, .revents = 0};
        int status = poll(&control, 1, 5);
        if (status < 0 && errno == EINTR) continue;
        if (status < 0 || (control.revents & (POLLHUP | POLLERR | POLLNVAL))) {
            firstError(state, @"control_channel_failed", state.lastGlobal); return false;
        }
    }
}

static bool emitBarrier(OwnedNativeState *state, NSString *phase) {
    return emitOwned(state, ownedFrame(state, @"barrier", @{@"phase": phase, @"type_watermarks": typeWatermarks(state),
        @"collector_instance_sha256": state.instanceSHA, @"event_prefix": prefixBinding(state)}));
}

static void emitFirstError(OwnedNativeState *state) {
    if (state.firstError && !state.firstErrorWritten) {
        state.firstErrorWritten = emitOwned(state, ownedFrame(state, @"incomplete", @{@"reason": state.firstError,
            @"first_error_sequence": state.firstErrorSequence ?: (id)[NSNull null]}));
    }
}

static int ownedCollectorMain(int argc, const char *argv[]) {
    int contextFD = -1, eventsFD = -1, controlFD = -1;
    bool diagnose = argc == 6 && strcmp(argv[1], "--probe-v2") == 0;
    bool collect = argc == 8 && strcmp(argv[1], "--collect-owned-probe-v2") == 0;
    if ((!diagnose && !collect) || strcmp(argv[2], "--context-fd") != 0 || !parseFD(argv[3], &contextFD)
        || strcmp(argv[4], "--events-fd") != 0 || !parseFD(argv[5], &eventsFD) || contextFD == eventsFD
        || (fcntl(contextFD, F_GETFL) & O_ACCMODE) != O_RDONLY || (fcntl(eventsFD, F_GETFL) & O_ACCMODE) != O_WRONLY) return 64;
    if (collect && (strcmp(argv[6], "--control-fd") != 0 || !parseFD(argv[7], &controlFD)
        || controlFD == contextFD || controlFD == eventsFD || (fcntl(controlFD, F_GETFL) & O_ACCMODE) != O_RDONLY)) return 64;
    if (!ownedPipes(contextFD, eventsFD, controlFD)) return 64;
    NSDictionary *request = strictOwnedJSON(readOwnedLine(contextFD, maximumContextBytes, monotonicNow() + 15));
    close(contextFD);
    double deadline = 0;
    if (!request || !ownedRequestValid(request, diagnose, &deadline)) return 64;
    /* 仅忽略本进程的写 pipe 错误信号；不用信号停止任何探针或外部进程。 */
    signal(SIGPIPE, SIG_IGN);
    OwnedNativeState *state = [[OwnedNativeState alloc] init];
    state.request = request; state.eventsFD = eventsFD; state.deadline = deadline;
    NSDictionary *limits = request[@"context"][@"limits"];
    state.maxRecords = [limits[@"max_records"] unsignedLongLongValue]; state.maxBytes = [limits[@"max_total_bytes"] unsignedLongLongValue];
    state.maxRecordBytes = [limits[@"max_record_bytes"] unsignedLongLongValue]; state.maxQueue = [limits[@"max_queue_records"] unsignedLongLongValue];
    state.maxRelations = [limits[@"max_pending_relations"] unsignedLongLongValue];
    state.queue = [NSMutableArray array]; state.members = [NSMutableDictionary dictionary]; state.alive = [NSMutableSet set];
    state.execCounts = [NSMutableDictionary dictionary]; state.seenTypes = [NSMutableDictionary dictionary];
    state.writtenTypes = [NSMutableDictionary dictionary]; state.writtenAlive = [NSMutableSet set];
    CC_SHA256_CTX hash; CC_SHA256_Init(&hash); state.prefixHash = hash;
    if (fcntl(eventsFD, F_SETFL, fcntl(eventsFD, F_GETFL) | O_NONBLOCK) < 0) return 74;
    state.bootObservation = readBoot();
    id boot = state.bootObservation[@"boot_session_uuid"];
    state.boot = [boot isKindOfClass:[NSString class]] ? boot : nil;
    if (monotonicNow() >= deadline) return 78;
    es_client_t *client = NULL;
    es_new_client_result_t created = es_new_client(&client,
        ^(es_client_t *current, const es_message_t *message) { (void)current; if (!diagnose) handleOwnedMessage(state, message); });
    if (diagnose || created != ES_NEW_CLIENT_RESULT_SUCCESS) {
        id deleted = client ? @(es_delete_client(client)) : (id)[NSNull null];
        NSDictionary *record = ownedFrame(state, @"diagnostic", @{@"native_result": creationResult(created),
            @"create_result": @(created), @"delete_result": deleted,
            @"boot_observation": state.bootObservation,
            @"missing_prerequisites": @[@"boot_representation_unqualified", @"mute_unqualified", @"closing_barrier_unqualified"]});
        return emitOwned(state, record) ? 78 : 74;
    }
    if (!state.boot) firstError(state, @"boot_unavailable", nil);
    bool subscriptionPerformed = !state.stopped;
    es_return_t subscribed = subscriptionPerformed ? subscribeFixedEvents(client) : ES_RETURN_ERROR;
    id subscriptionResult = subscriptionPerformed ? @(subscribed) : (id)[NSNull null];
    bool muteClear = false;
    NSDictionary *mute = readMute(client, request, &muteClear);
    if (subscribed != ES_RETURN_SUCCESS) firstError(state, @"observer_terminated", nil);
    if (!muteClear) firstError(state, @"mute_unknown", nil);
    (void)emitOwned(state, ownedFrame(state, @"state_observation", @{@"phase": @"before", @"boot_observation": state.bootObservation,
        @"mute_observation": mute, @"create_result": @(created), @"subscribe_result": subscriptionResult}));
    bool ready = !state.stopped && runOwnedStage(state, @"ready", controlFD) && emitBarrier(state, @"ready");
    bool workRan = false, closing = false;
    uint64_t sequence = 1;
    while (ready && !state.stopped) {
        if (!drainOwned(state)) break;
        struct pollfd item = {.fd = controlFD, .events = POLLIN, .revents = 0};
        int available = poll(&item, 1, 5);
        if (available < 0 && errno == EINTR) continue;
        if (monotonicNow() >= deadline) { firstError(state, @"deadline_exceeded", state.lastGlobal); break; }
        if (available == 0) continue;
        NSDictionary *control = available > 0 && (item.revents & POLLIN)
            ? strictOwnedJSON(readOwnedLine(controlFD, maximumContextBytes, deadline)) : nil;
        if (!exactKeys(control, @[@"schema_version", @"collector_run_id", @"context_sha256", @"plan_sha256", @"sequence", @"operation"])
            || ![control[@"schema_version"] isEqual:@"claude-mac-owned-process-control/internal-v2"]
            || ![control[@"collector_run_id"] isEqual:request[@"context"][@"collector_run_id"]]
            || ![control[@"context_sha256"] isEqual:request[@"context_sha256"]] || ![control[@"plan_sha256"] isEqual:request[@"plan_sha256"]]
            || !uintBound(control[@"sequence"], UINT64_MAX, true) || [control[@"sequence"] unsignedLongLongValue] != sequence++) {
            firstError(state, @"control_channel_failed", state.lastGlobal); break;
        }
        if ([control[@"operation"] isEqual:@"run_owned_probe"] && !workRan) {
            workRan = true; if (!runOwnedStage(state, @"work", controlFD)) break;
        } else if ([control[@"operation"] isEqual:@"close"]) {
            closing = runOwnedStage(state, @"closing", controlFD) && emitBarrier(state, @"closing"); break;
        } else { firstError(state, @"control_channel_failed", state.lastGlobal); break; }
    }
    if (closing) {
        NSDictionary *afterBoot = readBoot();
        bool afterClear = false;
        NSDictionary *afterMute = readMute(client, request, &afterClear);
        if (![afterBoot[@"boot_session_uuid"] isEqual:state.boot]) firstError(state, @"boot_changed", state.lastGlobal);
        if (!afterClear) firstError(state, @"mute_unknown", state.lastGlobal);
        emitFirstError(state);
        (void)emitOwned(state, ownedFrame(state, @"state_observation", @{@"phase": @"after", @"boot_observation": afterBoot,
            @"mute_observation": afterMute, @"create_result": @(created), @"subscribe_result": subscriptionResult}));
    }
    es_return_t unsubscribed = es_unsubscribe_all(client);
    /* 创建与删除均在 main 线程；delete 成功只表示资源释放，不承诺尾部完整。 */
    es_return_t deleted = es_delete_client(client);
    @synchronized (state) { state.callbackClosed = true; }
    (void)drainOwned(state);
    if (unsubscribed != ES_RETURN_SUCCESS || deleted != ES_RETURN_SUCCESS) firstError(state, @"observer_terminated", state.lastGlobal);
    if (!closing && !state.firstError) firstError(state, @"closing_barrier_missing", state.lastGlobal);
    emitFirstError(state);
    NSUInteger unclosed = state.writtenAlive.count;
    @synchronized (state) {
        NSUInteger observed = state.alive.count > 0 ? state.alive.count - 1 : 0;
        if (observed > unclosed) unclosed = observed;
    }
    (void)emitOwned(state, ownedFrame(state, @"closed", @{@"type_watermarks": typeWatermarks(state),
        @"unclosed_instances": @(unclosed), @"drain_status": state.firstError ? @"failed" : @"observed_unqualified",
        @"event_prefix": prefixBinding(state), @"unsubscribe_result": @(unsubscribed), @"delete_result": @(deleted)}));
    close(controlFD); close(eventsFD);
    return state.outputFailed ? 74 : state.firstError ? 78 : 0;
}

#endif
