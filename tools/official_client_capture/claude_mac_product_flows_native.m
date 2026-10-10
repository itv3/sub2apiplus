/* Mac 网络原件来源。只记录 NE 原生端点、进程 audit token 和 FlowClosed；
 * 不解密 TLS、不猜 HTTP ordinal、不改客户端请求。服务端与调用方以公开 XPC
 * peer code-signing requirement 双向验身份。私有构建不安装、不激活、不建密钥。
 */
#import <Foundation/Foundation.h>
#import <NetworkExtension/NetworkExtension.h>
#import <Network/Network.h>
#import <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>
#include <xpc/xpc.h>
#include <bsm/libbsm.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>

static NSString *const protocolName = @"claude-mac-ne-flow-rpc/v1";
static NSString *const keyTag = @"com.sub2apiplus.claude.network-observer.origin-v1";
static const NSUInteger recordLimit = 65536;
static void *providerQueueKey = &providerQueueKey;
static NSData *encode(id value) {
    return [NSJSONSerialization dataWithJSONObject:value
        options:NSJSONWritingSortedKeys | NSJSONWritingWithoutEscapingSlashes error:nil];
}
static NSString *utcNow(void) {
    NSISO8601DateFormatter *f = [NSISO8601DateFormatter new];
    f.formatOptions = NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
    return [f stringFromDate:NSDate.date];
}
static NSString *sha(NSData *raw) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH]; CC_SHA256(raw.bytes, (CC_LONG)raw.length, digest);
    NSMutableString *value = [NSMutableString string];
    for (size_t i=0; i<sizeof(digest); i++) [value appendFormat:@"%02x", digest[i]];
    return value;
}
static NSArray *auditWords(NSData *data) {
    if (!data || data.length != sizeof(audit_token_t)) return nil;
    audit_token_t token; memcpy(&token, data.bytes, sizeof(token));
    if (audit_token_to_pid(token) <= 0) return nil;
    NSMutableArray *words = [NSMutableArray array];
    for (size_t i=0; i<8; i++) [words addObject:@(token.val[i])];
    return words;
}
static id endpoint(nw_endpoint_t value) {
    if (!value || nw_endpoint_get_type(value) != nw_endpoint_type_address) return NSNull.null;
    const char *host = nw_endpoint_get_hostname(value);
    uint16_t port = nw_endpoint_get_port(value);
    return host && port ? @{@"host": @(host), @"port": @(port)} : (id)NSNull.null;
}
static bool exact(NSDictionary *value, NSArray *names) {
    return [value isKindOfClass:NSDictionary.class]
        && [[NSSet setWithArray:value.allKeys] isEqual:[NSSet setWithArray:names]];
}
static NSDictionary *failure(NSString *reason) { return @{@"ok": @NO, @"error": reason}; }

@interface ClaudeFlowDataProvider : NEFilterDataProvider
@property(nonatomic) dispatch_queue_t queue;
@property(nonatomic) xpc_connection_t listener;
@property(nonatomic) xpc_connection_t owner;
@property(nonatomic) NSDictionary *context;
@property(nonatomic) NSMutableArray<NSData *> *records;
@property(nonatomic) NSMutableDictionary<NSString *, NSDictionary *> *flows;
@property(nonatomic) NSMutableDictionary<NSString *, NEFilterSocketFlow *> *flowObjects;
@property(nonatomic) NSMutableSet<NSString *> *closedFlows;
@property(nonatomic) NSMutableSet<NSArray *> *quiescedTokens;
@property(nonatomic) NSString *firstError;
@property(nonatomic) NSDictionary *seal;
@property(nonatomic) dispatch_source_t expiry;
@property(nonatomic) SecKeyRef originKey;
@end

