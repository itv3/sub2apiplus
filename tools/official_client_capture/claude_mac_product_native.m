/* Darwin 产品观察器。只观测冻结产品及其子进程，不读取 argv、环境或凭据。
 * socket 是带内核身份的读时样本，不能冒充 connect/close 完整事件；JS 模块
 * 激活须再用产品原件闭合。未签名构建及 ES 创建失败均不能授正式来源资格。
 */
#import <Foundation/Foundation.h>
#import <EndpointSecurity/EndpointSecurity.h>
#import <ApplicationServices/ApplicationServices.h>
#import <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>
#include <bsm/libbsm.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/mman.h>
#include <sys/xattr.h>
#include <dirent.h>
#include <arpa/inet.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <time.h>

static CC_SHA256_CTX observationHash;
static uint64_t observationBytes=0,observationRecords=0;
static bool observationActive=false;
static NSData *json(id value) { return [NSJSONSerialization dataWithJSONObject:value
    options:NSJSONWritingSortedKeys|NSJSONWritingWithoutEscapingSlashes error:nil]; }
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
    if(fd==STDOUT_FILENO && observationActive){
        CC_SHA256_Update(&observationHash,raw.bytes,(CC_LONG)raw.length);
        observationBytes+=raw.length;observationRecords++;
    }
    return true;
}
static NSString *sha(NSData *raw) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];CC_SHA256(raw.bytes,(CC_LONG)raw.length,digest);
    NSMutableString *value=[NSMutableString string];
    for(NSUInteger i=0;i<sizeof(digest);i++)[value appendFormat:@"%02x",digest[i]];return value;
}
static SecKeyRef sourceKey(void) {
    NSData *tag=[@"com.sub2apiplus.claude.product-observer.origin-v1" dataUsingEncoding:NSUTF8StringEncoding];
    NSDictionary *query=@{(__bridge id)kSecClass:(__bridge id)kSecClassKey,
        (__bridge id)kSecAttrApplicationTag:tag,
        (__bridge id)kSecAttrKeyType:(__bridge id)kSecAttrKeyTypeECSECPrimeRandom,
        (__bridge id)kSecReturnRef:@YES,(__bridge id)kSecUseAuthenticationUI:(__bridge id)kSecUseAuthenticationUIFail};
    CFTypeRef key=NULL;
    if(SecItemCopyMatching((__bridge CFDictionaryRef)query,&key)!=errSecSuccess || !key
        || CFGetTypeID(key)!=SecKeyGetTypeID()){if(key)CFRelease(key);return NULL;}return (SecKeyRef)key;
}
static bool sealObservations(SecKeyRef key,NSDictionary *context,NSString *firstError) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];CC_SHA256_Final(digest,&observationHash);
    NSMutableString *hash=[NSMutableString string];
    for(NSUInteger i=0;i<sizeof(digest);i++)[hash appendFormat:@"%02x",digest[i]];
    NSData *payload=json(@{@"schema_version":@"claude-mac-product-native-attestation/v1",@"context":context,
        @"record_count":@(observationRecords),@"raw_bytes":@(observationBytes),@"stream_sha256":hash,
        @"first_error":firstError ?: (id)NSNull.null});
    CFErrorRef error=NULL;
    CFDataRef signature=key ? SecKeyCreateSignature(key,kSecKeyAlgorithmECDSASignatureMessageX962SHA256,
        (__bridge CFDataRef)payload,&error) : NULL;
    if(error){CFRelease(error);error=NULL;}
    SecKeyRef publicKey=key ? SecKeyCopyPublicKey(key) : NULL;
    CFDataRef publicBytes=publicKey ? SecKeyCopyExternalRepresentation(publicKey,&error) : NULL;
    observationActive=false;
    bool result=signature && publicBytes && !error && writeJSON(STDOUT_FILENO,@{@"kind":@"origin_attestation",
        @"payload_base64":[payload base64EncodedStringWithOptions:0],
        @"signature_base64":[(__bridge NSData *)signature base64EncodedStringWithOptions:0],
        @"public_key_sha256":sha((__bridge NSData *)publicBytes)});
    if(signature)CFRelease(signature);if(publicBytes)CFRelease(publicBytes);if(publicKey)CFRelease(publicKey);
    if(error)CFRelease(error);return result;
}
static NSString *now(void) {
    NSISO8601DateFormatter *format = [[NSISO8601DateFormatter alloc] init];
    format.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
    return [format stringFromDate:[NSDate date]];
}
static NSString *eventTime(const es_message_t *message) {
    if(message->time.tv_sec<=0 || message->time.tv_nsec<0 || message->time.tv_nsec>=1000000000)return nil;
    NSISO8601DateFormatter *format=[NSISO8601DateFormatter new];
    format.formatOptions=NSISO8601DateFormatWithInternetDateTime|NSISO8601DateFormatWithFractionalSeconds;
    return [format stringFromDate:[NSDate dateWithTimeIntervalSince1970:message->time.tv_sec+message->time.tv_nsec/1e9]];
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
static NSString *fileSHA(const es_file_t *file) {
    /* 只对本轮产品真实关闭的有界普通原件取字节；与内核 stat 不符的竞态
     * 不作为稳定文件原件发出，后续实际关闭可再次形成有效观测。 */
    NSString *path=file && !file->path_truncated ? token(file->path) : nil;
    if(!path || !S_ISREG(file->stat.st_mode) || file->stat.st_size<0 || file->stat.st_size>32*1024*1024)return nil;
    int fd=open(path.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    struct stat before={0},after={0};
    bool valid=fd>=0 && fstat(fd,&before)==0 && S_ISREG(before.st_mode) && before.st_nlink==1
        && before.st_dev==file->stat.st_dev && before.st_ino==file->stat.st_ino && before.st_size==file->stat.st_size
        && before.st_mtimespec.tv_sec==file->stat.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec==file->stat.st_mtimespec.tv_nsec
        && before.st_ctimespec.tv_sec==file->stat.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==file->stat.st_ctimespec.tv_nsec;
    CC_SHA256_CTX state;CC_SHA256_Init(&state);unsigned char buffer[65536],digest[CC_SHA256_DIGEST_LENGTH];
    ssize_t count=0;uint64_t total=0;
    while(valid && (count=read(fd,buffer,sizeof(buffer)))>0){total+=count;if(total>32*1024*1024)valid=false;
        else CC_SHA256_Update(&state,buffer,(CC_LONG)count);}
    valid=valid && count==0 && total==before.st_size && fstat(fd,&after)==0
        && before.st_dev==after.st_dev && before.st_ino==after.st_ino && before.st_size==after.st_size
        && before.st_mtimespec.tv_sec==after.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec==after.st_mtimespec.tv_nsec
        && before.st_ctimespec.tv_sec==after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==after.st_ctimespec.tv_nsec;
    if(fd>=0)close(fd);if(!valid)return nil;CC_SHA256_Final(digest,&state);
    NSMutableString *value=[NSMutableString string];for(NSUInteger i=0;i<sizeof(digest);i++)[value appendFormat:@"%02x",digest[i]];return value;
}
static NSString *filePath(const es_file_t *file) {
    return file && !file->path_truncated ? token(file->path) : nil;
}
static NSDictionary *fileFact(const es_file_t *file, bool closed) {
    if(!file)return nil;
    NSString *kind=S_ISREG(file->stat.st_mode) ? @"file" : S_ISDIR(file->stat.st_mode) ? @"directory" : @"unsupported";
    return @{ @"type":kind,@"mode":@(file->stat.st_mode & 07777),
        @"mtime_ns":@((uint64_t)file->stat.st_mtimespec.tv_sec*1000000000+file->stat.st_mtimespec.tv_nsec),
        @"bytes":@(MAX(0,file->stat.st_size)),@"device":@(file->stat.st_dev),@"inode":@(file->stat.st_ino),
        @"sha256":closed ? (fileSHA(file) ?: (id)NSNull.null) : (id)NSNull.null };
}
static NSString *childPath(const es_file_t *dir,es_string_token_t name) {
    NSString *parent=filePath(dir),*leaf=token(name);
    return parent && leaf && ![leaf containsString:@"/"] && ![leaf isEqual:@"."] && ![leaf isEqual:@".."]
        ? [parent stringByAppendingPathComponent:leaf] : nil;
}
static NSDictionary *stateEvent(const es_message_t *m) {
    NSString *event=nil,*path=nil,*other=nil;const es_file_t *file=NULL;NSDictionary *requested=nil;bool closed=false;
    switch(m->event_type) {
        case ES_EVENT_TYPE_NOTIFY_CREATE:
            event=@"create";
            if(m->event.create.destination_type==ES_DESTINATION_TYPE_EXISTING_FILE)file=m->event.create.destination.existing_file;
            else {path=childPath(m->event.create.destination.new_path.dir,m->event.create.destination.new_path.filename);
                requested=@{@"unapplied":@YES};}break;
        case ES_EVENT_TYPE_NOTIFY_WRITE:event=@"write";file=m->event.write.target;break;
        case ES_EVENT_TYPE_NOTIFY_CLOSE:
            if(!m->event.close.modified && !(m->version>=6 && m->event.close.was_mapped_writable))return @{};
            event=@"close";file=m->event.close.target;closed=true;break;
        case ES_EVENT_TYPE_NOTIFY_MMAP:
            if(!(m->event.mmap.protection & PROT_WRITE) || !(m->event.mmap.flags & MAP_SHARED))return @{};
            event=@"mmap";file=m->event.mmap.source;break;
        case ES_EVENT_TYPE_NOTIFY_TRUNCATE:event=@"truncate";file=m->event.truncate.target;break;
        case ES_EVENT_TYPE_NOTIFY_UNLINK:event=@"unlink";file=m->event.unlink.target;break;
        case ES_EVENT_TYPE_NOTIFY_RENAME:event=@"rename";file=m->event.rename.source;
            other=m->event.rename.destination_type==ES_DESTINATION_TYPE_EXISTING_FILE
                ? filePath(m->event.rename.destination.existing_file)
                : childPath(m->event.rename.destination.new_path.dir,m->event.rename.destination.new_path.filename);break;
        case ES_EVENT_TYPE_NOTIFY_SETMODE:event=@"setmode";file=m->event.setmode.target;requested=@{@"mode":@(m->event.setmode.mode & 07777)};break;
        case ES_EVENT_TYPE_NOTIFY_UTIMES:event=@"utimes";file=m->event.utimes.target;
            requested=@{@"mtime_ns":@((uint64_t)m->event.utimes.mtime.tv_sec*1000000000+m->event.utimes.mtime.tv_nsec)};break;
        case ES_EVENT_TYPE_NOTIFY_LINK:event=@"link";file=m->event.link.source;
            other=childPath(m->event.link.target_dir,m->event.link.target_filename);break;
        case ES_EVENT_TYPE_NOTIFY_SETOWNER:event=@"setowner";file=m->event.setowner.target;break;
        case ES_EVENT_TYPE_NOTIFY_SETFLAGS:event=@"setflags";file=m->event.setflags.target;break;
        case ES_EVENT_TYPE_NOTIFY_SETATTRLIST:event=@"setattrlist";file=m->event.setattrlist.target;break;
        case ES_EVENT_TYPE_NOTIFY_SETACL:event=@"setacl";file=m->event.setacl.target;break;
        case ES_EVENT_TYPE_NOTIFY_SETEXTATTR:event=@"setextattr";file=m->event.setextattr.target;break;
        case ES_EVENT_TYPE_NOTIFY_DELETEEXTATTR:event=@"deleteextattr";file=m->event.deleteextattr.target;break;
        case ES_EVENT_TYPE_NOTIFY_EXCHANGEDATA:event=@"exchangedata";file=m->event.exchangedata.file1;other=filePath(m->event.exchangedata.file2);break;
        case ES_EVENT_TYPE_NOTIFY_CLONE:event=@"clone";file=m->event.clone.source;other=childPath(m->event.clone.target_dir,m->event.clone.target_name);break;
        case ES_EVENT_TYPE_NOTIFY_COPYFILE:event=@"copyfile";file=m->event.copyfile.source;other=childPath(m->event.copyfile.target_dir,m->event.copyfile.target_name);break;
        default:return @{};
    }
    if(!path)path=filePath(file);
    if(!path || (([event isEqual:@"rename"] || [event isEqual:@"link"] || [event isEqual:@"exchangedata"]
        || [event isEqual:@"clone"] || [event isEqual:@"copyfile"]) && !other))return nil;
    return @{@"event":event,@"paths":other ? @[path,other] : @[path],@"file":file ? fileFact(file,closed) : (id)NSNull.null,
        @"requested":requested ?: (id)NSNull.null};
}
static bool scoped(NSString *path,NSArray *roots) {
    for(NSString *root in roots)if([path isEqual:root] || [path hasPrefix:[root stringByAppendingString:@"/"]])return true;
    return false;
}
static bool stableStat(struct stat a,struct stat b) {
    return a.st_dev==b.st_dev && a.st_ino==b.st_ino && a.st_mode==b.st_mode && a.st_size==b.st_size
        && a.st_mtimespec.tv_sec==b.st_mtimespec.tv_sec && a.st_mtimespec.tv_nsec==b.st_mtimespec.tv_nsec
        && a.st_ctimespec.tv_sec==b.st_ctimespec.tv_sec && a.st_ctimespec.tv_nsec==b.st_ctimespec.tv_nsec;
}
static bool snapshotNode(int parent,NSString *leaf,NSString *path,NSMutableDictionary *states,
    uid_t user,int depth,uint64_t *entries,uint64_t *bytes) {
    if(depth>16 || ++*entries>4096)return false;
    struct stat info={0};
    if(fstatat(parent,leaf.fileSystemRepresentation,&info,AT_SYMLINK_NOFOLLOW)<0){
        if(errno!=ENOENT)return false;states[path]=@{@"type":@"missing"};return true;
    }
    if(info.st_uid!=user || (info.st_mode & 07000) || info.st_flags || (!S_ISREG(info.st_mode) && !S_ISDIR(info.st_mode)))return false;
    int fd=openat(parent,leaf.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|(S_ISDIR(info.st_mode)?O_DIRECTORY:0));
    struct stat opened={0},ended={0},again={0};
    if(fd<0 || fstat(fd,&opened)<0 || !stableStat(info,opened) || flistxattr(fd,NULL,0,0)!=0){if(fd>=0)close(fd);return false;}
    NSMutableDictionary *value=[@{@"type":S_ISDIR(info.st_mode)?@"directory":@"file",@"mode":@(info.st_mode & 0777),
        @"mtime_ns":@((uint64_t)info.st_mtimespec.tv_sec*1000000000+info.st_mtimespec.tv_nsec)} mutableCopy];
    bool valid=true;
    if(S_ISREG(info.st_mode)){
        valid=info.st_nlink==1 && info.st_size>=0 && info.st_size<=8*1024*1024;
        CC_SHA256_CTX hash;CC_SHA256_Init(&hash);unsigned char buffer[65536],digest[CC_SHA256_DIGEST_LENGTH];ssize_t count=0;uint64_t size=0;
        while(valid && (count=read(fd,buffer,sizeof(buffer)))>0){size+=count;valid=size<=8*1024*1024;
            if(valid)CC_SHA256_Update(&hash,buffer,(CC_LONG)count);}
        *bytes+=size;valid=valid && count==0 && size==info.st_size && *bytes<=32*1024*1024;
        if(valid){CC_SHA256_Final(digest,&hash);NSMutableString *text=[NSMutableString string];
            for(NSUInteger i=0;i<sizeof(digest);i++)[text appendFormat:@"%02x",digest[i]];
            value[@"bytes"]=@(size);value[@"sha256"]=text;}
    }else{
        int copy=dup(fd);DIR *dir=copy>=0 ? fdopendir(copy) : NULL;struct dirent *entry=NULL;
        if(!dir){if(copy>=0)close(copy);valid=false;}
        if(dir){errno=0;while(valid && (entry=readdir(dir))){
            if(!strcmp(entry->d_name,".") || !strcmp(entry->d_name,".."))continue;
            NSString *name=[[NSString alloc] initWithBytes:entry->d_name length:strlen(entry->d_name) encoding:NSUTF8StringEncoding];
            valid=name && snapshotNode(fd,name,[path stringByAppendingPathComponent:name],states,user,depth+1,entries,bytes);errno=0;
        }if(errno)valid=false;closedir(dir);}
    }
    valid=valid && fstat(fd,&ended)==0 && stableStat(info,ended)
        && fstatat(parent,leaf.fileSystemRepresentation,&again,AT_SYMLINK_NOFOLLOW)==0 && stableStat(info,again);
    close(fd);if(valid)states[path]=value;return valid;
}
static NSDictionary *snapshotState(NSArray *roots,uid_t user) {
    NSMutableDictionary *states=[NSMutableDictionary dictionary];uint64_t entries=0,bytes=0;
    for(NSString *path in roots){NSArray *parts=[path pathComponents];int fd=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW);bool valid=fd>=0,missing=false;
        for(NSUInteger i=1;valid && i+1<parts.count;i++){
            int next=openat(fd,[parts[i] fileSystemRepresentation],O_RDONLY|O_DIRECTORY|O_NOFOLLOW);
            if(next<0){if(errno==ENOENT)missing=true;valid=false;break;}close(fd);fd=next;
        }
        if(missing)states[path]=@{@"type":@"missing"};
        else if(valid)valid=snapshotNode(fd,parts.lastObject,path,states,user,1,&entries,&bytes);
        if(fd>=0)close(fd);if(!valid && !missing)return nil;
    }return states;
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
        if(argc==2 && !strcmp(argv[1],"--product-ui-check-v1")) {
            if(geteuid()==0)return 78;
            return writeJSON(STDOUT_FILENO,@{@"schema_version":@"claude-mac-product-ui-check/v1",
                @"user_id":@(geteuid()),@"accessibility_trusted":@(AXIsProcessTrusted())}) ? 0 : 78;
        }
        if (argc == 2 && !strcmp(argv[1], "--product-ui-v1")) return driveProductUI();
        if (argc == 2 && !strcmp(argv[1], "--product-stop-v1")) return stopProduct();
        if (argc != 2 || strcmp(argv[1], "--product-events-broker-v1")) return 64;
        signal(SIGPIPE, SIG_IGN);
        NSDictionary *brokerPeer=readJSON(3);close(3);
        NSDictionary *request = readJSON(STDIN_FILENO);
        NSSet *fields = [NSSet setWithArray:@[@"schema_version", @"task_sha256", @"run_id", @"deployment", @"user_id", @"launcher_pid", @"product_log_root", @"product_user_data_root", @"product_module_path", @"application_executable", @"timeout_seconds", @"max_records", @"state_paths"]];
        if (![request isKindOfClass:[NSDictionary class]] || ![[NSSet setWithArray:request.allKeys] isEqual:fields]
            || ![request[@"schema_version"] isEqual:@"claude-mac-product-native-request/v1"]
            || ![request[@"application_executable"] isKindOfClass:[NSString class]]
            || ![request[@"application_executable"] hasPrefix:@"/"]
            || ![request[@"product_log_root"] isKindOfClass:[NSString class]] || ![request[@"product_log_root"] hasPrefix:@"/"]
            || ![request[@"product_user_data_root"] isKindOfClass:[NSString class]] || ![request[@"product_user_data_root"] hasPrefix:@"/"]
            || (request[@"product_module_path"] != NSNull.null && (![request[@"product_module_path"] isKindOfClass:NSString.class]
                || ![request[@"product_module_path"] hasPrefix:@"/"]))
            || ![request[@"launcher_pid"] isKindOfClass:[NSNumber class]] || [request[@"launcher_pid"] intValue] <= 0
            || ![request[@"user_id"] isKindOfClass:NSNumber.class] || [request[@"user_id"] unsignedIntValue]==0
            || ![request[@"task_sha256"] isKindOfClass:[NSString class]] || [request[@"task_sha256"] length] != 64
            || ![request[@"run_id"] isKindOfClass:[NSString class]] || [request[@"run_id"] length] == 0
            || ![request[@"deployment"] isKindOfClass:NSDictionary.class]
            || ![[NSSet setWithArray:[request[@"deployment"] allKeys]] isEqual:[NSSet setWithArray:@[@"deployment_id",@"tool_identity_sha256"]]]
            || [request[@"timeout_seconds"] doubleValue] <= 0 || [request[@"timeout_seconds"] doubleValue] > 7200
            || [request[@"max_records"] unsignedLongLongValue] == 0 || [request[@"max_records"] unsignedLongLongValue] > 65536
            || ![request[@"state_paths"] isKindOfClass:NSArray.class] || [request[@"state_paths"] count]==0 || [request[@"state_paths"] count]>64) return 64;
        NSArray *statePaths=request[@"state_paths"];
        for(id path in statePaths)if(![path isKindOfClass:NSString.class] || ![path hasPrefix:@"/"]
            || ![path isEqual:[path stringByStandardizingPath]] || [path length]>4096)return 64;
        if([[NSSet setWithArray:statePaths] count]!=statePaths.count)return 64;
        NSString *boot = bootUUID();
        if (!boot) { writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"boot_unavailable"}); return 78; }
        /* ES 需要真实 root 特权。这里只检测已经受管安装的运行条件，不提权，
         * 不调用 sudo、不设置任何安装位；输入中的产品用户还须核真实父进程。 */
        struct proc_bsdinfo launcher={0},bridge={0};audit_token_t peer={0};socklen_t peerSize=sizeof(peer);
        char parentPath[PROC_PIDPATHINFO_MAXSIZE]={0};
        NSArray *peerValues=brokerPeer[@"peer_audit_token"];
        bool validPeer=[brokerPeer isKindOfClass:NSDictionary.class]
            && [brokerPeer[@"schema_version"] isEqual:@"claude-mac-product-broker-peer/v1"]
            && [peerValues isKindOfClass:NSArray.class] && peerValues.count==8
            && getsockopt(STDIN_FILENO,SOL_LOCAL,LOCAL_PEERTOKEN,&peer,&peerSize)==0 && peerSize==sizeof(peer);
        if(validPeer)for(size_t i=0;i<8;i++)if(![peerValues[i] isEqual:@(peer.val[i])])validPeer=false;
        if(geteuid()!=0 || !validPeer || [brokerPeer[@"user_id"] unsignedIntValue]!=[request[@"user_id"] unsignedIntValue]
            || audit_token_to_euid(peer)!=[request[@"user_id"] unsignedIntValue]
            || ![brokerPeer[@"launcher_pid"] isEqual:request[@"launcher_pid"]]
            || proc_pidpath(getppid(),parentPath,sizeof(parentPath))<=0
            || strcmp(parentPath,"/Library/Application Support/sub2apiplus/claude-client-recorder/claude-product-broker")
            || proc_pidinfo(audit_token_to_pid(peer),PROC_PIDTBSDINFO,0,&bridge,sizeof(bridge))!=sizeof(bridge)
            || bridge.pbi_ppid!=[request[@"launcher_pid"] unsignedIntValue]
            || proc_pidinfo([request[@"launcher_pid"] intValue],PROC_PIDTBSDINFO,0,&launcher,sizeof(launcher))!=sizeof(launcher)
            || launcher.pbi_uid!=[request[@"user_id"] unsignedIntValue]
            || launcher.pbi_start_tvsec!=[brokerPeer[@"launcher_start_seconds"] unsignedLongLongValue]
            || launcher.pbi_start_tvusec!=[brokerPeer[@"launcher_start_microseconds"] unsignedLongLongValue]) {
            writeJSON(STDOUT_FILENO,@{@"kind":@"unavailable",@"reason":@"privileged_observer_unavailable"});return 78;
        }
        /* 使用安装时已提供的独立来源密钥；缺密钥只拒绝，不创建、不弹权限。 */
        SecKeyRef originKey=sourceKey();
        if(!originKey){writeJSON(STDOUT_FILENO,@{@"kind":@"unavailable",@"reason":@"origin_key_unavailable"});return 78;}
        NSDictionary *context=@{@"task_sha256":request[@"task_sha256"],@"run_id":request[@"run_id"],
            @"user_id":request[@"user_id"],@"deployment":request[@"deployment"]};
        CC_SHA256_Init(&observationHash);observationActive=true;
        NSLock *lock = [[NSLock alloc] init];
        NSMutableArray *queue = [NSMutableArray array]; NSMutableDictionary *members = [NSMutableDictionary dictionary];
        NSMutableDictionary *sequences = [NSMutableDictionary dictionary];
        __block NSNumber *globalSequence=nil;
        __block NSString *stateScopeSHA=nil;
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
                : message->event_type == ES_EVENT_TYPE_NOTIFY_CLOSE ? @"file_close"
                : message->event_type == ES_EVENT_TYPE_NOTIFY_OPEN ? @"file_open" : @"exit";
            NSNumber *sequenceKey=@(message->event_type), *old = sequences[sequenceKey];
            if (old && message->seq_num != old.unsignedLongLongValue + 1) firstError = @"event_sequence_gap";
            sequences[sequenceKey] = @(message->seq_num);
            if(globalSequence && message->global_seq_num!=globalSequence.unsignedLongLongValue+1)firstError=@"global_sequence_gap";
            globalSequence=@(message->global_seq_num);
            const es_process_t *target = message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC ? message->event.exec.target
                : message->event_type == ES_EVENT_TYPE_NOTIFY_FORK ? message->event.fork.child : message->process;
            NSDictionary *value = process(target, boot);
            NSDictionary *previous=message->event_type==ES_EVENT_TYPE_NOTIFY_EXEC ? process(message->process,boot) : nil;
            NSString *observed=eventTime(message);
            if (!value || !observed || (message->event_type==ES_EVENT_TYPE_NOTIFY_EXEC && !previous)) {
                firstError = @"process_identity_unknown"; [lock unlock]; return; }
            NSNumber *pid = value[@"pid"], *parent = value[@"ppid"];
            bool sameUser=target->audit_token.val[1]==[request[@"user_id"] unsignedIntValue];
            NSDictionary *change=stateEvent(message);
            if(!change){firstError=@"state_path_unknown";[lock unlock];return;}
            if(change.count){
                bool relevant=false,allScoped=true;
                for(NSString *path in change[@"paths"]){bool inside=scoped(path,statePaths);relevant|=inside;allScoped&=inside;}
                if(relevant){
                    bool ownedWriter=sameUser && members[pid] && [members[pid][@"audit_token"] isEqual:value[@"audit_token"]];
                    if(!stateScopeSHA || !ownedWriter || !allScoped || [change[@"requested"] isKindOfClass:NSDictionary.class]
                        && change[@"requested"][@"unapplied"])firstError=@"state_scope_or_writer_unknown";
                    if(++total>[request[@"max_records"] unsignedLongLongValue] || queue.count>=1024)firstError=@"record_limit";
                    else {[queue addObject:@{@"kind":@"product_state",@"event":change[@"event"],@"paths":change[@"paths"],
                        @"file":change[@"file"],@"requested":change[@"requested"],@"owned":@(ownedWriter),@"process":value,
                        @"observed_at_utc":observed,@"global_sequence":@(message->global_seq_num),@"event_sequence":@(message->seq_num)}];}
                }
            }
            if (message->event_type == ES_EVENT_TYPE_NOTIFY_CLOSE || message->event_type == ES_EVENT_TYPE_NOTIFY_OPEN) {
                bool opened=message->event_type == ES_EVENT_TYPE_NOTIFY_OPEN;
                const es_file_t *file = opened ? message->event.open.file : message->event.close.target;
                NSString *path = file && !file->path_truncated ? token(file->path) : nil;
                bool ownedWriter = sameUser && members[pid] && [members[pid][@"audit_token"] isEqual:value[@"audit_token"]];
                bool moduleRead=opened && path && [path isEqual:request[@"product_module_path"]];
                bool logClose=!opened && message->event.close.modified && path
                    && (([path hasPrefix:[request[@"product_log_root"] stringByAppendingString:@"/"]] && [path hasSuffix:@".jsonl"])
                        || ([path hasPrefix:[request[@"product_user_data_root"] stringByAppendingString:@"/logs/"]]
                            && [path hasSuffix:@".log"]));
                if (ownedWriter && (moduleRead || logClose)) {
                    NSString *contentHash=logClose ? fileSHA(file) : nil;
                    if(logClose && !contentHash){[lock unlock];return;}
                    if (++total > [request[@"max_records"] unsignedLongLongValue] || queue.count >= 1024) firstError = @"record_limit";
                    else {NSMutableDictionary *record=[@{@"kind": moduleRead ? @"product_module_open" : @"product_file", @"process": value, @"path": path,
                        @"device": @(file->stat.st_dev), @"inode": @(file->stat.st_ino), @"size": @(file->stat.st_size),
                        @"mtime_nanoseconds": @((uint64_t)file->stat.st_mtimespec.tv_sec*1000000000+file->stat.st_mtimespec.tv_nsec),
                        @"ctime_nanoseconds": @((uint64_t)file->stat.st_ctimespec.tv_sec*1000000000+file->stat.st_ctimespec.tv_nsec),
                        @"observed_at_utc": observed, @"global_sequence": @(message->global_seq_num), @"event_sequence": @(message->seq_num)} mutableCopy];
                        if(contentHash)record[@"content_sha256"]=contentHash;[queue addObject:record];}
                }
                [lock unlock]; return;
            }
            if(message->event_type!=ES_EVENT_TYPE_NOTIFY_EXEC && message->event_type!=ES_EVENT_TYPE_NOTIFY_FORK
                && message->event_type!=ES_EVENT_TYPE_NOTIFY_EXIT){[lock unlock];return;}
            bool root = sameUser && message->event_type == ES_EVENT_TYPE_NOTIFY_EXEC && [value[@"executable"] isEqual:request[@"application_executable"]]
                && audit_token_to_pid(target->parent_audit_token) == [request[@"launcher_pid"] intValue];
            bool owned = members[pid] && ([members[pid][@"audit_token"] isEqual:value[@"audit_token"]]
                || (previous && [members[pid][@"audit_token"] isEqual:previous[@"audit_token"]]));
            bool child = members[parent] && [members[parent][@"audit_token"] isEqual:value[@"parent_audit_token"]];
            if(root && !stateScopeSHA)firstError=@"product_launch_without_state_scope";
            if (root && rootSeen && !owned) firstError = @"second_application_instance";
            if ((root || owned || child) && !sameUser)firstError=@"owned_user_changed";
            if ((root || owned || child) && !firstError) {
                if (root) rootSeen = true;
                if (++total > [request[@"max_records"] unsignedLongLongValue] || queue.count >= 1024) firstError = @"record_limit";
                else {
                    NSMutableDictionary *event=[@{@"kind": @"process", @"event": kind, @"process": value,
                        @"observed_at_utc": observed, @"global_sequence": @(message->global_seq_num),
                        @"event_sequence": @(message->seq_num)} mutableCopy];
                    if(previous)event[@"previous_process"]=previous;[queue addObject:event];
                    if (message->event_type == ES_EVENT_TYPE_NOTIFY_EXIT) [members removeObjectForKey:pid];
                    else members[pid] = value;
                }
            }
            [lock unlock];
        });
        if (created != ES_NEW_CLIENT_RESULT_SUCCESS) {
            writeJSON(STDOUT_FILENO, @{@"kind": @"unavailable", @"reason": @"es_create_failed", @"native_result": @(created)});
            sealObservations(originKey,context,@"es_create_failed");CFRelease(originKey);return 78;
        }
        es_event_type_t types[] = {ES_EVENT_TYPE_NOTIFY_EXEC,ES_EVENT_TYPE_NOTIFY_FORK,ES_EVENT_TYPE_NOTIFY_EXIT,
            ES_EVENT_TYPE_NOTIFY_CLOSE,ES_EVENT_TYPE_NOTIFY_OPEN,ES_EVENT_TYPE_NOTIFY_CREATE,ES_EVENT_TYPE_NOTIFY_WRITE,
            ES_EVENT_TYPE_NOTIFY_MMAP,ES_EVENT_TYPE_NOTIFY_TRUNCATE,ES_EVENT_TYPE_NOTIFY_UNLINK,ES_EVENT_TYPE_NOTIFY_RENAME,
            ES_EVENT_TYPE_NOTIFY_SETMODE,ES_EVENT_TYPE_NOTIFY_UTIMES,ES_EVENT_TYPE_NOTIFY_LINK,ES_EVENT_TYPE_NOTIFY_SETOWNER,
            ES_EVENT_TYPE_NOTIFY_SETFLAGS,ES_EVENT_TYPE_NOTIFY_SETATTRLIST,ES_EVENT_TYPE_NOTIFY_SETACL,ES_EVENT_TYPE_NOTIFY_SETEXTATTR,
            ES_EVENT_TYPE_NOTIFY_DELETEEXTATTR,ES_EVENT_TYPE_NOTIFY_EXCHANGEDATA,ES_EVENT_TYPE_NOTIFY_CLONE,ES_EVENT_TYPE_NOTIFY_COPYFILE};
        es_return_t subscribed = es_subscribe(client, types, sizeof(types)/sizeof(types[0]));
        if (subscribed != ES_RETURN_SUCCESS) { es_delete_client(client);
            writeJSON(STDOUT_FILENO,@{@"kind":@"unavailable",@"reason":@"es_subscribe_failed"});
            sealObservations(originKey,context,@"es_subscribe_failed");CFRelease(originKey);return 78; }
        writeJSON(STDOUT_FILENO, @{@"kind": @"ready", @"task_sha256": request[@"task_sha256"],
            @"run_id": request[@"run_id"], @"boot_id": boot, @"observed_at_utc": now(),
            @"accessibility_trusted": @NO, @"socket_events_complete": @NO,
            @"module_activation_verified": @NO,@"state_paths":statePaths,
            @"state_observation_schema":@"claude-mac-product-state-observation/v1"});
        double started = monotonicTime(), deadline = started + [request[@"timeout_seconds"] doubleValue];
        if (started < 0) firstError = @"monotonic_clock_unavailable";
        bool closeRequested = false;
        while (!closeRequested && monotonicTime() >= 0 && monotonicTime() < deadline) {
            [lock lock]; NSArray *rows = [queue copy]; [queue removeAllObjects];
            NSString *error = firstError; [lock unlock];
            for (NSDictionary *row in rows) if (!writeJSON(STDOUT_FILENO, row)) { closeRequested = true; break; }
            if (error) { writeJSON(STDOUT_FILENO, @{@"kind": @"incomplete", @"reason": error}); break; }
            /* 完整网络来源由独立 NE flow 提供；不再用持续 libproc 轮询堆样本。 */
            struct pollfd control = {.fd = STDIN_FILENO, .events = POLLIN};
            int status = poll(&control, 1, 10);
            if (status < 0 && errno == EINTR) continue;
            if (status < 0 || (control.revents & (POLLHUP | POLLERR))) break;
            if (status > 0 && (control.revents & POLLIN)) {
                NSDictionary *command = readJSON(STDIN_FILENO);
                if([command isKindOfClass:NSDictionary.class] && [[NSSet setWithArray:command.allKeys]
                    isEqual:[NSSet setWithArray:@[@"operation",@"task_sha256",@"scope_sha256"]]]
                    && [command[@"operation"] isEqual:@"authorize_state"] && [command[@"task_sha256"] isEqual:request[@"task_sha256"]]
                    && [command[@"scope_sha256"] isKindOfClass:NSString.class] && [command[@"scope_sha256"] length]==64){
                    [lock lock];if(stateScopeSHA || rootSeen)firstError=@"state_scope_repeated";
                    else stateScopeSHA=[command[@"scope_sha256"] copy];[lock unlock];
                    if(firstError)break;
                    writeJSON(STDOUT_FILENO,@{@"kind":@"product_scope",@"scope_sha256":stateScopeSHA,@"observed_at_utc":now()});continue;
                }
                closeRequested = [command isEqual:@{@"operation": @"close", @"task_sha256": request[@"task_sha256"]}];
                if (!closeRequested) { firstError = @"control_invalid"; break; }
            }
        }
        /* 全部真实 owned exit 之后，在仍订阅时独立读取精确私有树。未知写者、
         * 不受支持对象、xattr/flags 或超限均留下错误，不以 after 反向授权。 */
        [lock lock];bool maySnapshot=closeRequested && !firstError && stateScopeSHA && members.count==0;[lock unlock];
        NSDictionary *stateSnapshot=maySnapshot ? snapshotState(statePaths,[request[@"user_id"] unsignedIntValue]) : nil;
        NSDictionary *stateAgain=stateSnapshot ? snapshotState(statePaths,[request[@"user_id"] unsignedIntValue]) : nil;
        if(maySnapshot && (!stateSnapshot || ![stateSnapshot isEqual:stateAgain])){
            [lock lock];if(!firstError)firstError=@"state_snapshot_unstable_or_unsupported";[lock unlock];}
        es_return_t unsubscribed = es_unsubscribe_all(client), deleted = es_delete_client(client);
        [lock lock]; NSArray *tail = [queue copy]; NSUInteger alive = members.count; NSString *error = firstError; [lock unlock];
        for (NSDictionary *row in tail) writeJSON(STDOUT_FILENO, row);
        if(stateSnapshot && !error)writeJSON(STDOUT_FILENO,@{@"kind":@"product_state_snapshot",@"states":stateSnapshot,@"observed_at_utc":now()});
        writeJSON(STDOUT_FILENO, @{@"kind": @"closed", @"task_sha256": request[@"task_sha256"],
            @"run_id": request[@"run_id"], @"observed_at_utc": now(), @"close_requested": @(closeRequested),
            @"unsubscribe_result": @(unsubscribed), @"delete_result": @(deleted), @"unclosed_instances": @(alive),
            @"first_error": error ?: (id)[NSNull null], @"drain_status": @"observed_unqualified",
            @"socket_events_complete": @NO, @"module_activation_verified": @NO});
        bool sealed=sealObservations(originKey,context,error);CFRelease(originKey);
        return closeRequested && !error && sealed && unsubscribed == ES_RETURN_SUCCESS && deleted == ES_RETURN_SUCCESS ? 0 : 78;
    }
}
