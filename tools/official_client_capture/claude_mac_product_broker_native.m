/* 固定 root ES 服务。安装与 launchd 启动属于现场动作；这里不 sudo、setuid
 * 或申请权限。受管 Unix socket 的实际 audit token 与代码 requirement 核对
 * 后，才派生原用户 launcher 的观察 worker。UI/停止仍由普通用户执行。 */
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <bsm/libbsm.h>
#include <launch.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <spawn.h>
#include <poll.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <stddef.h>
#include <time.h>

static const char *base="/Library/Application Support/sub2apiplus/claude-client-recorder";
static const char *socketPath="/Library/Application Support/sub2apiplus/claude-client-recorder/product-events.sock";
static bool writeAll(int fd,const void *bytes,size_t length) {
    const char *at=bytes;
    while(length){ssize_t n=write(fd,at,length);if(n<0&&errno==EINTR)continue;
        if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)){struct pollfd ready={.fd=fd,.events=POLLOUT};
            if(poll(&ready,1,10000)>0 && (ready.revents&POLLOUT))continue;return false;}
        if(n<=0)return false;at+=n;length-=n;}return true;
}
static int safeDescriptor(int fd) {
    if(fd<0)return -1;int copy=fcntl(fd,F_DUPFD_CLOEXEC,10);close(fd);return copy;
}
static bool workerCode(NSString *worker,NSString *text) {
    if(![text isKindOfClass:NSString.class] || !text.length || text.length>4096)return false;
    SecRequirementRef requirement=NULL;SecStaticCodeRef code=NULL;
    bool valid=SecRequirementCreateWithString((__bridge CFStringRef)text,kSecCSDefaultFlags,&requirement)==errSecSuccess
        && SecStaticCodeCreateWithPath((__bridge CFURLRef)[NSURL fileURLWithPath:worker],kSecCSDefaultFlags,&code)==errSecSuccess
        && SecStaticCodeCheckValidity(code,kSecCSStrictValidate,requirement)==errSecSuccess;
    if(code)CFRelease(code);if(requirement)CFRelease(requirement);return valid;
}
static NSData *rootFile(NSString *path) {
    int fd=open(path.fileSystemRepresentation,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    struct stat before={0},after={0},current={0};
    bool good=fd>=0 && fstat(fd,&before)==0 && S_ISREG(before.st_mode) && before.st_uid==0
        && before.st_nlink==1 && !(before.st_mode&022) && before.st_size>0 && before.st_size<=65536;
    NSMutableData *raw=[NSMutableData data];char bytes[4096];ssize_t n=0;
    while(good && (n=read(fd,bytes,sizeof(bytes)))>0){[raw appendBytes:bytes length:n];if(raw.length>65536)good=false;}
    good=good && n==0 && fstat(fd,&after)==0 && lstat(path.fileSystemRepresentation,&current)==0
        && before.st_dev==after.st_dev && before.st_ino==after.st_ino && before.st_size==after.st_size
        && before.st_mtimespec.tv_sec==after.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec==after.st_mtimespec.tv_nsec
        && before.st_ctimespec.tv_sec==after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==after.st_ctimespec.tv_nsec
        && current.st_dev==before.st_dev && current.st_ino==before.st_ino;
    if(fd>=0)close(fd);return good ? raw : nil;
}
static bool safeAncestors(void) {
    NSString *path=[NSString stringWithUTF8String:base];
    while(path.length){struct stat info={0};if(lstat(path.fileSystemRepresentation,&info)!=0
        || !S_ISDIR(info.st_mode) || info.st_uid!=0 || (info.st_mode&022))return false;
        if([path isEqual:@"/"])break;path=path.stringByDeletingLastPathComponent;}return true;
}
static bool peerCode(int fd,NSDictionary *install,audit_token_t *peer) {
    socklen_t size=sizeof(*peer);
    if(getsockopt(fd,SOL_LOCAL,LOCAL_PEERTOKEN,peer,&size)!=0 || size!=sizeof(*peer)
       || audit_token_to_euid(*peer)==0 || audit_token_to_pid(*peer)<=0)return false;
    NSString *text=install[@"client_requirement"];
    if(![text isKindOfClass:NSString.class] || !text.length || text.length>4096)return false;
    SecRequirementRef requirement=NULL;SecCodeRef code=NULL;
    NSDictionary *attrs=@{(__bridge id)kSecGuestAttributeAudit:[NSData dataWithBytes:peer length:sizeof(*peer)]};
    bool valid=SecRequirementCreateWithString((__bridge CFStringRef)text,kSecCSDefaultFlags,&requirement)==errSecSuccess
        && SecCodeCopyGuestWithAttributes(NULL,(__bridge CFDictionaryRef)attrs,kSecCSDefaultFlags,&code)==errSecSuccess
        && SecCodeCheckValidity(code,kSecCSStrictValidate,requirement)==errSecSuccess;
    if(code)CFRelease(code);if(requirement)CFRelease(requirement);return valid;
}
static int serve(void) {
    if(geteuid()!=0 || !safeAncestors())return 78;
    NSString *root=[NSString stringWithUTF8String:base];
    NSData *installationRaw=rootFile([root stringByAppendingPathComponent:@"broker-installation.json"]);
    if(!installationRaw)return 78;
    NSDictionary *install=[NSJSONSerialization JSONObjectWithData:installationRaw options:0 error:nil];
    if(![install isKindOfClass:NSDictionary.class] || ![install[@"schema_version"] isEqual:@"claude-mac-product-broker-installation/v1"]
        || ![install[@"socket_path"] isEqual:[NSString stringWithUTF8String:socketPath]])return 78;
    int *listeners=NULL;size_t count=0;
    if(launch_activate_socket("ProductEvents",&listeners,&count)!=0 || count!=1){free(listeners);return 78;}
    int listener=safeDescriptor(listeners[0]);free(listeners);if(listener<0)return 78;
    struct stat socketInfo={0};if(lstat(socketPath,&socketInfo)!=0 || !S_ISSOCK(socketInfo.st_mode)
        || socketInfo.st_uid!=0 || (socketInfo.st_mode&0777)!=0666)return 78;
    NSString *worker=[root stringByAppendingPathComponent:@"claude-product-events"];
    signal(SIGPIPE,SIG_IGN);
    for(;;){@autoreleasepool {
        int connection=accept(listener,NULL,NULL);if(connection<0){if(errno==EINTR)continue;return 78;}
        connection=safeDescriptor(connection);if(connection<0)return 78;
        audit_token_t peer={0};struct proc_bsdinfo child={0},launcher={0};
        bool good=[installationRaw isEqual:rootFile([root stringByAppendingPathComponent:@"broker-installation.json"])]
            && workerCode(worker,install[@"worker_requirement"]) && peerCode(connection,install,&peer)
            && proc_pidinfo(audit_token_to_pid(peer),PROC_PIDTBSDINFO,0,&child,sizeof(child))==sizeof(child)
            && child.pbi_uid==audit_token_to_euid(peer) && child.pbi_ppid>0
            && proc_pidinfo(child.pbi_ppid,PROC_PIDTBSDINFO,0,&launcher,sizeof(launcher))==sizeof(launcher)
            && launcher.pbi_uid==audit_token_to_euid(peer);
        if(!good){close(connection);continue;}
        NSMutableArray *audit=[NSMutableArray array];for(size_t i=0;i<8;i++)[audit addObject:@(peer.val[i])];
        NSDictionary *identity=@{@"schema_version":@"claude-mac-product-broker-peer/v1",@"user_id":@(audit_token_to_euid(peer)),
            @"peer_audit_token":audit,@"launcher_pid":@(child.pbi_ppid),
            @"launcher_start_seconds":@(launcher.pbi_start_tvsec),@"launcher_start_microseconds":@(launcher.pbi_start_tvusec)};
        NSMutableData *metadata=[[NSJSONSerialization dataWithJSONObject:identity options:NSJSONWritingSortedKeys error:nil] mutableCopy];
        [metadata appendBytes:"\n" length:1];int control[2];
        if(pipe(control)!=0){close(connection);continue;}
        control[0]=safeDescriptor(control[0]);control[1]=safeDescriptor(control[1]);
        int nullfd=safeDescriptor(open("/dev/null",O_WRONLY));
        if(control[0]<0 || control[1]<0 || nullfd<0){if(control[0]>=0)close(control[0]);if(control[1]>=0)close(control[1]);
            if(nullfd>=0)close(nullfd);close(connection);continue;}
        posix_spawn_file_actions_t actions;posix_spawn_file_actions_init(&actions);
        posix_spawn_file_actions_adddup2(&actions,connection,STDIN_FILENO);
        posix_spawn_file_actions_adddup2(&actions,connection,STDOUT_FILENO);
        posix_spawn_file_actions_adddup2(&actions,nullfd,STDERR_FILENO);
        posix_spawn_file_actions_adddup2(&actions,control[0],3);
        if(connection>3)posix_spawn_file_actions_addclose(&actions,connection);
        if(control[0]>3)posix_spawn_file_actions_addclose(&actions,control[0]);
        if(control[1]>3)posix_spawn_file_actions_addclose(&actions,control[1]);
        if(listener!=3)posix_spawn_file_actions_addclose(&actions,listener);
        char *argv[]={(char *)worker.fileSystemRepresentation,"--product-events-broker-v1",NULL};char *env[]={NULL};pid_t pid=0;
        struct timeval receiveLimit={.tv_sec=10};setsockopt(connection,SOL_SOCKET,SO_RCVTIMEO,&receiveLimit,sizeof(receiveLimit));
        setsockopt(connection,SOL_SOCKET,SO_SNDTIMEO,&receiveLimit,sizeof(receiveLimit));
        posix_spawnattr_t attributes;posix_spawnattr_init(&attributes);
        posix_spawnattr_setflags(&attributes,POSIX_SPAWN_CLOEXEC_DEFAULT);
        int result=posix_spawn(&pid,worker.fileSystemRepresentation,&actions,&attributes,argv,env);
        posix_spawnattr_destroy(&attributes);
        posix_spawn_file_actions_destroy(&actions);close(control[0]);close(connection);if(nullfd>=0)close(nullfd);
        if(result==0)writeAll(control[1],metadata.bytes,metadata.length);close(control[1]);
        if(result==0){int status=0;while(waitpid(pid,&status,0)<0&&errno==EINTR){}}
    }}
}
static int connectClient(void) {
    if(geteuid()==0 || !safeAncestors())return 78;
    struct stat before={0};if(lstat(socketPath,&before)!=0 || !S_ISSOCK(before.st_mode)
        || before.st_uid!=0 || (before.st_mode&0777)!=0666)return 78;
    int fd=socket(AF_UNIX,SOCK_STREAM,0);struct sockaddr_un address={0};address.sun_family=AF_UNIX;
    if(fd<0 || strlen(socketPath)>=sizeof(address.sun_path))return 78;
    strcpy(address.sun_path,socketPath);address.sun_len=(uint8_t)(offsetof(struct sockaddr_un,sun_path)+strlen(socketPath)+1);
    if(connect(fd,(struct sockaddr *)&address,address.sun_len)!=0){close(fd);return 78;}
    uid_t uid=0;gid_t gid=0;struct stat after={0};
    if(getpeereid(fd,&uid,&gid)!=0 || uid!=0 || lstat(socketPath,&after)!=0
        || before.st_dev!=after.st_dev || before.st_ino!=after.st_ino){close(fd);return 78;}
    struct timeval sendLimit={.tv_sec=10};setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&sendLimit,sizeof(sendLimit));
    fcntl(STDOUT_FILENO,F_SETFL,fcntl(STDOUT_FILENO,F_GETFL)|O_NONBLOCK);
    signal(SIGPIPE,SIG_IGN);bool input=true;char bytes[65536];
    while(true){struct pollfd descriptors[2]={{.fd=fd,.events=POLLIN},{.fd=input?STDIN_FILENO:-1,.events=POLLIN}};
        int n=poll(descriptors,2,1000);if(n<0&&errno==EINTR)continue;if(n<0){close(fd);return 78;}
        if(descriptors[0].revents&(POLLIN|POLLHUP)){ssize_t amount=read(fd,bytes,sizeof(bytes));
            if(amount==0){close(fd);return 0;}if(amount<0||!writeAll(STDOUT_FILENO,bytes,amount)){close(fd);return 78;}}
        if(descriptors[0].revents&(POLLERR|POLLNVAL)){close(fd);return 78;}
        if(input && (descriptors[1].revents&(POLLIN|POLLHUP))){ssize_t amount=read(STDIN_FILENO,bytes,sizeof(bytes));
            if(amount==0){shutdown(fd,SHUT_WR);input=false;}else if(amount<0||!writeAll(fd,bytes,amount)){close(fd);return 78;}}
    }
}
int main(int argc,const char *argv[]) {@autoreleasepool {
    if(argc==2 && !strcmp(argv[1],"--product-broker-v1"))return serve();
    if(argc==2 && !strcmp(argv[1],"--product-root-client-v1"))return connectClient();return 64;
}}