@implementation ClaudeFlowDataProvider
- (void)dealloc { if (_originKey) CFRelease(_originKey); }
- (void)sync:(dispatch_block_t)operation {
    /* updateFlow 可引发系统回调；若回调已在本串行队列，不再同步等待自己。 */
    if(dispatch_get_specific(providerQueueKey)==(__bridge void *)self)operation();
    else dispatch_sync(self.queue,operation);
}
- (void)append:(NSString *)event facts:(NSDictionary *)facts {
    if (self.seal) return;
    if (self.records.count >= recordLimit) { self.firstError = @"record_limit"; return; }
    NSDictionary *row = @{@"schema_version": @"claude-mac-ne-flow-event/v1", @"event": event,
        @"sequence": @(self.records.count), @"task_sha256": self.context[@"task_sha256"],
        @"run_id": self.context[@"run_id"], @"observed_at_utc": utcNow(), @"facts": facts};
    NSMutableData *raw = [encode(row) mutableCopy];
    if (!raw || raw.length > 65536) { self.firstError = @"record_serialization"; return; }
    [raw appendBytes:"\n" length:1]; [self.records addObject:raw];
}
- (NSDictionary *)freeze:(NSString *)reason {
    if (self.seal) return self.seal;
    if (reason && !self.firstError) self.firstError = reason;
    [self append:@"session_end" facts:@{@"first_error": self.firstError ?: (id)NSNull.null,
        @"open_flow_ids": [[self.flows.allKeys filteredArrayUsingPredicate:
            [NSPredicate predicateWithBlock:^BOOL(NSString *identifier, NSDictionary *bindings) {
                (void)bindings; return ![self.closedFlows containsObject:identifier]; }]] sortedArrayUsingSelector:@selector(compare:)]}];
    NSMutableData *stream = [NSMutableData data]; for (NSData *row in self.records) [stream appendData:row];
    NSData *message = encode(@{@"schema_version": @"claude-mac-ne-flow-attestation/v1",
        @"context": self.context, @"record_count": @(self.records.count), @"raw_bytes": @(stream.length),
        @"stream_sha256": sha(stream), @"first_error": self.firstError ?: (id)NSNull.null});
    CFErrorRef error = NULL;
    CFDataRef signedData = self.originKey ? SecKeyCreateSignature(self.originKey,
        kSecKeyAlgorithmECDSASignatureMessageX962SHA256, (__bridge CFDataRef)message, &error) : NULL;
    SecKeyRef publicKey = self.originKey ? SecKeyCopyPublicKey(self.originKey) : NULL;
    CFDataRef publicBytes = publicKey ? SecKeyCopyExternalRepresentation(publicKey, &error) : NULL;
    if (!signedData || !publicBytes || error) {
        self.seal = failure(@"origin_signature_unavailable");
    } else self.seal = @{@"ok": @YES, @"attestation": @{
        @"payload_base64": [message base64EncodedStringWithOptions:0],
        @"signature_base64": [(__bridge NSData *)signedData base64EncodedStringWithOptions:0],
        @"public_key_sha256": sha((__bridge NSData *)publicBytes)}};
    if (signedData) CFRelease(signedData); if (publicBytes) CFRelease(publicBytes);
    if (publicKey) CFRelease(publicKey); if (error) CFRelease(error);
    if (self.expiry) { dispatch_source_cancel(self.expiry); self.expiry=nil; }
    return self.seal;
}
- (NSDictionary *)operation:(NSDictionary *)request peer:(xpc_connection_t)peer {
    if (!exact(request, [request[@"operation"] isEqual:@"quiesce"]
        ? @[@"protocol",@"operation",@"context",@"offset",@"audit_tokens"]
        : @[@"protocol", @"operation", @"context", @"offset"])) return failure(@"request_fields");
    if (![request[@"protocol"] isEqual:protocolName]) return failure(@"protocol");
    NSDictionary *context = request[@"context"];
    if (!exact(context,@[@"task_sha256", @"run_id", @"user_id", @"deadline_utc", @"deployment"])) return failure(@"context_fields");
    NSString *operation=request[@"operation"];
    if(![operation isKindOfClass:NSString.class] || ![request[@"offset"] isKindOfClass:NSNumber.class]
        || [request[@"offset"] longLongValue]<0 || [request[@"offset"] unsignedLongLongValue]>recordLimit)return failure(@"operation_fields");
    if ([operation isEqual:@"begin"]) {
        if (self.context) return failure(@"session_already_exists");
        if (![context[@"task_sha256"] isKindOfClass:NSString.class] || [context[@"task_sha256"] length]!=64
            || ![context[@"run_id"] isKindOfClass:NSString.class] || [context[@"run_id"] length]==0
            || [context[@"run_id"] length]>128 || ![context[@"deadline_utc"] isKindOfClass:NSString.class]
            || ![context[@"user_id"] isKindOfClass:NSNumber.class] || [context[@"user_id"] unsignedIntValue]==0
            || [context[@"user_id"] unsignedIntValue]!=xpc_connection_get_euid(peer)
            || !exact(context[@"deployment"],@[@"deployment_id",@"tool_identity_sha256"])) return failure(@"context_value");
        NSISO8601DateFormatter *formatter=[NSISO8601DateFormatter new];
        formatter.formatOptions=NSISO8601DateFormatWithInternetDateTime | NSISO8601DateFormatWithFractionalSeconds;
        NSDate *end=[formatter dateFromString:context[@"deadline_utc"]];
        NSTimeInterval remaining=[end timeIntervalSinceNow];
        if (!end || remaining<=0 || remaining>7200) return failure(@"deadline");
        NSDictionary *query=@{(__bridge id)kSecClass:(__bridge id)kSecClassKey,
            (__bridge id)kSecAttrApplicationTag:[keyTag dataUsingEncoding:NSUTF8StringEncoding],
            (__bridge id)kSecAttrKeyType:(__bridge id)kSecAttrKeyTypeECSECPrimeRandom,
            (__bridge id)kSecReturnRef:@YES, (__bridge id)kSecUseAuthenticationUI:(__bridge id)kSecUseAuthenticationUIFail};
        CFTypeRef key=NULL; OSStatus found=SecItemCopyMatching((__bridge CFDictionaryRef)query,&key);
        if (found!=errSecSuccess || !key || CFGetTypeID(key)!=SecKeyGetTypeID()) {
            if(key)CFRelease(key); return failure(@"origin_key_unavailable"); }
        self.originKey=(SecKeyRef)key; self.owner=peer; self.context=context;
        self.records=[NSMutableArray array]; self.flows=[NSMutableDictionary dictionary];self.flowObjects=[NSMutableDictionary dictionary];
        self.closedFlows=[NSMutableSet set];self.quiescedTokens=[NSMutableSet set];
        [self append:@"session_begin" facts:@{@"provider_class":@"NEFilterDataProvider",
            @"source_token_kind":@"sourceProcessAuditToken",@"close_report_kind":@"NEFilterReportEventFlowClosed"}];
        self.expiry=dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER,0,0,self.queue);
        dispatch_source_set_timer(self.expiry,dispatch_time(DISPATCH_TIME_NOW,(int64_t)(remaining*NSEC_PER_SEC)),DISPATCH_TIME_FOREVER,0);
        __weak ClaudeFlowDataProvider *weak=self;
        dispatch_source_set_event_handler(self.expiry,^{ [weak freeze:@"deadline_expired"]; }); dispatch_resume(self.expiry);
        return @{@"ok":@YES,@"record_count":@(self.records.count)};
    }
    /* 原连接中断后，同一受管签名 peer 仍可取回原 task 的已冻结记录；不会 begin
     * 第二个会话或换 context。权限由 XPC runtime 的逐消息签名校验提供。 */
    if (!self.context || ![self.context isEqual:context]
        || [context[@"user_id"] unsignedIntValue]!=xpc_connection_get_euid(peer)) return failure(@"session_owner");
    if ([operation isEqual:@"close"]) return [self freeze:nil];
    if ([operation isEqual:@"quiesce"]) {
        if(self.seal || ![request[@"audit_tokens"] isKindOfClass:NSArray.class]
            || [request[@"audit_tokens"] count]>8192)return failure(@"quiesce_fields");
        for(NSArray *words in request[@"audit_tokens"]) {
            if(![words isKindOfClass:NSArray.class] || words.count!=8)return failure(@"quiesce_token");
            audit_token_t token={0};
            for(NSUInteger i=0;i<8;i++){
                if(![words[i] isKindOfClass:NSNumber.class] || [words[i] unsignedLongLongValue]>UINT32_MAX)return failure(@"quiesce_token");
                token.val[i]=[words[i] unsignedIntValue];
            }
            if(audit_token_to_euid(token)!=[self.context[@"user_id"] unsignedIntValue]
                || audit_token_to_pid(token)<=0 || audit_token_to_pidversion(token)==0)return failure(@"quiesce_user");
        }
        [self.quiescedTokens addObjectsFromArray:request[@"audit_tokens"]];
        NSMutableArray *pending=[NSMutableArray array];
        for(NSString *identifier in self.flows.allKeys){
            if([self.closedFlows containsObject:identifier]
                || ![self.quiescedTokens containsObject:self.flows[identifier][@"source_process_audit_token"]])continue;
            [pending addObject:identifier];
            NEFilterDataVerdict *drop=NEFilterDataVerdict.dropVerdict;drop.shouldReport=YES;
            [self updateFlow:self.flowObjects[identifier] usingVerdict:drop forDirection:NETrafficDirectionAny];
        }
        return @{@"ok":@YES,@"pending_flow_ids":[pending sortedArrayUsingSelector:@selector(compare:)]};
    }
    if ([operation isEqual:@"release"]) {
        if(!self.seal || ![self.seal[@"ok"] boolValue])return failure(@"unsealed_session");
        self.context=nil;self.owner=nil;self.records=nil;self.flows=nil;self.flowObjects=nil;self.closedFlows=nil;
        self.quiescedTokens=nil;self.seal=nil;self.firstError=nil;
        if(self.originKey){CFRelease(self.originKey);self.originKey=NULL;}return @{@"ok":@YES};
    }
    if (![operation isEqual:@"read"] || ![request[@"offset"] isKindOfClass:NSNumber.class]) return failure(@"operation");
    NSUInteger offset=[request[@"offset"] unsignedIntegerValue];
    if(offset>self.records.count) return failure(@"offset");
    NSMutableArray *rows=[NSMutableArray array]; NSUInteger bytes=0;
    for(NSUInteger i=offset;i<self.records.count && rows.count<128;i++) {
        NSData *raw=self.records[i]; if(bytes+raw.length>262144)break;
        [rows addObject:[raw base64EncodedStringWithOptions:0]];bytes+=raw.length;
    }
    return @{@"ok":@YES,@"offset":@(offset),@"records":rows,@"record_count":@(self.records.count),@"sealed":@(self.seal!=nil)};
}
- (void)startFilterWithCompletionHandler:(void (^)(NSError *))completionHandler {
    self.queue=dispatch_queue_create("com.sub2apiplus.claude.flow-source",DISPATCH_QUEUE_SERIAL);
    dispatch_queue_set_specific(self.queue,providerQueueKey,(__bridge void *)self,NULL);
    NSString *name=NSBundle.mainBundle.infoDictionary[@"NetworkExtension"][@"NEMachServiceName"];
    NSString *requirement=NSBundle.mainBundle.infoDictionary[@"RecorderClientRequirement"];
    if(![name isKindOfClass:NSString.class] || name.length==0 || ![requirement isKindOfClass:NSString.class] || requirement.length==0) {
        completionHandler([NSError errorWithDomain:@"ClaudeFlow" code:78 userInfo:nil]); return; }
    self.listener=xpc_connection_create_mach_service(name.UTF8String,self.queue,XPC_CONNECTION_MACH_SERVICE_LISTENER);
    if(!self.listener || xpc_connection_set_peer_code_signing_requirement(self.listener,requirement.UTF8String)!=0) {
        completionHandler([NSError errorWithDomain:@"ClaudeFlow" code:78 userInfo:nil]); return; }
    __weak ClaudeFlowDataProvider *weak=self;
    xpc_connection_set_event_handler(self.listener,^(xpc_object_t incoming) {
        if(xpc_get_type(incoming)!=XPC_TYPE_CONNECTION)return;
        xpc_connection_t peer=(xpc_connection_t)incoming;
        if(xpc_connection_set_peer_code_signing_requirement(peer,requirement.UTF8String)!=0) { xpc_connection_cancel(peer);return; }
        xpc_connection_set_target_queue(peer,weak.queue);
        xpc_connection_set_event_handler(peer,^(xpc_object_t message) {
            if(xpc_get_type(message)==XPC_TYPE_ERROR) {
                if(weak.owner==peer && weak.context && !weak.seal)[weak freeze:@"peer_disconnected"]; return; }
            if(xpc_get_type(message)!=XPC_TYPE_DICTIONARY)return;
            size_t length=0;const void *bytes=xpc_dictionary_get_data(message,"json",&length);
            NSDictionary *request=bytes && length<=65536 ? [NSJSONSerialization JSONObjectWithData:
                [NSData dataWithBytes:bytes length:length] options:0 error:nil] : nil;
            NSDictionary *result=[weak operation:request peer:peer]; NSData *raw=encode(result);
            xpc_object_t reply=xpc_dictionary_create_reply(message); if(!reply)return;
            xpc_dictionary_set_data(reply,"json",raw.bytes,raw.length); xpc_connection_send_message(peer,reply);
        }); xpc_connection_resume(peer);
    });xpc_connection_resume(self.listener);completionHandler(nil);
}
- (void)stopFilterWithReason:(NEProviderStopReason)reason completionHandler:(void (^)(void))completionHandler {
    (void)reason; dispatch_async(self.queue,^{if(self.context && !self.seal)[self freeze:@"provider_stopped"];
        if(self.listener)xpc_connection_cancel(self.listener);completionHandler();});
}
- (NSDictionary *)flowFacts:(NEFilterSocketFlow *)flow {
    return @{@"flow_id":flow.identifier.UUIDString.lowercaseString,
        @"source_process_audit_token":auditWords(flow.sourceProcessAuditToken) ?: (id)NSNull.null,
        @"source_app_audit_token":auditWords(flow.sourceAppAuditToken) ?: (id)NSNull.null,
        @"local_endpoint":endpoint(flow.localFlowEndpoint),@"remote_endpoint":endpoint(flow.remoteFlowEndpoint),
        @"socket_family":@(flow.socketFamily),@"socket_type":@(flow.socketType),@"socket_protocol":@(flow.socketProtocol)};
}
- (NEFilterNewFlowVerdict *)handleNewFlow:(NEFilterFlow *)flow {
    __block BOOL tracked=NO,quiesced=NO;
    if([flow isKindOfClass:NEFilterSocketFlow.class] && flow.direction==NETrafficDirectionOutbound) {
        NEFilterSocketFlow *socket=(NEFilterSocketFlow *)flow;
        [self sync:^{
            if(!self.context || self.seal)return;
            NSData *data=socket.sourceProcessAuditToken ?: socket.sourceAppAuditToken;
            audit_token_t token={0}; if(!data || data.length!=sizeof(token))return;memcpy(&token,data.bytes,sizeof(token));
            if(audit_token_to_euid(token)!=[self.context[@"user_id"] unsignedIntValue] || socket.socketProtocol!=IPPROTO_TCP)return;
            NSDictionary *facts=[self flowFacts:socket];NSString *identifier=facts[@"flow_id"];
            if(self.flows[identifier]){self.firstError=@"duplicate_flow";return;}
            if(self.flows.count>=8192){self.firstError=@"flow_limit";return;}
            self.flows[identifier]=facts;self.flowObjects[identifier]=socket;[self append:@"flow_open" facts:facts];tracked=YES;
            quiesced=[self.quiescedTokens containsObject:facts[@"source_process_audit_token"]];
        }];
    }
    NEFilterNewFlowVerdict *verdict=quiesced ? NEFilterNewFlowVerdict.dropVerdict : tracked ? [NEFilterNewFlowVerdict filterDataVerdictWithFilterInbound:YES
        peekInboundBytes:1 filterOutbound:YES peekOutboundBytes:1] : [NEFilterNewFlowVerdict allowVerdict];
    verdict.shouldReport=tracked;return verdict;
}
- (NEFilterDataVerdict *)observeData:(NEFilterFlow *)flow {
    __block BOOL tracked=NO,quiesced=NO;
    [self sync:^{
        NSString *identifier=flow.identifier.UUIDString.lowercaseString;
        if(!self.context || self.seal || !self.flows[identifier] || ![flow isKindOfClass:NEFilterSocketFlow.class])return;
        NSDictionary *facts=[self flowFacts:(NEFilterSocketFlow *)flow];
        if(![facts isEqual:self.flows[identifier]]){self.flows[identifier]=facts;[self append:@"flow_endpoints" facts:facts];}
        tracked=YES;quiesced=[self.quiescedTokens containsObject:facts[@"source_process_audit_token"]];
    }];
    NEFilterDataVerdict *verdict=quiesced ? NEFilterDataVerdict.dropVerdict : NEFilterDataVerdict.allowVerdict;
    verdict.shouldReport=tracked;return verdict;
}
- (NEFilterDataVerdict *)handleInboundDataFromFlow:(NEFilterFlow *)flow readBytesStartOffset:(NSUInteger)offset readBytes:(NSData *)readBytes {
    (void)offset;(void)readBytes;return [self observeData:flow];
}
- (NEFilterDataVerdict *)handleOutboundDataFromFlow:(NEFilterFlow *)flow readBytesStartOffset:(NSUInteger)offset readBytes:(NSData *)readBytes {
    (void)offset;(void)readBytes;return [self observeData:flow];
}
- (NEFilterDataVerdict *)handleInboundDataCompleteForFlow:(NEFilterFlow *)flow {return [self observeData:flow];}
- (NEFilterDataVerdict *)handleOutboundDataCompleteForFlow:(NEFilterFlow *)flow {return [self observeData:flow];}
- (void)handleReport:(NEFilterReport *)report {
    if(report.event!=NEFilterReportEventFlowClosed)return;
    [self sync:^{
        NEFilterFlow *flow=report.flow;NSString *identifier=flow.identifier.UUIDString.lowercaseString;
        if(!self.context || self.seal || !self.flows[identifier])return;
        if(![flow isKindOfClass:NEFilterSocketFlow.class] || [self.closedFlows containsObject:identifier]) {
            self.firstError=@"close_report_identity";return;}
        NSMutableDictionary *facts=[[self flowFacts:(NEFilterSocketFlow *)flow] mutableCopy];
        facts[@"bytes_in"]=@(report.bytesInboundCount);facts[@"bytes_out"]=@(report.bytesOutboundCount);
        facts[@"report_event"]=@(report.event);[self append:@"flow_closed" facts:facts];[self.closedFlows addObject:identifier];
    }];
}
@end

