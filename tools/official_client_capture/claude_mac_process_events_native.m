/* Darwin 原生投影与独立无网络探针。当前 CLI 在任何 ES 创建前明确拒绝。
 * boot 返回表示、mute 与收尾屏障待现场资格化；编译通过不能解除该拒绝。
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
 * 当前没有合格 boot 来源或实例范围，因此入口不会调用此投影来宣称实际来源。
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
        return fixtureMain(argc, argv);
#else
        return collectorMain(argc, argv);
#endif
    }
}
