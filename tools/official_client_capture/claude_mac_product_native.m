/* Darwin 产品观察器。只观测冻结产品及其子进程，不读取 argv、环境或凭据。
 * socket 是带内核身份的读时样本，不能冒充 connect/close 完整事件；JS 模块
 * 激活须再用产品原件闭合。未签名构建及 ES 创建失败均不能授正式来源资格。
 */
#import <Foundation/Foundation.h>
#import <EndpointSecurity/EndpointSecurity.h>
#import <ApplicationServices/ApplicationServices.h>
#include <bsm/libbsm.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <arpa/inet.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <time.h>

static NSData *json(id value) { return [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingSortedKeys error:nil]; }
static bool writeJSON(int fd, id value) {
    NSMutableData *raw = [json(value) mutableCopy];
    if (!raw || raw.length > 1048576) return false;
    [raw appendBytes:"\n" length:1];
    size_t at = 0;
    while (at < raw.length) {
        ssize_t n = write(fd, (const char *)raw.bytes + at, raw.length - at);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        at += (size_t)n;
    }
    return true;
}
static NSString *now(void) {
    NSISO8601DateFormatter *format = [[NSISO8601DateFormatter alloc] init];
    format.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
    return [format stringFromDate:[NSDate date]];
}
static double monotonicTime(void) {
    struct timespec value = {0};
    return clock_gettime(CLOCK_MONOTONIC_RAW, &value) == 0 ? value.tv_sec + value.tv_nsec / 1e9 : -1;
}
static id readJSON(int fd) {
    NSMutableData *raw = [NSMutableData data]; char byte;
    while (raw.length <= 65536) {
        ssize_t n = read(fd, &byte, 1);
        if (n < 0 && errno == EINTR) continue;
        if (n != 1) return nil;
        if (byte == '\n') return [NSJSONSerialization JSONObjectWithData:raw options:0 error:nil];
        [raw appendBytes:&byte length:1];
    }
    return nil;
}
static NSString *bootUUID(void) {
    char value[64] = {0}; size_t size = sizeof(value);
    if (sysctlbyname("kern.bootsessionuuid", value, &size, NULL, 0) != 0 || size != 37 || value[36] != 0) return nil;
    NSString *text = [[NSString alloc] initWithBytes:value length:36 encoding:NSASCIIStringEncoding];
    NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:text];
    return uuid ? uuid.UUIDString.lowercaseString : nil;
}
static NSString *token(es_string_token_t value) {
    if (!value.data || value.length == 0 || value.length > 4096 || memchr(value.data, 0, value.length)) return nil;
    return [[NSString alloc] initWithBytes:value.data length:value.length encoding:NSUTF8StringEncoding];
}
static NSDictionary *process(const es_process_t *p, NSString *boot) {
    if (!p || !p->executable || p->executable->path_truncated || !boot) return nil;
    NSString *path = token(p->executable->path);
    if (!path || ![path hasPrefix:@"/"] || p->start_time.tv_sec <= 0) return nil;
    NSMutableString *hash = [NSMutableString string];
    NSMutableArray *audit = [NSMutableArray array], *parentAudit = [NSMutableArray array];
    for (size_t i = 0; i < 8; i++) {
        [audit addObject:@(p->audit_token.val[i])]; [parentAudit addObject:@(p->parent_audit_token.val[i])];
    }
    for (size_t i = 0; i < sizeof(p->cdhash); i++) [hash appendFormat:@"%02x", p->cdhash[i]];
    return @{@"pid": @(audit_token_to_pid(p->audit_token)), @"ppid": @(p->ppid),
        @"pidversion": @(audit_token_to_pidversion(p->audit_token)), @"boot_id": boot,
        @"start_seconds": @(p->start_time.tv_sec), @"start_microseconds": @(p->start_time.tv_usec),
        @"executable": path, @"cdhash": hash, @"device": @(p->executable->stat.st_dev),
        @"inode": @(p->executable->stat.st_ino), @"size": @(p->executable->stat.st_size),
        @"audit_token": audit, @"parent_audit_token": parentAudit};
}
static NSDictionary *snapshot(pid_t pid, NSString *boot) {
    struct proc_bsdinfo before = {0}, after = {0}; char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &before, sizeof(before)) != sizeof(before)
        || proc_pidpath(pid, path, sizeof(path)) <= 0) return nil;
    int bytes = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, NULL, 0);
    if (bytes < 0 || bytes > 1048576) return nil;
    NSMutableData *buffer = [NSMutableData dataWithLength:(NSUInteger)bytes + sizeof(struct proc_fdinfo) * 32];
    int actual = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, buffer.mutableBytes, (int)buffer.length);
    if (actual < 0 || actual % sizeof(struct proc_fdinfo) != 0 || (NSUInteger)actual >= buffer.length) return nil;
    NSMutableArray *flows = [NSMutableArray array];
    struct proc_fdinfo *fds = buffer.mutableBytes;
    for (size_t i = 0; i < (size_t)actual / sizeof(*fds); i++) {
        if (fds[i].proc_fdtype != PROX_FDTYPE_SOCKET) continue;
        struct socket_fdinfo socket = {0};
        if (proc_pidfdinfo(pid, fds[i].proc_fd, PROC_PIDFDSOCKETINFO, &socket, sizeof(socket)) != sizeof(socket)) return nil;
        if (socket.psi.soi_kind != SOCKINFO_TCP) continue;
        struct in_sockinfo *in = &socket.psi.soi_proto.pri_tcp.tcpsi_ini;
        char local[INET6_ADDRSTRLEN] = {0}, peer[INET6_ADDRSTRLEN] = {0};
        int family = socket.psi.soi_family;
        const void *localAddress = family == AF_INET ? (const void *)&in->insi_laddr.ina_46.i46a_addr4 : (const void *)&in->insi_laddr.ina_6;
        const void *peerAddress = family == AF_INET ? (const void *)&in->insi_faddr.ina_46.i46a_addr4 : (const void *)&in->insi_faddr.ina_6;
        if ((family != AF_INET && family != AF_INET6) || !inet_ntop(family, localAddress, local, sizeof(local))
            || !inet_ntop(family, peerAddress, peer, sizeof(peer))) return nil;
        [flows addObject:@{@"fd": @(fds[i].proc_fd), @"socket_handle": @(socket.psi.soi_so),
            @"family": @(family), @"local_host": @(local), @"local_port": @(ntohs((uint16_t)in->insi_lport)),
            @"peer_host": @(peer), @"peer_port": @(ntohs((uint16_t)in->insi_fport)),
            @"tcp_state": @(socket.psi.soi_proto.pri_tcp.tcpsi_state)}];
    }
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &after, sizeof(after)) != sizeof(after)
        || before.pbi_pid != after.pbi_pid || before.pbi_ppid != after.pbi_ppid
        || before.pbi_start_tvsec != after.pbi_start_tvsec || before.pbi_start_tvusec != after.pbi_start_tvusec
        || ![bootUUID() isEqual:boot]) return nil;
    return @{@"pid": @(pid), @"ppid": @(before.pbi_ppid), @"boot_id": boot,
        @"start_seconds": @(before.pbi_start_tvsec), @"start_microseconds": @(before.pbi_start_tvusec),
        @"executable": @(path), @"observed_at_utc": now(), @"flows": flows,
        @"scope": @"kernel_socket_snapshot", @"socket_events_complete": @NO};
}