#if defined(CLAUDE_FLOW_CLIENT)
/* 固定安装下的客户端只做认证读取，不安装或激活扩展。先验 root 原件，再用
 * public XPC API 在收到每个响应时核系统对端签名，禁止仅按 PID 检查身份。 */
static NSDictionary *installation(void) {
    NSString *path=@"/Library/Application Support/sub2apiplus/claude-client-recorder/network-installation.json";
    int fd=open(path.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    struct stat before={0},after={0},current={0};NSMutableData *raw=[NSMutableData data];
    if(fd<0 || fstat(fd,&before)!=0 || !S_ISREG(before.st_mode) || before.st_uid!=0
        || (before.st_mode & 022)!=0 || before.st_nlink!=1 || before.st_size>65536){if(fd>=0)close(fd);return nil;}
    char buffer[4096];ssize_t count;
    while((count=read(fd,buffer,sizeof(buffer)))>0){[raw appendBytes:buffer length:count];if(raw.length>65536)break;}
    bool stable=count==0 && fstat(fd,&after)==0 && lstat(path.fileSystemRepresentation,&current)==0
        && before.st_dev==after.st_dev && after.st_dev==current.st_dev && before.st_ino==after.st_ino
        && after.st_ino==current.st_ino && before.st_size==after.st_size && after.st_size==current.st_size
        && before.st_mtimespec.tv_sec==after.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec==after.st_mtimespec.tv_nsec
        && before.st_ctimespec.tv_sec==after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==after.st_ctimespec.tv_nsec;
    close(fd);return stable ? [NSJSONSerialization JSONObjectWithData:raw options:0 error:nil] : nil;
}
int main(int argc,const char *argv[]) {
    @autoreleasepool {
        if(argc!=2 || strcmp(argv[1],"--flow-session-v1"))return 64;
        NSDictionary *config=installation();
        if(!config || ![config[@"schema_version"] isEqual:@"claude-mac-network-installation/v1"]
            || ![config[@"mach_service"] isKindOfClass:NSString.class]
            || ![config[@"provider_requirement"] isKindOfClass:NSString.class]
            || [config[@"provider_requirement"] length]==0)return 78;
        dispatch_queue_t queue=dispatch_queue_create("com.sub2apiplus.claude.flow-client",DISPATCH_QUEUE_SERIAL);
        xpc_connection_t peer=xpc_connection_create_mach_service([config[@"mach_service"] UTF8String],queue,0);
        if(!peer || xpc_connection_set_peer_code_signing_requirement(peer,[config[@"provider_requirement"] UTF8String])!=0)return 78;
        __block BOOL disconnected=NO;xpc_connection_set_event_handler(peer,^(xpc_object_t message){
            if(xpc_get_type(message)==XPC_TYPE_ERROR)disconnected=YES;});xpc_connection_resume(peer);
        char *line=NULL;size_t allocated=0;ssize_t bytes;
        while((bytes=getline(&line,&allocated,stdin))>0) {
            if(bytes>65536 || line[bytes-1]!='\n' || disconnected){free(line);return 78;}
            xpc_object_t request=xpc_dictionary_create(NULL,NULL,0);xpc_dictionary_set_data(request,"json",line,bytes-1);
            xpc_object_t reply=xpc_connection_send_message_with_reply_sync(peer,request);
            size_t length=0;const void *raw=xpc_get_type(reply)==XPC_TYPE_DICTIONARY ? xpc_dictionary_get_data(reply,"json",&length) : NULL;
            if(!raw || length==0 || length>524288){free(line);return 78;}
            if(fwrite(raw,1,length,stdout)!=length || fputc('\n',stdout)==EOF || fflush(stdout)!=0){free(line);return 78;}
        }
        free(line);xpc_connection_cancel(peer);return bytes<0 && ferror(stdin) ? 78 : 0;
    }
}
#else
int main(void) { @autoreleasepool { [NEProvider startSystemExtensionMode];dispatch_main(); } }
#endif