static int driveProductUI(void) {
    NSDictionary *request = readJSON(STDIN_FILENO);
    NSSet *keys = [NSSet setWithArray:@[@"task_sha256", @"pid", @"application_executable", @"challenge"]];
    if (![request isKindOfClass:[NSDictionary class]] || ![[NSSet setWithArray:request.allKeys] isEqual:keys]
        || ![request[@"challenge"] isKindOfClass:[NSString class]] || [request[@"challenge"] length] < 8
        || [request[@"challenge"] length] > 16384 || [request[@"pid"] intValue] <= 0) return 64;
    if (!AXIsProcessTrusted()) {
        writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"accessibility_unavailable"}); return 78;
    }
    NSString *boot = bootUUID();
    NSDictionary *before = boot ? snapshot([request[@"pid"] intValue], boot) : nil;
    if (!before || ![before[@"executable"] isEqual:request[@"application_executable"]]) return 78;
    AXUIElementRef app = AXUIElementCreateApplication([request[@"pid"] intValue]);
    CFTypeRef focused = NULL, role = NULL;
    AXError result = AXUIElementCopyAttributeValue(app, kAXFocusedUIElementAttribute, &focused);
    pid_t actualPID = 0;
    if (result != kAXErrorSuccess || !focused || CFGetTypeID(focused) != AXUIElementGetTypeID()
        || AXUIElementGetPid((AXUIElementRef)focused, &actualPID) != kAXErrorSuccess
        || actualPID != [request[@"pid"] intValue]
        || AXUIElementCopyAttributeValue((AXUIElementRef)focused, kAXRoleAttribute, &role) != kAXErrorSuccess
        || (!CFEqual(role, kAXTextAreaRole) && !CFEqual(role, kAXTextFieldRole))) {
        if (role) CFRelease(role); if (focused) CFRelease(focused); CFRelease(app);
        writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"focused_product_input_unknown"}); return 78;
    }
    result = AXUIElementSetAttributeValue((AXUIElementRef)focused, kAXValueAttribute, (__bridge CFStringRef)request[@"challenge"]);
    CFTypeRef value = NULL;
    bool readback = result == kAXErrorSuccess && AXUIElementCopyAttributeValue((AXUIElementRef)focused, kAXValueAttribute, &value) == kAXErrorSuccess
        && CFEqual(value, (__bridge CFStringRef)request[@"challenge"]);
    if (value) CFRelease(value); CFRelease(role); CFRelease(focused); CFRelease(app);
    NSDictionary *after = snapshot([request[@"pid"] intValue], boot);
    if (!readback || !after || ![before[@"start_seconds"] isEqual:after[@"start_seconds"]]
        || ![before[@"start_microseconds"] isEqual:after[@"start_microseconds"]]) return 78;
    CGEventRef down = CGEventCreateKeyboardEvent(NULL, 36, true), up = CGEventCreateKeyboardEvent(NULL, 36, false);
    if (!down || !up) { if (down) CFRelease(down); if (up) CFRelease(up); return 78; }
    CGEventPostToPid(actualPID, down); CGEventPostToPid(actualPID, up); CFRelease(down); CFRelease(up);
    writeJSON(STDOUT_FILENO, @{@"kind": @"input_observed", @"task_sha256": request[@"task_sha256"],
        @"process": before, @"observed_at_utc": now(), @"challenge": request[@"challenge"],
        @"completion_observed": @NO});
    return 0;
}

static int stopProduct(void) {
    NSDictionary *request = readJSON(STDIN_FILENO);
    NSSet *keys = [NSSet setWithArray:@[@"task_sha256", @"process"]];
    if (![request isKindOfClass:[NSDictionary class]] || ![[NSSet setWithArray:request.allKeys] isEqual:keys]) return 64;
    NSDictionary *identity = request[@"process"];
    NSArray *values = identity[@"audit_token"];
    if (![identity isKindOfClass:[NSDictionary class]] || ![values isKindOfClass:[NSArray class]] || values.count != 8) return 64;
    audit_token_t audit = {0};
    for (size_t i = 0; i < 8; i++) {
        if (![values[i] isKindOfClass:[NSNumber class]] || [values[i] unsignedLongLongValue] > UINT32_MAX) return 64;
        audit.val[i] = [values[i] unsignedIntValue];
    }
    if (audit_token_to_pid(audit) != [identity[@"pid"] intValue]
        || audit_token_to_pidversion(audit) != [identity[@"pidversion"] unsignedIntValue]) return 64;
    NSString *boot = bootUUID(); NSDictionary *before = boot ? snapshot(audit_token_to_pid(audit), boot) : nil;
    if (!before || ![before[@"boot_id"] isEqual:identity[@"boot_id"]]
        || ![before[@"start_seconds"] isEqual:identity[@"start_seconds"]]
        || ![before[@"start_microseconds"] isEqual:identity[@"start_microseconds"]]
        || ![before[@"executable"] isEqual:identity[@"executable"]]) return 78;
    int signal = 0, result = proc_terminate_with_audittoken(&audit, &signal), code = result == 0 ? 0 : errno;
    writeJSON(STDOUT_FILENO, @{@"kind": @"termination_requested", @"task_sha256": request[@"task_sha256"],
        @"audit_token": values, @"returncode": @(result), @"errno": @(code), @"signal": @(signal),
        @"observed_at_utc": now(), @"stopped": @NO});
    return result == 0 ? 0 : 78;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc == 2 && !strcmp(argv[1], "--product-ui-v1")) return driveProductUI();
        if (argc == 2 && !strcmp(argv[1], "--product-stop-v1")) return stopProduct();
        if (argc != 2 || strcmp(argv[1], "--product-events-v1")) return 64;
        signal(SIGPIPE, SIG_IGN);
        NSDictionary *request = readJSON(STDIN_FILENO);
        NSSet *fields = [NSSet setWithArray:@[@"schema_version", @"task_sha256", @"run_id", @"launcher_pid", @"product_log_root", @"application_executable", @"timeout_seconds", @"max_records"]];
        if (![request isKindOfClass:[NSDictionary class]] || ![[NSSet setWithArray:request.allKeys] isEqual:fields]
            || ![request[@"schema_version"] isEqual:@"claude-mac-product-native-request/v1"]
            || ![request[@"application_executable"] isKindOfClass:[NSString class]]
            || ![request[@"application_executable"] hasPrefix:@"/"]
            || ![request[@"product_log_root"] isKindOfClass:[NSString class]] || ![request[@"product_log_root"] hasPrefix:@"/"]
            || ![request[@"launcher_pid"] isKindOfClass:[NSNumber class]] || [request[@"launcher_pid"] intValue] <= 0
            || ![request[@"task_sha256"] isKindOfClass:[NSString class]] || [request[@"task_sha256"] length] != 64
            || ![request[@"run_id"] isKindOfClass:[NSString class]] || [request[@"run_id"] length] == 0
            || [request[@"timeout_seconds"] doubleValue] <= 0 || [request[@"timeout_seconds"] doubleValue] > 7200
            || [request[@"max_records"] unsignedLongLongValue] == 0 || [request[@"max_records"] unsignedLongLongValue] > 65536) return 64;
        NSString *boot = bootUUID();
        if (!boot) { writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"boot_unavailable"}); return 78; }
        NSLock *lock = [[NSLock alloc] init];
        NSMutableArray *queue = [NSMutableArray array]; NSMutableDictionary *members = [NSMutableDictionary dictionary];
        NSMutableDictionary *sequences = [NSMutableDictionary dictionary];
        __block NSString *firstError = nil; __block uint64_t total = 0; __block bool rootSeen = false;
        es_client_t *client = NULL;
        es_new_client_result_t created = es_new_client(&client, ^(es_client_t *current, const es_message_t *message) {
            (void)current; [lock lock];
            if (firstError) { [lock unlock]; return; }
            if (message->version < 4 || message->version > 8 || message->action_type != ES_ACTION_TYPE_NOTIFY) {
                firstError = @"message_version_unknown"; [lock unlock]; return;
            }
            NSString *kind = message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC ? @"exec"
                : message->event_type == ES_EVENT_TYPE_NOTIFY_FORK ? @"fork"
                : message->event_type == ES_EVENT_TYPE_NOTIFY_CLOSE ? @"file_close" : @"exit";
            NSNumber *old = sequences[kind];
            if (old && message->seq_num != old.unsignedLongLongValue + 1) firstError = @"event_sequence_gap";
            sequences[kind] = @(message->seq_num);
            const es_process_t *target = message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC ? message->event.exec.target
                : message->event_type == ES_EVENT_TYPE_NOTIFY_FORK ? message->event.fork.child : message->process;
            NSDictionary *value = process(target, boot);
            if (!value) { firstError = @"process_identity_unknown"; [lock unlock]; return; }
            NSNumber *pid = value[@"pid"], *parent = value[@"ppid"];
            if (message->event_type == ES_EVENT_TYPE_NOTIFY_CLOSE) {
                const es_file_t *file = message->event.close.target;
                NSString *path = file && !file->path_truncated ? token(file->path) : nil;
                bool ownedWriter = members[pid] && [members[pid][@"audit_token"] isEqual:value[@"audit_token"]];
                if (ownedWriter && message->event.close.modified && path
                    && [path hasPrefix:[request[@"product_log_root"] stringByAppendingString:@"/"]]
                    && [path hasSuffix:@".jsonl"]) {
                    if (++total > [request[@"max_records"] unsignedLongLongValue] || queue.count >= 1024) firstError = @"record_limit";
                    else [queue addObject:@{@"kind": @"product_file", @"process": value, @"path": path,
                        @"device": @(file->stat.st_dev), @"inode": @(file->stat.st_ino), @"size": @(file->stat.st_size),
                        @"observed_at_utc": now(), @"global_sequence": @(message->global_seq_num), @"event_sequence": @(message->seq_num)}];
                }
                [lock unlock]; return;
            }
            bool root = message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC && [value[@"executable"] isEqual:request[@"application_executable"]]
                && audit_token_to_pid(target->parent_audit_token) == [request[@"launcher_pid"] intValue];
            bool owned = members[pid] && [members[pid][@"audit_token"] isEqual:value[@"audit_token"]];
            bool child = members[parent] && [members[parent][@"audit_token"] isEqual:value[@"parent_audit_token"]];
            if (root && rootSeen && !owned) firstError = @"second_application_instance";
            if ((root || owned || child) && !firstError) {
                if (root) rootSeen = true;
                if (++total > [request[@"max_records"] unsignedLongLongValue] || queue.count >= 1024) firstError = @"record_limit";
                else {
                    [queue addObject:@{@"kind": @"process", @"event": kind, @"process": value,
                        @"observed_at_utc": now(), @"global_sequence": @(message->global_seq_num), @"event_sequence": @(message->seq_num)}];
                    if (message->event_type == ES_EVENT_TYPE_NOTIFY_EXIT) [members removeObjectForKey:pid];
                    else members[pid] = value;
                }
            }
            [lock unlock];
        });
        if (created != ES_NEW_CLIENT_RESULT_SUCCESS) {
            writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"es_create_failed", @"native_result": @(created)}); return 78;
        }
        es_event_type_t types[] = {ES_EVENT_TYPE_NOTIFY_EXEC, ES_EVENT_TYPE_NOTIFY_FORK, ES_EVENT_TYPE_NOTIFY_EXIT, ES_EVENT_TYPE_NOTIFY_CLOSE};
        es_return_t subscribed = es_subscribe(client, types, 4);
        if (subscribed != ES_RETURN_SUCCESS) { es_delete_client(client); return 78; }
        writeJSON(STDOUT_FILENO, @{@"kind": @"ready", @"task_sha256": request[@"task_sha256"],
            @"run_id": request[@"run_id"], @"boot_id": boot, @"observed_at_utc": now(),
            @"accessibility_trusted": @(AXIsProcessTrusted()), @"socket_events_complete": @NO,
            @"module_activation_verified": @NO});
        double started = monotonicTime(), deadline = started + [request[@"timeout_seconds"] doubleValue];
        if (started < 0) firstError = @"monotonic_clock_unavailable";
        bool closeRequested = false;
        while (!closeRequested && monotonicTime() >= 0 && monotonicTime() < deadline) {
            [lock lock]; NSArray *rows = [queue copy]; [queue removeAllObjects]; NSDictionary *owned = [members copy];
            NSString *error = firstError; [lock unlock];
            for (NSDictionary *row in rows) if (!writeJSON(STDOUT_FILENO, row)) { closeRequested = true; break; }
            if (error) { writeJSON(STDOUT_FILENO, @{@"kind": @"incomplete", @"reason": error}); break; }
            for (NSNumber *pid in owned) {
                NSDictionary *value = snapshot(pid.intValue, boot);
                NSDictionary *instance = owned[pid];
                if (value && [value[@"start_seconds"] isEqual:instance[@"start_seconds"]]
                    && [value[@"start_microseconds"] isEqual:instance[@"start_microseconds"]]
                    && [value[@"executable"] isEqual:instance[@"executable"]])
                    writeJSON(STDOUT_FILENO, @{@"kind": @"socket_snapshot", @"snapshot": value});
                /* 退出与快照之间的竞态不能补成零 socket，进程事件仍保存。 */
            }
            struct pollfd control = {.fd = STDIN_FILENO, .events = POLLIN};
            int status = poll(&control, 1, 10);
            if (status < 0 && errno == EINTR) continue;
            if (status < 0 || (control.revents & (POLLHUP | POLLERR))) break;
            if (status > 0 && (control.revents & POLLIN)) {
                NSDictionary *command = readJSON(STDIN_FILENO);
                closeRequested = [command isEqual:@{@"operation": @"close", @"task_sha256": request[@"task_sha256"]}];
                if (!closeRequested) { firstError = @"control_invalid"; break; }
            }
        }
        es_return_t unsubscribed = es_unsubscribe_all(client), deleted = es_delete_client(client);
        [lock lock]; NSArray *tail = [queue copy]; NSUInteger alive = members.count; NSString *error = firstError; [lock unlock];
        for (NSDictionary *row in tail) writeJSON(STDOUT_FILENO, row);
        writeJSON(STDOUT_FILENO, @{@"kind": @"closed", @"task_sha256": request[@"task_sha256"],
            @"run_id": request[@"run_id"], @"observed_at_utc": now(), @"close_requested": @(closeRequested),
            @"unsubscribe_result": @(unsubscribed), @"delete_result": @(deleted), @"unclosed_instances": @(alive),
            @"first_error": error ?: (id)[NSNull null], @"drain_status": @"observed_unqualified",
            @"socket_events_complete": @NO, @"module_activation_verified": @NO});
        return closeRequested && !error && unsubscribed == ES_RETURN_SUCCESS && deleted == ES_RETURN_SUCCESS ? 0 : 78;
    }
}
