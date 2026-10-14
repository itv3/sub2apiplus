#!/usr/bin/env python3
"""声明级承接的读集采样器（``declared-inputs/v1`` 的证据来源）。

为什么要有：命令单元（backend 的 go test、frontend 的 lint、egress-spec 的整条 make 目标等）原先声明“整个仓库四段加 HEAD”，
每换一次提交就全部失效。要改成“该命令真实读到的目录和文件”，就必须先知道真实读集——这个工具用 strace 把一条命令的实际
文件访问采下来，归并成可机器核查的证据，再据此起草声明范围草案和人工评审清单。采样只在 ARM64 上由调度者执行；本文件
在本机只用假 strace 和合成轨迹测试，因此不依赖 macOS 或 Linux 的专有行为（需要 Python 3.12、strace 6.x、git）。

四个子命令（数据流：run 产出 raw，merge 把若干 raw 合并成 sample，draft 把 sample 起草成登记表草案和评审清单）：

* ``filter``：作为 ``strace -o '|…'`` 管道的另一端读标准输入，流式输出 ``declared-sample-raw/v1``。内存受控：每处理
  ``--chunk-lines`` 行就把 ``TraceParser`` 里累积的路径排空到自己的紧凑聚合里，因此峰值内存只与不同路径数有关，与总行数
  无关。它也是将来“正式执行自带采样”要复用的那一端（见 ``StreamAggregator``）。
* ``run``：从门禁清单取指定命令单元的 argv、cwd、env，构造 strace 包装并执行，保存 raw。启动前缀（launcher，ARM64 上是
  私有挂载命名空间加 tmpfs 遮蔽）放在 strace **外侧**：命令看到的挂载命名空间与正式执行完全相同，而 unshare、mount 这类
  未建模的调用不会出现在 trace 里（``TraceParser`` 严格模式对未建模调用一律记 ``unsupported-call``，会让采样白白变无效）。
  现有 ReadAuditor 的做法是 strace 在最外层、靠 runtime.prepare 剥掉 launcher 再重装；本工具直接把 launcher 放外面，省去重装。
* ``merge``：多次 raw 取并集；用 ``git ls-files`` 给仓库内路径标 ``tracked``；按采样有效性判据判定 ``valid``，不满足时写明
  原因（绝不为了“让采样过”放宽规则或删路径）。
* ``draft``：把 sample 归并成 ``declared-scope/v1`` 草案和 ``review.md``。归并只做机械规则，不做任何语义判断的自动放行；
  过宽、被枚举的目录、写入集合、未跟踪读取、网络、冷热差异等全部交给人工评审。

``merge`` 的人工登记项（命令行选项，全部原样留痕在 ``review.allowances``，``draft`` 据此写出草案；都是可选的，不给就与从前逐字相同）：

* ``--allow-unix PATH``：登记允许连接的 AF_UNIX 目标。路径按登记侧 ``unix_sockets`` 的规范校验（以 / 开头、不以 / 结尾、
  无空／.／.. 分量），不合规 merge 直接拒绝；``draft`` 把“本单元实际连过且已登记”的目标写成 scope 顶层 ``unix_sockets``。
* ``--installed-dir 前缀=合同``：依赖安装目录（例如 ``frontend/node_modules/=cache:node-modules``，pnpm 的安装结果是仓库内
  未跟踪的路径）。前缀下的**读取**不再产生 ``untracked-read``；前缀下的**写入**仍按原规则判；前缀下若有已跟踪的路径被读或被
  枚举就拒绝（安装目录不能盖住已跟踪的输入）。``draft`` 写出 ``scope.repo.installed_dirs`` 并把合同并入 ``scope.contracts``。
  前缀与合同绑定（与登记侧一致，登记侧 WP1e 起强制）：合同必须是 ``INSTALLED_DIR_PREFIXES`` 里评审过的依赖安装合同，前缀必须恰等于它
  定义的安装目录（目前只有 ``cache:node-modules`` 对应 ``frontend/node_modules/``），否则登记表可以把任何目录登记成未跟踪读取的豁免区。
* ``--transient 模式``：命令自产的暂存路径（例如 ``frontend/.eslintcache``、``frontend/vitest.config.ts.timestamp-*``）。
  仓库相对的目录前缀（以 / 结尾）或 glob（只支持 * 与 ?，合计最多 4 个，不跨 /，不支持 ** 与方括号）；落在其内的**未跟踪**路径，只要本次
  采样里被**写过**，写入与读回就不产生 ``write``、``untracked-read`` 无效原因；只读不写的路径不豁免。运行前后 git status
  必须为空的判据照旧，“写完即删”靠它兜底。``draft`` 把命中过的模式并入 ``scope.repo.repo_ignored``。
* ``--class-prefix 前缀=类别[:合同]``：登记内置分类表没有覆盖、但经评审认定可以接受的仓库外前缀。与登记侧一致的宽度限制（登记侧 WP1e 起强制）：
  class=scratch（读写执行全放行）的前缀只许 ``/dev/null``、``{TMP}/``、``{WORK}/go-tmp/``、``{HOME}/.config/go/telemetry/`` 或它们之下更窄的路径，
  class=virtual（不绑定任何内容）的前缀只许 ``/proc/``、``/sys/``、``/dev/``、``/run/`` 或其下更窄的路径；其它类别不限。
* 以上三条命令行校验（以及 ``--allow-unix``）都抄自登记侧、各自测试：采样器不 import 登记侧的私有常量（``INSTALLED_DIR_PREFIXES``、``MAX_GLOB_WILDCARDS``、
  ``SCRATCH_CLASS_PREFIXES``、``VIRTUAL_CLASS_PREFIXES``、``CACHE_CONTRACT_LOCK_FILES`` 都是采样器自己的一份），``test_ci_declared_sampler_followup`` 用登记侧的
  同名常量与校验函数交叉核对同值、同接受／拒绝；登记侧改了这些规则，要在这里同步评审。

git 用法是否“固定”（``draft`` 的判定）：声明级承接要求结果只取决于范围内的文件，而 scope 的 ``repo.head`` 只能是 false。单元里只要有
一条 git 子进程命令不“固定”，登记条目就写成 ``{"status": "rerun-only", "reason": …, "head": true}``（采样本身无效时 reason 里并列，
head 取或），评审清单第 11 节列出全部未固定的命令。固定＝所有修订参数都是完整 40 位小写十六进制提交号（可带 ``:路径``、``^{tree}``、
``^{commit}``），且子命令属于只读、按对象寻址的一类（show、cat-file、rev-parse（选项只有 --verify、-q，修订都固定）、merge-base、ls-tree、diff-tree，以及两端都是
完整提交号的 log／rev-list）。HEAD、refs/…、origin、main、标签、缩写、相对写法，tag、worktree、branch、for-each-ref、ls-remote、remote、
config、status、stash 等读引用库或写仓库状态的子命令，-C、-c、--git-dir 等全局选项，清单之外的选项，不可见的参数——都算未固定；
拿不准一律算未固定（误判只让该单元保持 rerun-only，漏判会让登记表自相矛盾）。采样器分不清 git 命令作用在哪个仓库（测试常在临时合成
仓库里造提交、打标签），所以按命令字面判。
规格 v2（WP4d，独立验收 #3、#4 之后调度者统一的口径，登记侧按同一份规格改）：①选项表里没有 ``--oneline``（它等于 ``--pretty=oneline --abbrev-commit``，缩写长度随对象库
大小与 ``core.abbrev`` 配置变化）；②``--format=``／``--pretty=`` 的取值只放行精确的 ``raw``，和“普通字符＋白名单占位符”的自定义格式串（``format:<串>``、``tformat:<串>``，
或含 ``%`` 的无前缀值）——白名单 ``%H %T %P %an %ae %aI %at %cn %ce %cI %ct %s %b %B %n %%`` 与 ``%x`` 加两位十六进制，其余 ``%…`` 一律未固定（``%D %d %N %S %g* %(…) %h %t %p
%ad %cd %aD %cD %ar %cr %aN`` 等都在内），命名格式（oneline、medium……）和 ``pretty.<名字>`` 配置别名也未固定；③``merge-base``、``cat-file``、``rev-parse`` 的 argv 里出现 ``--``
一律未固定（它在这三个子命令里只是“选项结束”，其后仍是修订；``git merge-base A B -- HEAD`` 的输出随 HEAD 变化），``show``／``log``／``rev-list``／``ls-tree``／``diff-tree`` 里 ``--``
之后才是路径；④所有“数字”判定（``-n <数字>``、``-<数字>``、``--max-count=<数字>``）只接受 ASCII 数字，``²`` 之类的 Unicode 数字未固定；⑤``ls-tree --abbrev=<n>`` 未固定（WP1g：
``<n>`` 只是缩写长度的下限，对象库出现碰撞时输出会更长；拿不准一律算未固定，没有任何已登记单元用它）。
已知残余：``log``／``show`` 默认显示 ``refs/notes/commits`` 里的 notes（规格不要求 ``--no-notes``；被测仓库里极少见）。

覆盖闭合与覆盖自检（``draft`` 的默认行为，``--no-coverage-check`` 关闭）：起草只按读集归并，可登记侧的覆盖检查（``unit_records.declared_coverage``）
另有自己的规则，两边对不上，草案就会在登记时才被拒绝。所以 ``draft`` 先做覆盖闭合，再用登记侧的同一个函数自检：

* 仓库内的 missing 探测（命令找一个不存在的文件，例如 TypeScript 的模块解析候选、eslint 的配置候选）：为它所在的**最近的、不是树根的父目录**补一个
  range（``node_modules/@types`` 补 ``node_modules/``）。range 的摘要覆盖目录下全部跟踪文件，“这里没有这个文件”也就被绑定了；目录不存在或
  下面没有跟踪文件也照补，将来有人在里面新增跟踪文件摘要就变。只被 stat／dir、其下没有任何声明内容的目录，为目录自身补 range。已被现有
  range 或单文件覆盖的、``.git`` 下的、依赖安装目录前缀与 ``repo_ignored`` 内的不补。树根以下的 ``node_modules/`` 是**树根**的
  node_modules，与 ``frontend/node_modules/`` 这样的安装目录前缀不是一回事。
* 树根目录自身被枚举或读取，或树根直接子项有 missing 探测（``package.json``、``eslint.config.js``、``RCS``……）：``repo.root_listing: true``
  （登记侧用树根下被跟踪的顶层名字摘要绑定）。``go.mod``、``go.work`` 这类祖先探测名登记侧本来就放行，不触发。
* ``repo_ignored`` 的目录前缀不得与依赖安装目录前缀重叠或盖住它：落在安装目录里的被写文件、写在安装目录的上级目录或树根下的文件，改用精确路径；
  被写入的 ``/dev/null`` 在 ``outside`` 里补精确前缀 ``/dev/null``（scratch 类）；``/run/`` 归 virtual 类。
* 依赖锁文件：登记侧规定 ``scope.contracts`` 声明了工具链合同（``toolchain:go``、``toolchain:pnpm``）或缓存合同（``cache:node-modules``→``frontend/pnpm-lock.yaml``、
  ``cache:go-mod-cache``→``backend/go.sum``，含 ``--installed-dir`` 带来的合同），就必须在 ``repo.files`` 里单独列出它的锁文件；``draft`` 据此补入（``TOOLCHAIN_LOCK_FILES``、
  ``CACHE_CONTRACT_LOCK_FILES``），评审清单注明“由合同要求补入”；文件必须是仓库里跟踪的，不存在或未跟踪的不凭空添加，只标“缺失”。
* 自检：起草好的 scope 连同 sample 交给登记侧的 ``declared_coverage``（按文件路径加载同目录的 ``unit_records.py``，只读使用）。通过才写 ``declared``；
  不通过写成 ``{"status": "rerun-only", "reason": "声明级覆盖自检不通过：N 处未覆盖（规则统计…；前 3 处…）"}``（head 为 false），评审清单第 13 节
  按规则分组列出全部未覆盖项。采样本身无效、git 用法未固定的单元已是 rerun-only，不再自检；登记侧模块不可用时跳过自检并在清单里注明。

被丢弃的 strace 标记行：Go 运行时的线程被杀或退出时，strace 把它当作还在系统调用里，用残留寄存器解码出一行垃圾（名字是随机的系统调用名，参数
乱七八糟）。除了不带名字的裸标记 ``<pid> ???( <detached ...>``，还有带名字的两种形态：行尾是 ``<detached ...>``（名字任意，包括 syscall_0x…
与 ???），和行尾是 `` = ?`` 而名字不是 execve、execveat、exit、exit_group 的。filter 把它们都丢弃并计入 ``coverage.detached_markers``（不新增
coverage 键：登记侧对它是闭合的），带名字的行原样留样（别名化、≤300 字符、最多 8 条、去重）在 raw 顶层 ``detached_samples``，merge 后进
``review.detached_samples``，评审清单第 12 节列出；合并后总数超过 64 判采样无效。

丢弃垃圾行的两项留痕与自检（只给评审看或失败关闭，不改变“个位数垃圾行无害”的口径）：

* 副作用类单独计数：被丢弃的行里名字属于副作用类（``SIDE_EFFECT_CALLS``：unlink／rename／link／symlink／mkdir／rmdir／truncate／chmod／chown／各种 setxattr、
  creat、带 O_WRONLY／O_RDWR／O_CREAT／O_TRUNC 的 open／openat、connect／sendto 等会改文件系统或对外连接的调用名；名字解析不出来或随机名的不算）的条数写在 raw 顶层
  ``detached_side_effects``（merge 后进 ``review.detached_side_effects``，有才写；不进字段闭合的 coverage，登记侧不解释 review）。这类调用若真的生效，trace 里
  不会有任何痕迹，是丢弃垃圾行的已知代价；``detached_samples`` 的 8 条上限里副作用类优先留样（副作用类在前）。名字随机的垃圾行很常见（见过 lsetxattr、
  unlinkat、syscall_0x…），所以只计数留样、不按名字直接判无效。
* 线程是否真的消失：filter 收尾时核对每条被丢弃的、带进程号前缀的垃圾行（含裸标记）——它的 tid 在之后还出现过任何行，就说明那个线程没有消失、被丢弃的那一行可能是
  真实调用，记未解析原因 ``sampler:detached-not-terminal:<tid>``（同一个 tid 只记一次，最多 20 个不同原因，超出的合并成 ``…:more``）。追踪表内存随不同 tid 数增长
  （上限 ``MAX_DETACHED_TIDS``，超出记 ``sampler:limit:detached-tids``），与总行数、聚合块大小无关。

未解析原因的诊断样例：对每个不在容忍集合内的 ``unresolved_reasons`` 原因，filter 保留前 3 条原始 strace 行（采样根的绝对路径
换成别名、每条截到 300 字符，最多 20 个原因），写在 raw 的 ``coverage.unresolved_samples``，merge 后进 ``review.unresolved_samples``
并拼进 ``[unresolved]`` 无效原因，评审清单第 9 节逐条列出。容忍的 ``unfinished-calls-at-eof`` 另保留最多 8 条悬着调用的起始行
（``coverage.unfinished_samples``）。

设计要点（改动前先读）：

* 只复用 ``read_audit.TraceParser(strict=True)``（不带 runtime_context：没有输出归属，也没有 200000 事件上限），以及它的
  公共属性 accesses、lines、unresolved、unresolved_reasons、pending、deferred_count，外加清空 accesses 与 samples 来裁剪内存。
  诊断样例另外读 pending 的值（悬着调用的起始部分，不含 ``<unfinished ...>``）和 deferred（缓存的原始行；用 getattr 取，没有就不取样）。
  不调用 ``trace_document``：它会把全部路径序列化成一个大 JSON，也会按根目录筛路径，不适合目录粒度的采样；不 import
  read_audit_runtime，不改 read_audit.py（它的摘要被钉在受管工具里，control 四链的 sidecar 还会动态复用它）。
  测试里的 T-API 钉住这些依赖，审计器改名或改语义会立刻红。
* 仓库内路径完整保留（标 tracked、逐个核对），仓库外路径按目录聚合（kinds 并集加计数）：仓库内路径的内容进入范围摘要，
  必须逐个能说清；仓库外的 GOCACHE 内容寻址文件、GOROOT 源码是海量文件，逐个登记既不可能也没有意义，它们靠合同和类别
  绑定（工具链、缓存、系统库），只需知道读了哪些目录。语义分类只发生在验证侧和 draft 的前缀表里，采样本身不分类。
* 路径别名：证据与草案里只出现 ``{TREE}``（测试树根）、``{WORK}``（Go 缓存的父目录）、``{HOME}``、``{TMP}``，采样时的
  具体绝对路径只在 ``aliases`` 里留痕，因此跨工作目录不失效。
* **设计盲区（网络）**：网络检查只覆盖 ``connect``（``STRACE_OPTIONS`` 的 trace 集合里没有 ``sendto``／``sendmsg``／``sendmmsg``），所以 UDP 未连接套接字
  直接向外部地址 ``sendto`` 看不到。已知用法下这不构成漏洞：TCP 外连一定经 ``connect``，glibc 与 Go 的 DNS 解析也经 ``connect``（先 ``connect`` UDP 套接字
  再发送）。把别的写法当成“已覆盖”是错的——新增依赖裸 ``sendto`` 的命令单元前，要先扩 trace 集合并重采。trace 集合（``STRACE_OPTIONS``）不因此改动。
* exec 调用：``execve`` 与 ``execveat`` 都收集 git 子进程的 argv（``execveat`` 的路径为空串即 AT_EMPTY_PATH 时，可执行文件取 dirfd 注解里的路径）；判定“是 git 命令”
  看 argv[0] 的 basename 或可执行文件路径的 basename 是 git（别名调用 ``execve("/usr/bin/git", ["gitx", …])`` 也收集，argv[0] 原样保留，之后按“不是 git 命令行，
  无法判定”失败关闭）；行首是 exec 调用却认不出形态记 ``sampler:exec-unparsed``，不悄悄漏掉。
* strace 选项：沿用审计器的文件类调用加进程派生，另加 ``connect``（收集网络目的地）。ARM64 实测（strace 6.8）：沿用审计器的
  ``-e verbose=none`` 时，execve 的 argv 和 connect 的 sockaddr 都被打印成不透明指针——读 git 历史的提交号取不到，连接的
  目的地也判不了——所以这里改成只让 execve、execveat、connect 以 verbose 形式解码（``-e verbose=execve,execveat,connect``），
  其它调用的结构仍缩写成指针。若 argv 或 sockaddr 仍然不可见，filter 会记下哨兵值或 ``unparsed``，merge 判采样无效，绝不静默漏判。
* 多线程下 connect 会被拆成 ``connect(… <unfinished ...>`` 与 ``<... connect resumed>) = 返回值`` 两行，中间夹着别的进程的行。
  filter 把这两种行都按进程号配对截走、不交给 TraceParser（它对 connect 记 unsupported-call，对没有起始行的 resumed 记
  resume-without-start）；缺 start、重叠、流结束仍悬着都记成未解析原因，采样随之无效。
* 过滤器是 strace 的管道子进程，与被跟踪命令同一个进程组：它忽略 SIGTERM 与 SIGINT，超时终止整组时仍能读到 EOF 后把已有读集
  写出来。``run`` 在采样树运行前就不干净时直接拒绝（树不干净的采样没有意义，别白跑几十分钟）；运行后不干净则照常写 raw，
  由 merge 判无效。冷缓存遇到非空旧目录只改名保留，不删除。非 Go 单元（前端、Python 工具）不用 GOCACHE，但冷热对比对它们的
  性能缓存（eslint、vitest、tsbuildinfo）同样有意义：``run --no-go-cache`` 让热缓存不再要求 go-build-cache 非空、冷缓存也
  不改名它，raw 的 run 记录里如实记下 ``no_go_cache``（不加该开关时不写这个键，与从前逐字相同）。

证据文件约定（登记侧的覆盖检查按这些约定读取 sample）：

* ``repo_accesses`` 的键是 ``{TREE}/相对路径``（``{TREE}`` 自身表示测试树根）；第三项 tracked 表示该路径是 git 跟踪的文件，
  或含跟踪文件的目录。唯一的目录聚合键是 ``{TREE}/.git/objects/``（内容寻址的对象文件成千上万，逐个保留没有意义）。
* ``outside_accesses`` 的键以 ``/`` 结尾表示目录聚合：目录自身被枚举（list）算在该目录的键下，其余访问算在父目录的键下；
  不以 ``/`` 结尾的是精确路径，只出现在 /dev、/proc、/sys、/etc、``{HOME}`` 这几个需要逐条看清的前缀下和别名根自身。
  ``{WORK}/go-tmp/`` 与 ``{TMP}/`` 是暂存区，整体折叠成前缀本身。计数是各聚合块内去重后的路径出现次数之和，只作规模提示，
  不参与覆盖判定。进程号和线程号规范化：``/proc/<进程号>/`` 记成 ``/proc/self/``，``/proc/self/task/<线程号>/`` 记成
  ``/proc/self/task/{TID}/``。聚合键丢了文件名，所以祖先链目录（别名根 TREE、WORK、HOME、TMP 自身及它们的各级祖先：``/``、``/var``、
  ``/var/tmp``……）上的 ``read`` 要先分清：这些路径按构造就是目录，读它们只能是不带 O_DIRECTORY 地打开目录（Go 的 ``os.Open(目录)``），
  filter 把它们的 ``read`` 记成 ``dir``。于是祖先链目录的聚合键（``/var/``）上剩下的 ``read``，一定是读了祖先目录下别的直接子项
  （文件，或不是祖先的目录）。
* ``coverage.unresolved`` 等于 ``unresolved_reasons`` 各计数之和；采样器自己的规模保护和流结束时仍悬着的调用也写成显式原因
  （``sampler:*``、``unfinished-calls-at-eof``、``deferred-process-events-at-eof``）。其中只有 ``unfinished-calls-at-eof``
  在容忍集合里（多线程进程退出时 strace 不为还在系统调用里的线程打印 resumed，正常现象；合并后的总数上限
  ``DECLARED_MAX_UNFINISHED_AT_EOF``，超过判无效），其余都不容忍。
* ``git_commands`` 里可能有哨兵 ``["git", "<argv-not-visible>"]``；``network`` 的 family 除 AF_INET、AF_INET6、AF_UNIX、
  AF_UNSPEC 外还可能是别的 AF_* 或 ``unparsed``（认不出结构，一律不能证明是本机连接）。

ARM64 使用方法（调度者执行；变量一律 ``${VAR}``）::

    python3 -B ${DRV}/declared_sampler.py run --manifest ${SAMPLE}/gates-manifest.json --unit backend:go-test \\
        --out ${SAMPLE}/raw --tree ${TREE} --work ${SAMPLE} --cache cold --launcher-json "${LAUNCHER}"
    python3 -B ${DRV}/declared_sampler.py run ... --cache warm --launcher-json "${LAUNCHER}"
    python3 -B ${DRV}/declared_sampler.py merge --raw ${SAMPLE}/raw/backend-go-test.cold.raw.json \\
        --raw ${SAMPLE}/raw/backend-go-test.warm.raw.json --tree ${TREE} --out ${SAMPLE}/samples/backend-go-test.sample.json
    python3 -B ${DRV}/declared_sampler.py draft --samples ${SAMPLE}/samples --tree ${TREE} \\
        --registry-out ${SAMPLE}/command_input_scopes.draft.json --review-out ${SAMPLE}/review.md

前端单元（不用 Go 缓存，依赖装在仓库内未跟踪的 node_modules，lint 与 vitest 会写自己的缓存或随机名临时文件）::

    python3 -B ${DRV}/declared_sampler.py run --manifest ${SAMPLE}/gates-manifest.json --unit frontend:lint \\
        --out ${SAMPLE}/raw --tree ${TREE} --work ${SAMPLE} --cache cold --no-go-cache --launcher-json "${LAUNCHER}"
    python3 -B ${DRV}/declared_sampler.py merge --raw ${SAMPLE}/raw/frontend-lint.cold.raw.json --tree ${TREE} \\
        --installed-dir frontend/node_modules/=cache:node-modules --transient frontend/.eslintcache \\
        --out ${SAMPLE}/samples/frontend-lint.sample.json

退出码：0 成功；1 仅 ``run``（命令退出码非零或超时，raw 仍已写出，merge 会判采样无效）；2 参数、环境或输入非法（未采样）；
3 仅 ``run``（过滤器没有产出，采样基础设施故障）。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve()

RAW_SCHEMA = "declared-sample-raw/v1"
SAMPLE_SCHEMA = "declared-sample/v1"
SCOPE_SCHEMA = "declared-scope/v1"
REGISTRY_SCHEMA = "declared-inputs-registry/v1"
GATES_SCHEMA = "unit-executor-gates/v1"

# 采样有效性里“可以容忍的 unresolved 原因”。匹配约定与登记侧（unit_records.DECLARED_TOLERATED_UNRESOLVED）一致：以 ":" 结尾的项按
# 前缀匹配，其余项精确匹配（免得名字相近的别种原因蹭上豁免）：
# * symlink-metadata-not-bound: ——pnpm 的 node_modules 全是符号链接，Go 的 os.Lstat 也会发 AT_SYMLINK_NOFOLLOW 的 newfstatat；
#   严格解析对这类调用只增加计数、不丢路径（路径照常记录），所以可以容忍；
# * unfinished-calls-at-eof ——多线程进程（node、vitest、go）退出时，strace 不会为还在系统调用里的线程打印 resumed，流末尾就留下
#   少量永远配不上的 ``<unfinished ...>``。被打断的调用没有返回成功，不贡献读集；但数量必须有上限（DECLARED_MAX_UNFINISHED_AT_EOF），
#   免得流被截断造成的大面积丢行被当成正常现象。
# 其余原因（unrecognized-line、unsupported-call、resume-without-start、sampler:* 等）都意味着有读取没被看见，一律判采样无效。
DECLARED_TOLERATED_UNRESOLVED = ("symlink-metadata-not-bound:", "unfinished-calls-at-eof")
# unfinished-calls-at-eof 的计数上限，按合并后的总数算（与登记侧覆盖检查的上限相同）：超过判采样无效。
DECLARED_MAX_UNFINISHED_AT_EOF = 64


def reason_tolerated(reason: str) -> bool:
    """unresolved 原因是否在容忍集合里：以 ":" 结尾的项前缀匹配，其余项精确匹配（unfinished-calls-at-eof-xx 之类蹭不上）。"""
    return any(reason.startswith(item) if item.endswith(":") else reason == item for item in DECLARED_TOLERATED_UNRESOLVED)

ALIAS_NAMES = ("TREE", "WORK", "HOME", "TMP")

# 读取方式（与 read_audit 的 kinds 同名）。用位掩码聚合：几十万个路径时，一个小整数比一个 set 对象省得多。
KIND_BITS = {"read": 1, "exec": 2, "write": 4, "missing": 8, "stat": 16, "dir": 32, "list": 64}
KIND_NAMES = sorted(KIND_BITS)  # 输出一律按字母序，与 read_audit.trace_document 的 sorted(kinds) 一致。
_READ_EXEC = KIND_BITS["read"] | KIND_BITS["exec"]
_READ = KIND_BITS["read"]
_DIR = KIND_BITS["dir"]
_WRITE = KIND_BITS["write"]
_LIST = KIND_BITS["list"]

# strace 选项：文件类调用、目录枚举、进程派生、fchdir（还原相对路径要用），加 connect（收集网络目的地）。execve、execveat 与
# connect 以 verbose 形式解码：execve 才能看到 git 子进程的 argv，connect 才能看到目的地址（ARM64 strace 6.8 实测，
# verbose=none 时两者都只打印成不透明指针）；其它调用的结构仍缩写成指针，与 verbose=none 等价（省 strace 自己的格式化开销，
# 也避免把环境变量打进轨迹）。
STRACE_OPTIONS = ("-f", "-qq", "--seccomp-bpf", "-y", "-s", "4096", "-e", "verbose=execve,execveat,connect", "-e", "signal=none",
                  "-e", "trace=%file,getdents64,clone,clone3,fork,vfork,fchdir,connect")

# 已知的 ARM64 启动前缀（与门禁入口脚本传给 plan 的 --launcher-json 同一份；这里独立保存一份只为“未显式给出时自动识别”，
# 不 import read_audit_runtime）。识别不出来时 run 会拒绝而不是猜测。
KNOWN_LAUNCHER = ["unshare", "-m", "--propagation", "private", "bash", "-c",
                  'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec "$@"', "entry-gates"]

# 过滤器内存与规模保护。超过任何一项都会在 unresolved_reasons 里记 sampler:limit:*，采样随之判无效，而不是悄悄丢数据。
DEFAULT_CHUNK_LINES = 100_000          # 每处理这么多行就排空一次 TraceParser 的路径字典
MAX_REPO_PATHS = 2_000_000             # 仓库内不同路径数上限（约 250 字节一条，最坏约 500 MB）
OUTSIDE_DIR_LIMIT = 100_000            # 仓库外目录键数上限，超过就折叠到祖先目录
OUTSIDE_FULL_LIMIT = 20_000            # 下面这些前缀下“完整保留”的路径数上限
MAX_GIT_COMMANDS = 20_000
MAX_NETWORK_TARGETS = 20_000
# 诊断样例（别名化、截断后的原始 strace 行）的体量上限：每个未解析原因最多留 3 条、最多 20 个原因；容忍的悬着调用最多留 8 条起始
# 行；每条最长 300 字符。8 与 300 同时是登记侧对 coverage.unfinished_samples 的类型长度检查上限，这里不能比它大。
MAX_SAMPLES_PER_REASON = 3
MAX_SAMPLE_REASONS = 20
MAX_UNFINISHED_SAMPLES = 8
SAMPLE_LINE_CHARS = 300
# 被当作标记丢弃的 strace 行（裸标记与线程中途消失留下的垃圾行）：留的样例条数，以及合并后总数的软上限——超过判采样无效，
# 免得大面积丢流（strace 被截断、大批线程消失）被当成正常现象。真实的 Go 单元里一份采样通常只有个位数。
MAX_DETACHED_SAMPLES = 8
DETACHED_MARKER_LIMIT = 64
# 样例拼进 [unresolved] 无效原因后，整条原因文字的总长上限（超出的样例部分被截掉，原因清单本身不截）。
INVALID_REASON_CHARS = 600
# review.allowances 里每条登记项（安装目录、自产暂存路径）留痕的路径样例条数。
ALLOWANCE_SAMPLE_PATHS = 20
# 仓库外路径里需要逐个看清的前缀：/dev（要认出 /dev/null 这种无害写入）、/proc 与 /sys（虚拟文件，数量有限）、/etc（环境
# 指纹只覆盖包管理器管理的文件，手改的文件要逐个评审）、HOME（配置文件要逐路径登记存在性与内容）。其余仓库外路径按目录聚合。
OUTSIDE_FULL_PREFIXES = ("/dev/", "/proc/", "/sys/", "/etc/", "{HOME}/")
OUTSIDE_FULL_EXACT = frozenset({"/dev", "/proc", "/sys", "/etc", "{HOME}"})
# 暂存区：里面读写什么都不绑定（go-build 临时目录名带随机数，t.TempDir 也是），按前缀整体折叠，免得目录键数以万计。
OUTSIDE_FOLD_PREFIXES = ("{WORK}/go-tmp/", "{TMP}/")
ALIAS_ROOTS = frozenset({"{WORK}", "{HOME}", "{TMP}"})

# strace 在线程于系统调用中途被分离或进程退出时打印的标记行：调用号未知、没有路径、也没有成功返回，不携带读取信息。
# 必须在交给 TraceParser 之前精确识别并单独计数，否则严格解析会把它们记成 unrecognized-line。这条正则要精确：
# 任何别的未识别行仍然让采样无效。
_MARKER = re.compile(r"^\d+\s+\?\?\?\( <(?:detached|unfinished) \.\.\.>\s*$")
# strace 对“中途消失的线程”打印的垃圾行（Go 运行时的线程被杀或退出时，strace 把它当作还在系统调用里，用残留寄存器解码出一行名字、
# 参数都乱七八糟的东西）。上面 _MARKER 是其中不带名字的裸标记；带名字的有两种形态，都形如“<进程号> <名字>(…”，名字可以是任意系统
# 调用名、syscall_0x… 或 ???：
#   形态 A：行尾是 <detached ...>，例如 ``711353 lsetxattr("\270J\321", 0x17, 0x23fdca6b7008, 39572929737628, XATTR_CREATE <detached ...>``；
#   形态 B：行尾是 “ = ?”（调用没有返回），例如 ``751440 lsetxattr(0xffffffffffffff00, "/usr/local/go/src/internal/abi", "", 0, 0) = ?``，
#           第一个参数是指针常量、第二个参数却恰好像路径；调用名是 execve、execveat、exit、exit_group 的除外——它们本来就可以 “= ?”。
# 这类行不携带任何读取信息，交给严格解析器只会制造 plain-path-not-resolved、result-or-string-incomplete、unsupported-call:syscall_0x…
# 之类的假未解析原因，所以和裸标记一样在 filter 里丢弃并计数（计入 coverage.detached_markers，样例另存）。
_GARBAGE_CALL = re.compile(r"\d+\s+([^\s(]+)\(")
_DETACHED_END = "<detached ...>"
_LOST_RESULT_END = " = ?"
_MAY_END_WITHOUT_RESULT = frozenset({"execve", "execveat", "exit", "exit_group"})
# 被丢弃的垃圾行里“名字属于副作用类”的单独计数（评审提示，不影响有效性判定）：这类调用若真的生效，就是会改文件系统或对外连接的调用——线程中途消失，
# 它的结果无从得知，trace 里却一点痕迹都不留（最坏情形：被杀线程的最后一次写入／unlink／rename／外连已经生效而采样看不见）。名字解析不出来或是随机名
# （``???``、``syscall_0x…``）的不算；真实垃圾行的名字本来就是随机的（见过 lsetxattr、unlinkat、syscall_0x…），所以只计数留样、不按名字直接判无效。
# 名字集合：unlink／rename／link／symlink／mkdir／rmdir／mknod／truncate／chmod／chown／各种 xattr 的写入与删除／改时间戳／creat，加会对外连接或在文件系统
# 里建节点的 connect、sendto、sendmsg、sendmmsg、bind，以及改挂载的 mount、umount；open／openat／openat2 只在参数里带写入类标志时才算。
SIDE_EFFECT_CALLS = frozenset({
    "unlink", "unlinkat", "rename", "renameat", "renameat2", "link", "linkat", "symlink", "symlinkat", "mkdir", "mkdirat", "rmdir", "mknod", "mknodat",
    "truncate", "truncate64", "ftruncate", "ftruncate64", "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "chown32", "fchown", "fchown32",
    "lchown", "lchown32", "fchownat", "setxattr", "lsetxattr", "fsetxattr", "setxattrat", "removexattr", "lremovexattr", "fremovexattr", "removexattrat",
    "utime", "utimes", "utimensat", "futimesat", "creat", "connect", "sendto", "sendmsg", "sendmmsg", "bind", "mount", "umount", "umount2", "pivot_root"})
_OPEN_CALLS = frozenset({"open", "openat", "openat2"})
_WRITE_OPEN_FLAGS = re.compile(r"\bO_(?:WRONLY|RDWR|CREAT|TRUNC)\b")
# 同一份 raw 里最多追踪多少个“出现过垃圾行的 tid”（用来核对它之后是否真的再没有任何行，见 StreamAggregator.finish）：每个 tid 存一条别名化的垃圾行
# （≤300 字符），内存有界；超出上限即记 sampler:limit:detached-tids（失败关闭）。垃圾行总数超过 64 本来就判采样无效，上限留得很宽。
MAX_DETACHED_TIDS = 1_000
_CONNECT_CALL = re.compile(r"^(?:\d+\s+)?connect\((.*)$", re.S)
_CONNECT_RESUMED = re.compile(r"^(?:\d+\s+)?<\.\.\. connect resumed>")
_EXECVE_CALL = re.compile(r'^(?:\d+\s+)?execve\("((?:[^"\\]|\\.)*)"(?:\.\.\.)?, ')
# execveat(dirfd, "路径", [argv], …)：dirfd 是 AT_FDCWD 或数字，-y 下带 <注解>（注解是该描述符指向的路径，可能含逗号，所以非贪婪地一直找到 “>, "” 为止）。
# AT_EMPTY_PATH 时路径是空串，可执行文件就是 dirfd 注解里的路径（fexecve 就是这样调用的）。
_EXECVEAT_CALL = re.compile(r'^(?:\d+\s+)?execveat\(([A-Za-z0-9_-]+(?:<.*?>)?),\s*"((?:[^"\\]|\\.)*)"(?:\.\.\.)?, ')
# 行首（可带进程号）是 execve／execveat 调用：只认调用本身，不认 “<... execve resumed>”、路径里碰巧含这个词的行。
_EXEC_START = re.compile(r"^(?:\d+\s+)?(execve|execveat)\(")
_DIRFD_ANNOTATION = re.compile(r"[^<]*<(.*)>", re.S)
_SOCK_INET = re.compile(r'sa_family=AF_INET, sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]*)"\)')
_SOCK_INET6 = re.compile(r'sa_family=AF_INET6, sin6_port=htons\((\d+)\),.*?inet_pton\(AF_INET6, "([^"]*)"')
_SOCK_UNIX = re.compile(r'sa_family=AF_UNIX, sun_path=(@?)"((?:[^"\\]|\\.)*)"(\.\.\.)?')
_PID = re.compile(r"^(\d+)\s+")
_SOCK_FAMILY = re.compile(r"sa_family=(AF_[A-Z0-9_]+)")
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_PROC_PID = re.compile(r"^/proc/\d+(?=/|$)")
_PROC_TASK = re.compile(r"^(/proc/self/task)/\d+(?=/|$)")
# 任意调用的 resumed 行（只用于取样：把 resumed 行与 TraceParser 记着的起始部分拼回完整的调用文本，诊断样例才带得上路径）。
# 标记必须紧跟进程号，与解析器自己的识别方式一致，免得路径里碰巧含有 "resumed>" 的普通行被当成 resumed 行。
_RESUMED_LINE = re.compile(r"^(\d+)\s+<\.\.\. [a-z_][a-z0-9_]* resumed>")
_UNFINISHED_MARK = "<unfinished ...>"
# 在一段文本里认路径前缀时的边界：前面不能紧跟路径字符（免得 /x/tree 命中 /y/x/tree），后面不能紧跟名字字符（免得 /x/tree 命中
# /x/tree-other）；后面是 / 、引号、> 等都算前缀的结尾。
_TEXT_BEFORE = r"(?<![\w/.~@+%-])"
_TEXT_AFTER = r"(?![\w.~@+%-])"

# git 子进程的 argv 不可见（strace 没有解码 execve 参数）时留下的哨兵：merge 看到它就判采样无效。
GIT_ARGV_HIDDEN = "<argv-not-visible>"
# 写入 /dev/null 没有任何可观察状态，既不是输入也不会改变结果；Go 的 os/exec 在子进程输出未接管时就会打开它。这是调研里
# 没有写明、但 Go 测试采样必然遇到的情形，唯一的写入豁免，merge 会在 review.writes.sinks 里留痕供评审。
DECLARED_WRITE_SINKS = ("/dev/null",)


class SamplerError(RuntimeError):
    """参数、环境或输入文件不合法（命令行返回 2）；采样本身的好坏不走这个异常，而是写进 valid 与 invalid_reasons。"""


# ---------------------------------------------------------------------------
# 摘要、时间与序列化
# ---------------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    """与 unit_records.canonical 逐字相同的规范化，command_sha256 和 launcher_sha256 要和登记侧的算法一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(unit_id: str) -> str:
    """单元 ID 变成文件名安全的短名（backend:go-test 变成 backend-go-test）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", unit_id)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(", ", ": "))


def _pretty(value: Any, indent: int = 0) -> str:
    """确定性的半展开 JSON：短值一行，长列表每个元素一行。

    证据文件会提交进仓库并被人 diff，所以要“每条路径一行”；同时文件字节必须稳定（evidence_sha256 取文件字节摘要）。
    一律 ASCII 转义：strace 里的非 UTF-8 路径经 surrogateescape 读入后是孤立代理字符，ensure_ascii 把它们写成合法的
    JSON 转义，读回来还是原样，文件本身永远是纯 ASCII。
    """
    one = _compact(value)
    if not isinstance(value, (dict, list)) or not value or len(one) + indent <= 120:
        return one
    pad = " " * (indent + 2)
    if isinstance(value, list):
        rows = [pad + (_compact(item) if len(_compact(item)) <= 600 else _pretty(item, indent + 2)) for item in value]
        return "[\n" + ",\n".join(rows) + "\n" + " " * indent + "]"
    rows = [pad + json.dumps(key, ensure_ascii=True) + ": " + _pretty(value[key], indent + 2) for key in sorted(value)]
    return "{\n" + ",\n".join(rows) + "\n" + " " * indent + "}"


def dumps_document(document: Mapping[str, Any]) -> str:
    return _pretty(dict(document)) + "\n"


def write_document(path: Path, document: Mapping[str, Any], *, exclusive: bool = False) -> None:
    """原子写入（临时文件加 replace）；exclusive 时目标已存在就拒绝覆盖，证据不能被静默改写。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise SamplerError(f"拒绝覆盖已存在的文件：{path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(dumps_document(document), encoding="utf-8")
    os.replace(temporary, path)


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SamplerError(f"读不出 JSON 文件：{path}（{error}）") from error
    if not isinstance(value, dict):
        raise SamplerError(f"JSON 顶层必须是对象：{path}")
    return value


def kinds_of(mask: int) -> list[str]:
    return [name for name in KIND_NAMES if mask & KIND_BITS[name]]


def mask_of(kinds: Iterable[str]) -> int:
    mask = 0
    for kind in kinds:
        mask |= KIND_BITS[kind]
    return mask


# ---------------------------------------------------------------------------
# 加载审计器的解析器（只用 TraceParser）
# ---------------------------------------------------------------------------

_READ_AUDIT_MODULE = None


def read_audit_module():
    """按文件路径加载同目录的 read_audit.py。

    驱动目录不是 Python 包（ARM64 上执行器从驱动目录运行），所以不能写 ``from tools.ci import read_audit``。加载期间强制
    不写字节码：在驱动目录或测试树里留下 __pycache__ 会让门禁前置检查直接退出。
    """
    global _READ_AUDIT_MODULE
    if _READ_AUDIT_MODULE is None:
        name = "_declared_sampler_read_audit"
        spec = importlib_util.spec_from_file_location(name, HERE.with_name("read_audit.py"))
        if spec is None or spec.loader is None:
            raise SamplerError("找不到同目录的 read_audit.py")
        module = importlib_util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            sys.modules[name] = module
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
        _READ_AUDIT_MODULE = module
    return _READ_AUDIT_MODULE


# ---------------------------------------------------------------------------
# 路径别名
# ---------------------------------------------------------------------------


class AliasTable:
    """绝对路径换成别名路径。

    最长前缀优先：采样根里测试树常在 WORK 之下（``${SAMPLE}/test-tree`` 与 ``${SAMPLE}/go-build-cache``），必须先认 TREE。
    每个别名同时登记原样路径和 realpath：内核经 ``-y`` 给出的是解析后的真实路径，而命令行参数里的字面路径可能没解析。
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        if set(values) != set(ALIAS_NAMES):
            raise SamplerError(f"别名必须恰为 {'、'.join(ALIAS_NAMES)}，收到：{sorted(values)}")
        self.values: dict[str, str] = {}
        pairs: list[tuple[str, str]] = []
        for name in ALIAS_NAMES:
            raw = values[name]
            if not isinstance(raw, str) or not os.path.isabs(raw):
                raise SamplerError(f"别名 {name} 必须是绝对路径：{raw!r}")
            normal = os.path.normpath(raw)
            if normal == "/":
                raise SamplerError(f"别名 {name} 不能是根目录")
            self.values[name] = normal
            for variant in {normal, os.path.realpath(normal)}:
                pairs.append((variant, name))
        pairs.sort(key=lambda pair: (-len(pair[0]), pair[1]))
        self._pairs = pairs
        # 文本替换规则（诊断样例用）：与 apply 同样最长前缀优先，每条规则先做一次子串快速判断，没出现就不跑正则。
        self._text_rules = [(prefix, re.compile(_TEXT_BEFORE + re.escape(prefix) + _TEXT_AFTER), "{" + name + "}") for prefix, name in pairs]

    def apply(self, path: str) -> str:
        for prefix, name in self._pairs:
            if path == prefix:
                return "{" + name + "}"
            if path.startswith(prefix + "/"):
                return "{" + name + "}" + path[len(prefix):]
        return path

    def apply_text(self, text: str) -> str:
        """一段文本（例如一行 strace 输出）里出现的采样根绝对路径换成别名。

        诊断样例会随证据提交进仓库、跨工作目录比对，所以和证据里的路径一样不能带采样时的具体绝对路径。整段前缀才替换：
        ``/var/tmp/x/test-tree`` 不会误伤 ``/var/tmp/x/test-tree-other``，也不会命中 ``/y/var/tmp/x/test-tree`` 的尾部。
        """
        for prefix, pattern, replacement in self._text_rules:
            if prefix in text:
                text = pattern.sub(replacement, text)
        return text


def ancestor_keys(aliases: Mapping[str, str]) -> set[str]:
    """采样根（TREE、WORK、HOME、TMP）的祖先链，在 ``outside_accesses`` 聚合键空间里的键：每一级目录 ``D`` 的 ``D/``（它的直接子项的访问），
    别名根另有不带斜杠的自身键（``{WORK}``）。TREE 的子项是仓库内路径，``{TREE}/`` 这个键不会在仓库外出现，多收了也无害。"""
    table = AliasTable(aliases)
    keys: set[str] = set()
    for root in table.values.values():
        for variant in {root, os.path.realpath(root)}:
            current = variant
            while True:
                aliased = table.apply(current)
                keys.add(aliased if aliased == "/" else aliased + "/")
                if aliased in ALIAS_ROOTS:
                    keys.add(aliased)
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
    return keys


def parse_aliases(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or name not in ALIAS_NAMES or name in values:
            raise SamplerError(f"--alias 必须是 NAME=绝对路径，NAME 取 {'、'.join(ALIAS_NAMES)} 且不重复：{item!r}")
        values[name] = value
    missing = [name for name in ALIAS_NAMES if name not in values]
    if missing:
        raise SamplerError(f"缺少别名：{'、'.join(missing)}")
    return values


def _volatile(path: str) -> str:
    """进程号和线程号每次都不同：/proc/3106409/cgroup 与 /proc/self/cgroup 是同一件事，规范成 self，免得目录键无限增长。"""
    if path.startswith("/proc/"):
        path = _PROC_PID.sub("/proc/self", path, count=1)
        path = _PROC_TASK.sub(r"\1/{TID}", path, count=1)
    return path


# ---------------------------------------------------------------------------
# strace 行的旁路解析：connect 与 execve
# ---------------------------------------------------------------------------


def _unescape(text: str) -> str:
    """strace 字符串字面量还原（反斜杠转义、八进制、十六进制）。

    自带一份而不用 read_audit.unescape：与审计器的耦合面只留 TraceParser 一处，审计器改名或改语义不会静默改变这里的结果。
    """
    if "\\" not in text:
        return text
    out = bytearray()
    index = 0
    simple = {"n": 10, "t": 9, "r": 13, "v": 11, "f": 12, "a": 7, "b": 8, "\\": 92, '"': 34, "'": 39}
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.extend(char.encode("utf-8", "surrogateescape"))
            index += 1
            continue
        nxt = text[index + 1]
        if nxt in simple:
            out.append(simple[nxt])
            index += 2
        elif nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[index + 2:index + 4] or ""):
            out.append(int(text[index + 2:index + 4], 16))
            index += 4
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", text[index + 1:]).group(0)  # type: ignore[union-attr]
            out.append(int(digits, 8) & 0xFF)
            index += 1 + len(digits)
        else:
            out.extend(nxt.encode("utf-8", "surrogateescape"))
            index += 2
    return out.decode("utf-8", "surrogateescape")


def _parse_string_array(text: str, start: int) -> list[str] | None:
    """从 text[start] 的 [ 起解析 strace 打印的字符串数组；格式不符（例如被打印成 0x7ffd... 指针）返回 None。"""
    if start >= len(text) or text[start] != "[":
        return None
    index, items = start + 1, []
    while index < len(text):
        while index < len(text) and text[index] == " ":
            index += 1
        if index >= len(text):
            return None
        if text[index] == "]":
            return items
        if text[index] == '"':
            token = _STRING.match(text, index)
            if token is None:
                return None
            items.append(_unescape(token.group(1)))
            index = token.end()
            if text.startswith("...", index):  # 字符串被 -s 截断
                index += 3
        elif text.startswith("...", index):  # 数组元素被省略
            items.append("...")
            index += 3
        else:
            return None
        while index < len(text) and text[index] == " ":
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
    return None


class ExecLineUnparsed(ValueError):
    """行首是 execve／execveat 调用，但认不出它的形态（路径参数不是字符串、execveat 的 dirfd 或路径解析不了，或 AT_EMPTY_PATH 而 dirfd 没有路径注解）：
    无法判断被执行的是不是 git。采样器对此失败关闭——``StreamAggregator`` 记一个 ``sampler:exec-unparsed`` 未解析原因，采样随之无效，而不是悄悄漏掉。"""


def parse_git_execve(line: str) -> tuple[str, ...] | None:
    """execve／execveat 行是 git 命令时返回完整 argv；argv 不可见但可执行文件名是 git 时返回哨兵；不是 git 返回 None；不是 exec 调用行也返回 None。

    判定“是 git 命令”：argv[0] 的 basename 是 git，**或**被执行的可执行文件路径的 basename 是 git（后者抓别名调用：
    ``execve("/usr/bin/git", ["gitx", …])`` 里 argv[0] 是 gitx，可执行文件却是 git；返回的 argv 原样保留 argv[0]，``git_usage_problem`` 与登记侧
    的 C6 看到 argv[0] 不是 git 就按“不是 git 命令行，无法判定”处理，同样失败关闭）。可执行文件路径：execve 取路径参数；execveat 取路径参数，为空串
    （AT_EMPTY_PATH）时取 dirfd 注解里的路径（``execveat(3</usr/bin/git>, "", …, AT_EMPTY_PATH)``）。

    行首是 exec 调用却认不出形态——路径参数不是字符串、execveat 的参数解析不了、AT_EMPTY_PATH 而 dirfd 没有绝对路径注解且 argv[0] 又不是 git——
    抛 ``ExecLineUnparsed``（失败关闭，不悄悄漏掉）。"""
    start = _EXEC_START.match(line)
    if start is None:
        return None
    unresolved = ExecLineUnparsed(line[:120])
    if start.group(1) == "execve":
        match = _EXECVE_CALL.match(line)
        if match is None:
            raise unresolved
        executable: str | None = _unescape(match.group(1))
        argv_start = match.end()
    else:
        match = _EXECVEAT_CALL.match(line)
        if match is None:
            raise unresolved
        executable = _unescape(match.group(2))
        if executable == "":  # AT_EMPTY_PATH：可执行文件就是 dirfd 指向的文件，路径在 dirfd 的 <…> 注解里（AT_FDCWD 的注解是当前目录，不是可执行文件）
            annotation = _DIRFD_ANNOTATION.fullmatch(match.group(1))
            path = _unescape(annotation.group(1)) if annotation and not match.group(1).startswith("AT_FDCWD") else ""
            executable = path if path.startswith("/") else None
        argv_start = match.end()
    argv = _parse_string_array(line, argv_start)
    if argv:
        if os.path.basename(argv[0]) == "git" or (executable is not None and os.path.basename(executable) == "git"):
            return tuple(argv)
        if executable is None:  # argv[0] 不是 git，而可执行文件认不出来：无法排除它其实是 git
            raise unresolved
        return None
    if executable is None:
        raise unresolved
    if os.path.basename(executable) == "git":
        return ("git", GIT_ARGV_HIDDEN)
    return None


def parse_connect(line: str) -> tuple[str, str] | None:
    """connect 行的 (地址族, 目的地)；不是 connect 行返回 None。

    目的地：AF_INET 为 ``地址:端口``，AF_INET6 为 ``[地址]:端口``（地址取自 ``inet_pton(AF_INET6, "…", &sin6_addr)``），
    AF_UNIX 为路径，抽象套接字统一记成 ``@名字``（strace 6.x 写 ``@"名字"``，旧写法是以 NUL 开头的 ``"\\0名字"``）。
    sockaddr 不是可解码结构（仍是不透明指针）或路径被 ``-s`` 截断时记 ``unparsed``——目的地不可判，宁可让 merge 因无法证明是
    本机连接而判无效，也不能当作没有网络。只看调用里的地址，不看返回值：非阻塞 connect 连不可达的外部地址同样返回
    EINPROGRESS，那也是一次外连尝试。
    """
    call = _CONNECT_CALL.match(line)
    if call is None:
        return None
    body = call.group(1)
    unix = _SOCK_UNIX.search(body)
    if unix:
        if unix.group(3):  # 路径被截断，目的地不完整
            return ("unparsed", body[:120])
        path = _unescape(unix.group(2))
        if unix.group(1) == "@":
            return ("AF_UNIX", "@" + path)
        return ("AF_UNIX", "@" + path[1:] if path.startswith("\x00") else path)
    inet = _SOCK_INET.search(body)
    if inet:
        return ("AF_INET", f"{inet.group(2)}:{inet.group(1)}")
    inet6 = _SOCK_INET6.search(body)
    if inet6:
        return ("AF_INET6", f"[{inet6.group(2)}]:{inet6.group(1)}")
    family = _SOCK_FAMILY.search(body)
    if family and family.group(1) == "AF_UNSPEC":
        return ("AF_UNSPEC", "")  # UDP 套接字断开关联，不是外连
    if family:
        return (family.group(1), "")
    return ("unparsed", body[:120])


def side_effect_call(line: str) -> bool:
    """一条被丢弃的垃圾行（或它的样例文本）的调用名是否属于“副作用类”（``SIDE_EFFECT_CALLS``；open／openat／openat2 要在参数里带
    O_WRONLY、O_RDWR、O_CREAT、O_TRUNC 才算）。名字解析不出来（不是“<进程号> <名字>(…”的形状）或是随机名（``???``、``syscall_0x…``）的不算。
    filter 用它给垃圾行分类计数，merge 用它给样例排序（副作用类优先留样）；纯函数，只看文本。"""
    found = _GARBAGE_CALL.match(line)
    if found is None:
        return False
    name = found.group(1)
    return name in SIDE_EFFECT_CALLS or (name in _OPEN_CALLS and _WRITE_OPEN_FLAGS.search(line) is not None)


# ---------------------------------------------------------------------------
# 流式聚合（filter 的核心，将来正式执行自带采样时直接复用）
# ---------------------------------------------------------------------------


class StreamAggregator:
    """逐行喂 strace 输出，流式得到 ``declared-sample-raw/v1``。

    内存模型：TraceParser 对每个不同路径存两份东西——accesses 里的 kinds 集合和 samples 里的一条样例行（最长 300 字符）。
    300 万行、180 万个不同路径的合成流里，仅 samples 就要近 1 GB。本类每 chunk_lines 行把 accesses 并入自己的紧凑聚合（路径
    到位掩码），然后清空 TraceParser 的 accesses 与 samples。TraceParser 的跨行状态（cwd、pending、deferred）都在别的属性里，
    清空这两个纯累积字典不会影响解析结果；T-API 测试钉住这一点。chunk_lines 为 0 表示只在结束时排空（等价于不裁剪，压力脚本
    用它做对照）。
    """

    def __init__(self, aliases: Mapping[str, str], *, unit_id: str, chunk_lines: int = DEFAULT_CHUNK_LINES) -> None:
        if chunk_lines < 0:
            raise SamplerError("chunk_lines 不能为负")
        self.alias_table = AliasTable(aliases)
        self.directory_paths = self._directory_path_set()
        self.unit_id = unit_id
        self.chunk_lines = chunk_lines
        self.parser = read_audit_module().TraceParser(strict=True)
        self.lines = 0
        self.markers = 0
        self.repo: dict[str, int] = {}
        self.outside: dict[str, list[int]] = {}
        self.git_commands: dict[tuple[str, ...], None] = {}
        self.network: dict[tuple[str, str], None] = {}
        # 进程号 -> 正在进行中的 connect（start 行已见、resumed 行未见）；用来给 connect 的 unfinished 与 resumed 成对。
        self.connect_pending: dict[str, tuple[str, str]] = {}
        # 同上，保留 start 行原文：流结束时仍悬着的 connect 要留诊断样例，只有目的地不够。
        self.connect_pending_lines: dict[str, str] = {}
        self.sampler_reasons: dict[str, int] = {}
        # 未解析原因的原始行样例 {原因: [别名化、截断后的行]}。只收不在容忍集合内的原因（容忍的悬着调用另有 unfinished_samples）；
        # 每个原因最多 MAX_SAMPLES_PER_REASON 条、最多 MAX_SAMPLE_REASONS 个原因，先到先得。
        self.unresolved_samples: dict[str, list[str]] = {}
        # 被当作垃圾行丢弃的带名字的 strace 行（形态 A、B）的原样样例（别名化、截断、去重）。两个桶各自最多 MAX_DETACHED_SAMPLES 条，输出时副作用类在前、
        # 其它在后，合起来最多 MAX_DETACHED_SAMPLES 条——副作用类优先留样（见 detached_samples）。
        self._samples_side: list[str] = []
        self._samples_other: list[str] = []
        # 被丢弃的垃圾行里名字属于副作用类（side_effect_call）的条数：只给评审看，不影响有效性判定；输出在 raw 顶层 detached_side_effects（有才写）。
        self.side_effect_markers = 0
        # 被丢弃的、带进程号（tid）前缀的垃圾行：tid -> 别名化的垃圾行原文。垃圾行描述的是“中途消失的线程”，它的 tid 之后不该再出现任何行；
        # feed 里每行先问一句“这个 tid 是不是在这里面”，在就挪到 _not_terminal（tid -> (垃圾行, 之后第一次出现它的行)），finish 时转成未解析原因。
        # 两张表合起来最多 MAX_DETACHED_TIDS 项，内存随不同 tid 数增长、与总行数无关。
        self._garbage_tids: dict[str, str] = {}
        self._not_terminal: dict[str, tuple[str, str]] = {}
        # 为发现“这一行引入了新的未解析原因”而保留的解析器计数快照：每行只比较一次整数（parser.unresolved），不逐行求和字典。
        self._seen_unresolved = 0
        self._seen_reasons: dict[str, int] = {}
        self._tolerated: dict[str, bool] = {}  # 原因 -> 是否在容忍集合内（缓存，原因种类很少）
        self._since_drain = 0
        self._full_keys = 0

    # -- 输入 -----------------------------------------------------------------
    def feed(self, raw: str) -> None:
        self.lines += 1
        line = raw.rstrip("\n")
        if self._garbage_tids:  # 见过垃圾行之后才付这个代价：这一行的 tid 若在“垃圾行的 tid”表里，那个线程其实没有消失（见 _seen_again）
            self._seen_again(line)
        if "???(" in line and _MARKER.match(line):
            self.markers += 1
            self._note_garbage_tid(line)
            return
        # 带名字的垃圾行（形态 A、B，见 _GARBAGE_CALL）也在这里丢弃并计数，不交给 TraceParser，也不走 connect／execve 旁路：
        # 垃圾行的名字是随机的，哪怕恰好叫 connect 或 execve，参数也是残留寄存器，不是真实的目的地或 argv。
        end = line.rstrip()
        if (end.endswith(_DETACHED_END) or end.endswith(_LOST_RESULT_END)) and self._garbled_marker(end):
            return
        if (" connect(" in line or line.startswith("connect(") or "<... connect resumed>" in line) and self._side_connect(line):
            return
        if "execve" in line:  # execve／execveat 调用行（_side_execve 自己按行首精确匹配，其它碰巧含这个词的行直接略过）
            self._side_execve(line)
        parser = self.parser
        # resumed 行本身不带调用的起点（路径、参数都在 unfinished 那一行）。解析器处理 resumed 行时会把 pending 里对应的起始部分
        # 取走，所以要在 feed 之前先把它记下来（只是取一个引用，不拼字符串），这一行若引入未解析原因，样例才带得上路径。
        # 没有悬着的调用时整段跳过；真正拼接留到 _note_unresolved，常规行不付这个代价。
        started = parser.pending.get(line.partition(" ")[0]) if parser.pending and "resumed>" in line else None
        parser.feed(raw)
        if parser.unresolved != self._seen_unresolved:  # 常规路径上这是每行唯一多出来的开销：一次整数比较
            self._note_unresolved(line, started)
        self._since_drain += 1
        if self.chunk_lines and self._since_drain >= self.chunk_lines:
            self._drain()

    def _directory_path_set(self) -> frozenset[str]:
        """按构造就是目录的路径（别名化后的写法）：全部别名根（TREE、WORK、HOME、TMP）自身及其各级祖先目录（``/``、``/var``、``/var/tmp``……）。

        仓库外的访问按目录聚合，丢了文件名：祖先目录 ``/var/tmp`` 被不带 O_DIRECTORY 地只读打开（Go 的 ``os.Open(目录)`` 就是这样），和读
        ``/var/`` 下的一个文件，在聚合键 ``/var/`` 上都是 ``read``，没法区分。这些路径下面有别的别名根，必然是目录，读它只能是“打开目录”，
        读不出任何文件内容——``_absorb`` 据此把它们的 ``read`` 记成 ``dir``。于是祖先链目录的聚合键上剩下的 ``read``，一定是读了祖先目录下
        别的直接子项（文件，或不是祖先的目录），登记侧可以放心地不再放行它。"""
        paths: set[str] = set()
        for root in self.alias_table.values.values():
            for variant in {root, os.path.realpath(root)}:
                current = variant
                while True:
                    paths.add(self.alias_table.apply(current))
                    parent = os.path.dirname(current)
                    if parent == current:
                        break
                    current = parent
        return frozenset(paths)

    def _garbled_marker(self, end: str) -> bool:
        """``end`` 是去掉行尾空白的一行，且以 <detached ...> 或 “ = ?” 结尾：是带名字的垃圾行（形态 A、B）就丢弃、计入 detached_markers
        并留样例，返回 True；否则（不是“<进程号> <名字>(…”的形状，或 “= ?” 结尾而名字本来就可以不返回）返回 False，照常处理。"""
        found = _GARBAGE_CALL.match(end)
        if found is None:
            return False
        if not end.endswith(_DETACHED_END) and found.group(1) in _MAY_END_WITHOUT_RESULT:
            return False
        self.markers += 1
        side_effect = side_effect_call(end)
        if side_effect:
            self.side_effect_markers += 1
        bucket = self._samples_side if side_effect else self._samples_other
        if len(bucket) < MAX_DETACHED_SAMPLES:
            sample = self._shorten(end)
            if sample not in self._samples_side and sample not in self._samples_other:
                bucket.append(sample)
        self._note_garbage_tid(end)
        return True

    @property
    def detached_samples(self) -> list[str]:
        """输出的垃圾行样例：副作用类在前、其它在后（各自按到达顺序），合起来最多 MAX_DETACHED_SAMPLES 条——副作用类优先留样，没有副作用类的行时与从前逐字相同。"""
        return (self._samples_side + self._samples_other)[:MAX_DETACHED_SAMPLES]

    def _note_garbage_tid(self, line: str) -> None:
        """记下一条被丢弃的垃圾行的 tid（行首的进程号），供之后核对“它是不是真的再没有出现”。已判定不是终结的 tid 不再重复登记（同一个 tid 只记一次）；
        追踪表满了（MAX_DETACHED_TIDS）就记 sampler:limit:detached-tids——这一条没法核对，失败关闭。"""
        found = _PID.match(line)
        if found is None:
            return
        tid = found.group(1)
        if tid in self._not_terminal:
            return
        if tid not in self._garbage_tids and len(self._garbage_tids) + len(self._not_terminal) >= MAX_DETACHED_TIDS:
            self._limit("limit:detached-tids", line)
            return
        self._garbage_tids[tid] = self._shorten(line)

    def _seen_again(self, line: str) -> None:
        """这一行的 tid 若是某条垃圾行的 tid：那个线程不是“中途消失”（消失的线程之后不会再有任何行），丢弃的那一行可能是真实调用，挪进 _not_terminal，
        finish 时记 ``sampler:detached-not-terminal:<tid>``（同一个 tid 只记一次）。垃圾行自己再来一条同 tid 的垃圾行也算（一个线程不会消失两次）。"""
        found = _PID.match(line)
        if found is None:
            return
        garbage = self._garbage_tids.pop(found.group(1), None)
        if garbage is not None:
            self._not_terminal[found.group(1)] = (garbage, self._shorten(line))

    def _limit(self, reason: str, line: str | None = None) -> None:
        key = "sampler:" + reason
        self.sampler_reasons[key] = self.sampler_reasons.get(key, 0) + 1
        if line is not None and self._wants_sample(key):
            self._keep_sample(key, self._shorten(line))

    # -- 诊断样例 ---------------------------------------------------------------
    def _shorten(self, text: str) -> str:
        """样例行：采样根的绝对路径换成别名，再截到 SAMPLE_LINE_CHARS 个字符。"""
        return self.alias_table.apply_text(text)[:SAMPLE_LINE_CHARS]

    def _wants_sample(self, reason: str) -> bool:
        bucket = self.unresolved_samples.get(reason)
        if bucket is None:
            return len(self.unresolved_samples) < MAX_SAMPLE_REASONS
        return len(bucket) < MAX_SAMPLES_PER_REASON

    def _keep_sample(self, reason: str, text: str) -> None:
        if self._wants_sample(reason):
            self.unresolved_samples.setdefault(reason, []).append(text)

    @staticmethod
    def _join_resumed(line: str, started: str | None) -> str:
        """resumed 行与它对应的起始部分（TraceParser.pending 里记的，不含 ``<unfinished ...>``）拼成完整的调用文本，拼法与解析器
        自己一致：``进程号 起始部分 + resumed 标记之后的尾巴``。拼不上（没有起始部分、不是 resumed 行）就原样返回这一行。"""
        resumed = _RESUMED_LINE.match(line)
        if started is None or resumed is None:
            return line
        return f"{resumed.group(1)} {started}{line[resumed.end():]}"

    def _is_tolerated(self, reason: str) -> bool:
        cached = self._tolerated.get(reason)
        if cached is None:
            cached = self._tolerated[reason] = reason_tolerated(reason)
        return cached

    def _note_unresolved(self, line: str, started: str | None) -> None:
        """这一行让解析器的未解析计数增加了：找出新增的原因，容忍集合之外的各留一条样例。

        只在计数变化的行上进来（常规行一次整数比较就绕过去）。pnpm 的 node_modules 下容忍的 symlink-metadata-not-bound 能占到
        一成的行，所以先看是不是容忍的原因（结果缓存），容忍的只更新快照；真正要留样例的原因满额之后也不再算别名替换。
        """
        parser = self.parser
        seen = self._seen_reasons
        sample = None
        for reason, count in parser.unresolved_reasons.items():
            if count == seen.get(reason, 0):
                continue
            seen[reason] = count
            if self._is_tolerated(reason) or not self._wants_sample(reason):
                continue
            if sample is None:
                sample = self._shorten(self._join_resumed(line, started) if started is not None else line)
            self._keep_sample(reason, sample)
        self._seen_unresolved = parser.unresolved

    def _side_connect(self, line: str) -> bool:
        """connect 的 start 行与 resumed 行都在这里截走，一律不交给 TraceParser：它对未建模的 connect 记 unsupported-call，
        对没有起始行的 resumed 记 resume-without-start，都会让采样白白变无效。

        多线程下 connect 会被拆成 ``connect(… <unfinished ...>`` 和 ``<... connect resumed>) = 返回值`` 两行，中间可以夹着别的
        进程的行。目的地在 start 行里（完整调用也一样），所以在 start 行收集；这里按进程号把 start 与 resumed 配对：
        resumed 找不到对应的 start、同一进程又来一个 unfinished、流结束时仍有 start 没等到 resumed，都意味着有一次 connect 的
        目的地我们没看全，各记一个未解析原因，采样随之无效——和 TraceParser 对别的调用要求配对是同一个道理。
        返回值不参与判定：只看地址，不看返回值（非阻塞 connect 连不可达的外部地址同样是 EINPROGRESS，仍是一次外连尝试）。
        """
        match = _PID.match(line)
        pid = match.group(1) if match else "0"
        if _CONNECT_RESUMED.match(line):
            self.connect_pending_lines.pop(pid, None)
            if self.connect_pending.pop(pid, None) is None:
                self._limit("connect-resume-without-start", line)
            return True
        target = parse_connect(line)
        if target is None:
            return False
        if line.rstrip().endswith("<unfinished ...>"):
            if pid in self.connect_pending:
                self._limit("connect-overlapping-unfinished", line)
            self.connect_pending[pid] = target
            self.connect_pending_lines[pid] = line
        if target not in self.network:
            if len(self.network) >= MAX_NETWORK_TARGETS:
                self._limit("limit:network-targets", line)
            else:
                self.network[target] = None
        return True

    def _side_execve(self, line: str) -> None:
        """execve／execveat 行同时交给 TraceParser（它要记 exec 路径），这里另外收集 git 子进程的 argv，用于确定读了哪些历史提交。

        行首是 exec 调用却认不出形态（见 ``ExecLineUnparsed``）：记 ``sampler:exec-unparsed`` 未解析原因并留样例，采样随之无效——失败关闭，不悄悄漏掉。"""
        try:
            argv = parse_git_execve(line)
        except ExecLineUnparsed:
            self._limit("exec-unparsed", line)
            return
        if argv is not None and argv not in self.git_commands:
            if len(self.git_commands) >= MAX_GIT_COMMANDS:
                self._limit("limit:git-commands", line)
            else:
                self.git_commands[argv] = None

    # -- 排空与聚合 -------------------------------------------------------------
    def _drain(self) -> None:
        accesses = self.parser.accesses
        for path, kinds in accesses.items():
            self._absorb(path, kinds)
        accesses.clear()
        samples = getattr(self.parser, "samples", None)
        if isinstance(samples, dict):
            samples.clear()
        self._since_drain = 0

    def _absorb(self, path: str, kinds: Iterable[str]) -> None:
        mask = 0
        for kind in kinds:
            bit = KIND_BITS.get(kind)
            if bit is None:
                self._limit("unknown-kind:" + str(kind))
            else:
                mask |= bit
        if not mask:
            return
        aliased = self.alias_table.apply(_volatile(path))
        if aliased == "{TREE}" or aliased.startswith("{TREE}/"):
            self._absorb_repo(aliased, mask)
        else:
            if mask & _READ and aliased in self.directory_paths:
                mask = (mask & ~_READ) | _DIR  # 目录不可能被“读”出文件内容：祖先链目录与别名根的 read 只是打开目录，记成 dir（见 _directory_path_set）
            self._absorb_outside(aliased, mask)

    def _absorb_repo(self, aliased: str, mask: int) -> None:
        # git 对象库里是成千上万个内容寻址文件：逐个保留没有意义，聚合到目录级（其余 .git 内路径仍逐个保留，数量很少）。
        key = "{TREE}/.git/objects/" if aliased == "{TREE}/.git/objects" or aliased.startswith("{TREE}/.git/objects/") else aliased
        current = self.repo.get(key)
        if current is not None:
            self.repo[key] = current | mask
        elif len(self.repo) >= MAX_REPO_PATHS:
            self._limit("limit:repo-paths")
        else:
            self.repo[key] = mask

    def _absorb_outside(self, aliased: str, mask: int) -> None:
        """仓库外：按目录聚合。目录自身被枚举（list）算在该目录的键下，其余访问算在父目录的键下；完整保留前缀下按精确路径。"""
        listed, rest = mask & _LIST, mask & ~_LIST
        full = aliased.startswith(OUTSIDE_FULL_PREFIXES) or aliased in OUTSIDE_FULL_EXACT
        if full and self._full_keys < OUTSIDE_FULL_LIMIT:
            if rest:
                self._bump(aliased, rest, full=True)
            if listed:
                self._bump(aliased.rstrip("/") + "/", listed, full=True)
            return
        if rest:
            parent = aliased if aliased in ALIAS_ROOTS else aliased.rsplit("/", 1)[0] + "/"
            self._bump(self._fold(parent), rest)
        if listed:
            self._bump(self._fold(aliased.rstrip("/") + "/"), listed)

    @staticmethod
    def _fold(key: str) -> str:
        for prefix in OUTSIDE_FOLD_PREFIXES:
            if key.startswith(prefix):
                return prefix
        return key

    def _bump(self, key: str, mask: int, *, full: bool = False) -> None:
        entry = self.outside.get(key)
        if entry is None:
            if len(self.outside) >= OUTSIDE_DIR_LIMIT:
                # 目录键数失控：折叠到最近的已有祖先，保住内存，同时让采样判无效（目录基数爆炸说明分类前缀需要补充）。
                self._limit("limit:outside-directories")
                while key not in self.outside and key != "/":
                    key = key.rstrip("/").rsplit("/", 1)[0] + "/"
                entry = self.outside.setdefault(key, [0, 0])
            else:
                entry = self.outside[key] = [0, 0]
                if full:
                    self._full_keys += 1
        entry[0] |= mask
        # 计数是各块内去重后的路径出现次数之和，只作规模提示，不参与覆盖判定。
        entry[1] += 1

    # -- 输出 -----------------------------------------------------------------
    def finish(self) -> dict[str, Any]:
        """结束流，返回 raw 文档（run 为 null，由 run 子命令补全）。"""
        self._drain()
        parser = self.parser
        reasons: dict[str, int] = dict(parser.unresolved_reasons)
        # 把 EOF 时仍悬着的调用也写成显式原因，保持“unresolved 等于各原因计数之和”，merge 不必再单独推断。
        if parser.pending:
            reasons["unfinished-calls-at-eof"] = len(parser.pending)
        if parser.deferred_count:
            reasons["deferred-process-events-at-eof"] = parser.deferred_count
        if self.connect_pending:
            reasons["sampler:connect-unfinished-at-eof"] = len(self.connect_pending)
        for reason, count in self.sampler_reasons.items():
            reasons[reason] = reasons.get(reason, 0) + count
        # 被丢弃的垃圾行描述的是“中途消失的线程”，它的 tid 之后不该再出现任何行；出现了就说明那一行不是垃圾（线程没有消失），丢弃它可能漏掉一次真实的调用，
        # 记 sampler:detached-not-terminal:<tid>（同一个 tid 只记一次）。不同的原因键最多 MAX_SAMPLE_REASONS 个，超出的合并成 …:more，计数是超出的 tid 数——
        # 键数有界（不随 tid 数膨胀），总计数仍是 tid 数，保持“unresolved 等于各原因计数之和”。这些原因都不在容忍集合内，采样随之无效／证据不完整。
        not_terminal = list(self._not_terminal.items())
        individual, overflow = not_terminal[:MAX_SAMPLE_REASONS], not_terminal[MAX_SAMPLE_REASONS:]
        for tid, _evidence in individual:
            key = "sampler:detached-not-terminal:" + tid
            reasons[key] = reasons.get(key, 0) + 1
        if overflow:
            reasons["sampler:detached-not-terminal:more"] = reasons.get("sampler:detached-not-terminal:more", 0) + len(overflow)
        # 流结束时仍悬着的东西也各留诊断样例。TraceParser.pending 是“进程号 → 起始部分（不含 <unfinished ...> 标记）”，拼回原行；
        # 容忍的 unfinished-calls-at-eof 放进 unfinished_samples（最多 8 条，给评审看这些悬着的是什么调用），不占未解析原因的样例额度。
        unfinished_samples = [self._shorten(f"{pid} {text}{_UNFINISHED_MARK}")
                              for pid, text in list(parser.pending.items())[:MAX_UNFINISHED_SAMPLES]]
        deferred = getattr(parser, "deferred", None)  # 属性不是审计器承诺的公共面，没有就不取样
        if parser.deferred_count and isinstance(deferred, dict):
            events = [event for pending_events in deferred.values() for event in pending_events[:MAX_SAMPLES_PER_REASON]]
            for event in events[:MAX_SAMPLES_PER_REASON]:
                self._keep_sample("deferred-process-events-at-eof", self._shorten(event))
        for pid in list(self.connect_pending)[:MAX_SAMPLES_PER_REASON]:
            self._keep_sample("sampler:connect-unfinished-at-eof", self._shorten(self.connect_pending_lines.get(pid, "")))
        for tid, evidence in individual:  # 每个 tid 两条样例：被丢弃的垃圾行，和它之后第一次再出现的那一行
            for text in evidence:
                self._keep_sample("sampler:detached-not-terminal:" + tid, text)
        if overflow:
            for text in overflow[0][1]:
                self._keep_sample("sampler:detached-not-terminal:more", text)
        coverage: dict[str, Any] = {
            "stream_complete": True, "unresolved": sum(reasons.values()), "unresolved_reasons": dict(sorted(reasons.items())),
            "unfinished_calls": len(parser.pending) + len(self.connect_pending), "unresolved_process_events": parser.deferred_count,
            "lines": self.lines, "detached_markers": self.markers}
        # 两个样例键是可选的：没有要留的样例时不写，输出与没有这项功能时逐字相同。
        if self.unresolved_samples:
            coverage["unresolved_samples"] = {reason: list(self.unresolved_samples[reason]) for reason in sorted(self.unresolved_samples)}
        if unfinished_samples:
            coverage["unfinished_samples"] = unfinished_samples
        document: dict[str, Any] = {
            "schema_version": RAW_SCHEMA,
            "unit_id": self.unit_id,
            "aliases": dict(self.alias_table.values),
            "chunk_lines": self.chunk_lines,
            "lines": self.lines,
            "coverage": coverage,
            "repo_accesses": [[path, kinds_of(self.repo[path])] for path in sorted(self.repo)],
            "outside_accesses": [[key, kinds_of(self.outside[key][0]), self.outside[key][1]] for key in sorted(self.outside)],
            "git_commands": [list(argv) for argv in self.git_commands],
            "network": [{"family": family, "target": target} for family, target in sorted(self.network)],
            "run": None,
        }
        # 顶层可选键：被当作垃圾行丢弃的带名字的 strace 行的原样样例（副作用类在前、最多 8 条），和其中名字属于副作用类的行数。放在 raw 顶层而不是
        # coverage 里——登记侧对 sample.coverage 的键集合是闭合的，只认 detached_markers 与 unfinished_samples；merge 后进 review（登记侧不解释 review）。
        # 没有这样的行时都不写，输出与没有这项功能时逐字相同。
        samples = self.detached_samples
        if samples:
            document["detached_samples"] = samples
        if self.side_effect_markers:
            document["detached_side_effects"] = self.side_effect_markers
        return document


def filter_stream(lines: Iterable[str], aliases: Mapping[str, str], *, unit_id: str,
                  chunk_lines: int = DEFAULT_CHUNK_LINES) -> dict[str, Any]:
    aggregator = StreamAggregator(aliases, unit_id=unit_id, chunk_lines=chunk_lines)
    for line in lines:
        aggregator.feed(line)
    return aggregator.finish()


# ---------------------------------------------------------------------------
# 文档校验（字段闭合）
# ---------------------------------------------------------------------------

COVERAGE_KEYS = frozenset({"stream_complete", "unresolved", "unresolved_reasons", "unfinished_calls", "unresolved_process_events",
                           "lines", "detached_markers"})
# 可选键：旧证据文件没有它们，缺省按“没有样例”处理（校验不得因为旧文件缺这些键而失败）。raw 的 coverage 可带
# unresolved_samples（{原因: [原始行样例]}）与 unfinished_samples（容忍的悬着调用的起始行）；sample 的 coverage 只带
# unfinished_samples——登记侧对 sample.coverage 的键集合是闭合的，只认识 unfinished_samples，所以按原因归类的样例放进 review
# （登记侧不解释 review），不放进 sample.coverage。
RAW_COVERAGE_OPTIONAL_KEYS = frozenset({"unresolved_samples", "unfinished_samples"})
SAMPLE_COVERAGE_OPTIONAL_KEYS = frozenset({"unfinished_samples"})
RAW_KEYS = frozenset({"schema_version", "unit_id", "aliases", "chunk_lines", "lines", "coverage", "repo_accesses", "outside_accesses",
                      "git_commands", "network", "run"})
# raw 顶层的可选键（旧 raw 没有，缺省为空）：detached_samples 是被当作垃圾行丢弃的带名字的 strace 行的原样样例（副作用类在前）；
# detached_side_effects 是被丢弃的垃圾行里名字属于副作用类的条数（评审提示，不影响有效性判定）。
RAW_OPTIONAL_KEYS = frozenset({"detached_samples", "detached_side_effects"})
COMMAND_KEYS = frozenset({"launcher", "argv", "cwd_rel", "sha256"})
RUN_KEYS = frozenset({"label", "cache", "exit_code", "signal", "timed_out", "seconds", "started_utc", "finished_utc", "command",
                      "git_status_before", "git_status_after", "commit", "kernel", "strace", "sampler_sha256", "env_overrides",
                      "docker", "filter_wait_seconds"})
# no_go_cache：run 带了 --no-go-cache（只在打开时才写这个键；缺省＝没打开）。
RUN_OPTIONAL_KEYS = frozenset({"no_go_cache"})
SAMPLE_KEYS = frozenset({"schema_version", "unit_id", "command", "sampled", "aliases", "coverage", "repo_accesses", "outside_accesses",
                         "git_commands", "network", "exit_code", "valid", "invalid_reasons", "review"})
SAMPLED_KEYS = frozenset({"commit", "utc", "kernel", "strace", "sampler_sha256", "runs"})
SAMPLED_RUN_KEYS = frozenset({"cache", "exit_code", "lines", "seconds"})
REVIEW_KEYS = frozenset({"runs", "git_status", "untracked_reads", "writes", "network", "run_differences", "docker", "allowances",
                         "git_argv_hidden"})
REVIEW_OPTIONAL_KEYS = frozenset({"unresolved_samples", "detached_samples", "detached_side_effects"})
# review.allowances 里新增的两类登记项（没登记时整个键不出现）。每一行的键集合是闭合的。
INSTALLED_ROW_KEYS = frozenset({"prefix", "contract", "reads", "sample", "unused"})
TRANSIENT_ROW_KEYS = frozenset({"pattern", "paths", "read_back", "sample", "unused"})
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_access_rows(problems: list[str], name: str, rows: Any, shape: str) -> None:
    """shape：raw-repo 是 [路径, kinds]，sample-repo 是 [路径, kinds, tracked]，outside 是 [目录或路径, kinds, 计数]。"""
    if not isinstance(rows, list):
        problems.append(f"{name} 必须是列表")
        return
    for row in rows:
        if not isinstance(row, list) or not row or not isinstance(row[0], str):
            problems.append(f"{name} 的条目格式非法：{str(row)[:80]}")
            return
        kinds = row[1] if len(row) > 1 else None
        if not isinstance(kinds, list) or not kinds or any(kind not in KIND_BITS for kind in kinds):
            problems.append(f"{name} 的读取方式非法：{str(row)[:80]}")
            return
        extra_ok = {"raw-repo": len(row) == 2, "sample-repo": len(row) == 3 and isinstance(row[2], bool),
                    "outside": len(row) == 3 and _is_int(row[2]) and row[2] >= 0}[shape]
        if not extra_ok:
            problems.append(f"{name} 的条目长度或类型非法：{str(row)[:80]}")
            return


def _check_unresolved_samples(problems: list[str], where: str, samples: Any, reasons: Any) -> None:
    """``{原因: [原始行样例]}``：最多 20 个原因，每个原因 1 到 3 条，每条是不超过 300 字符的字符串；原因必须出现在 unresolved_reasons 里。"""
    if (not isinstance(samples, dict) or len(samples) > MAX_SAMPLE_REASONS
            or not all(isinstance(reason, str) and reason and isinstance(items, list) and 1 <= len(items) <= MAX_SAMPLES_PER_REASON
                       and all(isinstance(item, str) and len(item) <= SAMPLE_LINE_CHARS for item in items) for reason, items in samples.items())):
        problems.append(f"{where} 必须是 {{原因: 原始行样例列表}}：最多 {MAX_SAMPLE_REASONS} 个原因，每个原因 1 到 {MAX_SAMPLES_PER_REASON} 条，"
                        f"每条是不超过 {SAMPLE_LINE_CHARS} 字符的字符串")
    elif isinstance(reasons, dict) and not set(samples) <= set(reasons):
        problems.append(f"{where} 的原因必须都出现在 coverage.unresolved_reasons 里")


def _check_unfinished_samples(problems: list[str], where: str, samples: Any, limit: int = MAX_UNFINISHED_SAMPLES) -> None:
    """行样例列表：最多 limit 条、每条不超过 300 字符的字符串。悬着调用的起始行样例（登记侧对 coverage.unfinished_samples 的同一约束，
    limit 取 8）与被丢弃的垃圾行样例共用它。"""
    if (not isinstance(samples, list) or len(samples) > limit
            or not all(isinstance(item, str) and len(item) <= SAMPLE_LINE_CHARS for item in samples)):
        problems.append(f"{where} 必须是字符串列表：最多 {limit} 条，每条不超过 {SAMPLE_LINE_CHARS} 字符")


def _check_side_effect_count(problems: list[str], where: str, value: Any, coverage: Any) -> None:
    """副作用类垃圾行的条数：非负整数，且不超过 coverage.detached_markers（它是被丢弃的垃圾行总数，副作用类只是其中一部分）。"""
    if not _is_int(value) or value < 0:
        problems.append(f"{where} 必须是非负整数")
    elif isinstance(coverage, dict) and _is_int(coverage.get("detached_markers")) and value > coverage["detached_markers"]:
        problems.append(f"{where}（{value}）不能超过 coverage.detached_markers（{coverage['detached_markers']}）")


def _check_common(problems: list[str], document: Mapping[str, Any], *, tracked: bool) -> None:
    coverage = document.get("coverage")
    optional = SAMPLE_COVERAGE_OPTIONAL_KEYS if tracked else RAW_COVERAGE_OPTIONAL_KEYS
    if not isinstance(coverage, dict) or not COVERAGE_KEYS <= set(coverage) <= COVERAGE_KEYS | optional:
        problems.append("coverage 的字段集合不符")
    else:
        if not isinstance(coverage["stream_complete"], bool):
            problems.append("coverage.stream_complete 必须是布尔值")
        for key in ("unresolved", "unfinished_calls", "unresolved_process_events", "lines", "detached_markers"):
            if not _is_int(coverage[key]) or coverage[key] < 0:
                problems.append(f"coverage.{key} 必须是非负整数")
        reasons = coverage["unresolved_reasons"]
        if (not isinstance(reasons, dict) or not all(isinstance(key, str) and _is_int(count) and count > 0 for key, count in reasons.items())
                or sum(reasons.values() if isinstance(reasons, dict) else []) != coverage["unresolved"]):
            problems.append("coverage.unresolved_reasons 必须是原因到正计数的映射，且计数之和等于 unresolved")
        if "unresolved_samples" in coverage:
            _check_unresolved_samples(problems, "coverage.unresolved_samples", coverage["unresolved_samples"], reasons)
        if "unfinished_samples" in coverage:
            _check_unfinished_samples(problems, "coverage.unfinished_samples", coverage["unfinished_samples"])
    aliases = document.get("aliases")
    if not isinstance(aliases, dict) or set(aliases) != set(ALIAS_NAMES) or not all(isinstance(value, str) for value in aliases.values()):
        problems.append("aliases 必须恰为 TREE、WORK、HOME、TMP 四个字符串")
    _check_access_rows(problems, "repo_accesses", document.get("repo_accesses"), "sample-repo" if tracked else "raw-repo")
    _check_access_rows(problems, "outside_accesses", document.get("outside_accesses"), "outside")
    commands = document.get("git_commands")
    if (not isinstance(commands, list) or not all(isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv)
                                                  for argv in commands)):
        problems.append("git_commands 必须是非空字符串列表的列表")
    network = document.get("network")
    if (not isinstance(network, list) or not all(isinstance(item, dict) and set(item) == {"family", "target"}
                                                 and all(isinstance(value, str) for value in item.values()) for item in network)):
        problems.append("network 的条目必须恰有 family 与 target 两个字符串")


def _check_command(problems: list[str], command: Any) -> None:
    if not isinstance(command, dict) or set(command) != COMMAND_KEYS:
        problems.append("command 的字段集合不符")
        return
    for key in ("launcher", "argv"):
        if not isinstance(command[key], list) or not all(isinstance(part, str) and part for part in command[key]):
            problems.append(f"command.{key} 必须是字符串列表")
    if not command["argv"]:
        problems.append("command.argv 不能为空")
    if not isinstance(command["cwd_rel"], str) or not command["cwd_rel"] or os.path.isabs(command["cwd_rel"]) or ".." in Path(command["cwd_rel"]).parts:
        problems.append("command.cwd_rel 必须是相对测试树的路径")
    if command["sha256"] != command_digest(command["argv"], command["cwd_rel"]):
        problems.append("command.sha256 与 argv、cwd_rel 不符")


def command_digest(argv: Sequence[str], cwd_rel: str) -> str:
    """命令身份：去掉 launcher 前缀的 argv 加相对测试树的 cwd。登记侧用同一个算法核对，命令一变证据就不再适用。"""
    return sha256_json({"argv": list(argv), "cwd_rel": cwd_rel})


def validate_raw(document: Any) -> list[str]:
    problems: list[str] = []
    if (not isinstance(document, dict) or not RAW_KEYS <= set(document) <= RAW_KEYS | RAW_OPTIONAL_KEYS
            or document.get("schema_version") != RAW_SCHEMA):
        return ["raw 的 schema_version 或字段集合不符"]
    if "detached_samples" in document:
        _check_unfinished_samples(problems, "detached_samples", document["detached_samples"], MAX_DETACHED_SAMPLES)
    if "detached_side_effects" in document:
        _check_side_effect_count(problems, "detached_side_effects", document["detached_side_effects"], document.get("coverage"))
    if not isinstance(document["unit_id"], str) or not document["unit_id"]:
        problems.append("unit_id 必须是非空字符串")
    if not _is_int(document["lines"]) or document["lines"] < 0 or not _is_int(document["chunk_lines"]) or document["chunk_lines"] < 0:
        problems.append("lines 与 chunk_lines 必须是非负整数")
    _check_common(problems, document, tracked=False)
    run = document["run"]
    if run is None:
        return problems
    if not isinstance(run, dict) or not RUN_KEYS <= set(run) <= RUN_KEYS | RUN_OPTIONAL_KEYS:
        return problems + ["run 的字段集合不符"]
    if "no_go_cache" in run and not isinstance(run["no_go_cache"], bool):
        problems.append("run.no_go_cache 必须是布尔值")
    _check_command(problems, run["command"])
    for key in ("label", "cache", "started_utc", "finished_utc", "commit", "kernel", "strace", "sampler_sha256"):
        if not isinstance(run[key], str) or not run[key]:
            problems.append(f"run.{key} 必须是非空字符串")
    if run["cache"] not in ("cold", "warm"):
        problems.append("run.cache 只能是 cold 或 warm")
    if not isinstance(run["timed_out"], bool) or not (run["exit_code"] is None or _is_int(run["exit_code"])):
        problems.append("run.exit_code 与 run.timed_out 类型非法")
    if not isinstance(run["seconds"], (int, float)) or isinstance(run["seconds"], bool):
        problems.append("run.seconds 必须是数字")
    for key in ("git_status_before", "git_status_after"):
        if not isinstance(run[key], list) or not all(isinstance(item, str) for item in run[key]):
            problems.append(f"run.{key} 必须是字符串列表")
    if not isinstance(run["env_overrides"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in run["env_overrides"].items()):
        problems.append("run.env_overrides 必须是字符串映射")
    return problems


def validate_sample(document: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(document, dict) or set(document) != SAMPLE_KEYS or document.get("schema_version") != SAMPLE_SCHEMA:
        return ["sample 的 schema_version 或字段集合不符"]
    if not isinstance(document["unit_id"], str) or not document["unit_id"]:
        problems.append("unit_id 必须是非空字符串")
    _check_command(problems, document["command"])
    sampled = document["sampled"]
    if not isinstance(sampled, dict) or set(sampled) != SAMPLED_KEYS:
        problems.append("sampled 的字段集合不符")
    else:
        if not HEX40.fullmatch(str(sampled["commit"])) or not HEX64.fullmatch(str(sampled["sampler_sha256"])):
            problems.append("sampled.commit 或 sampler_sha256 不是十六进制摘要")
        runs = sampled["runs"]
        if (not isinstance(runs, list) or not runs or not all(isinstance(item, dict) and set(item) == SAMPLED_RUN_KEYS for item in runs)):
            problems.append("sampled.runs 的条目字段集合不符")
    _check_common(problems, document, tracked=True)
    if not (document["exit_code"] is None or _is_int(document["exit_code"])):
        problems.append("exit_code 类型非法")
    if not isinstance(document["valid"], bool) or not isinstance(document["invalid_reasons"], list) \
            or not all(isinstance(item, str) for item in document["invalid_reasons"]):
        problems.append("valid 与 invalid_reasons 类型非法")
    elif document["valid"] == bool(document["invalid_reasons"]):
        problems.append("valid 必须恰好等于 invalid_reasons 为空")
    review = document["review"]
    if not isinstance(review, dict) or not REVIEW_KEYS <= set(review) <= REVIEW_KEYS | REVIEW_OPTIONAL_KEYS:
        problems.append("review 的字段集合不符")
        return problems
    if "unresolved_samples" in review:
        coverage = document["coverage"]
        _check_unresolved_samples(problems, "review.unresolved_samples", review["unresolved_samples"],
                                  coverage.get("unresolved_reasons") if isinstance(coverage, dict) else None)
    if "detached_samples" in review:
        _check_unfinished_samples(problems, "review.detached_samples", review["detached_samples"], MAX_DETACHED_SAMPLES)
    if "detached_side_effects" in review:
        _check_side_effect_count(problems, "review.detached_side_effects", review["detached_side_effects"], document.get("coverage"))
    allowances = review["allowances"]
    if isinstance(allowances, dict):  # 旧证据的 allowances 只有 unix_sockets 与 class_prefixes，这里只核新增的两类登记项
        for key, keys in (("installed_dirs", INSTALLED_ROW_KEYS), ("transient", TRANSIENT_ROW_KEYS)):
            if key in allowances:
                _check_allowance_rows(problems, key, allowances[key], keys)
    return problems


def _check_allowance_rows(problems: list[str], name: str, rows: Any, keys: frozenset[str]) -> None:
    """review.allowances 里 installed_dirs／transient 的每一行：键集合闭合，计数是非负整数，样例是路径字符串列表，unused 是布尔值。"""
    where = f"review.allowances.{name}"
    if not isinstance(rows, list):
        problems.append(f"{where} 必须是列表")
        return
    head = "prefix" if name == "installed_dirs" else "pattern"
    counts = ("reads",) if name == "installed_dirs" else ("paths", "read_back")
    for row in rows:
        if not isinstance(row, dict) or set(row) != keys:
            problems.append(f"{where} 的条目字段集合不符：{str(row)[:80]}")
            return
        if (not isinstance(row[head], str) or not row[head] or not all(_is_int(row[key]) and row[key] >= 0 for key in counts)
                or not isinstance(row["unused"], bool) or not isinstance(row["sample"], list)
                or len(row["sample"]) > ALLOWANCE_SAMPLE_PATHS or not all(isinstance(item, str) for item in row["sample"])
                or (name == "installed_dirs" and (not row["prefix"].endswith("/") or not str(row["contract"]).startswith("cache:")))):
            problems.append(f"{where} 的条目取值非法：{str(row)[:80]}")
            return


# ---------------------------------------------------------------------------
# run：从门禁清单取命令，构造 strace 包装并执行
# ---------------------------------------------------------------------------

# 与入口门禁脚本给 Go 的三个缓存目录同名同位置（WORK 下固定名字）：别名 {WORK} 就是它们的父目录，证据与正式运行才能对上。
CACHE_DIRS = (("GOCACHE", "go-build-cache"), ("GOMODCACHE", "go-mod-cache"), ("GOTMPDIR", "go-tmp"))
FILTER_WAIT_SECONDS = 600.0
DEFAULT_UNIT_TIMEOUT = 7200.0
# strace 对命令的放大实测 1.1 到 1.4 倍；超时按清单超时乘这个系数，免得采样被自己的开销误杀。
DEFAULT_TIMEOUT_FACTOR = 2.5
_PIPE_SAFE = re.compile(r"[A-Za-z0-9_./:=@%+,-]+")


def load_unit(manifest: Path, unit_id: str) -> dict[str, Any]:
    payload = load_document(manifest)
    if payload.get("schema_version") != GATES_SCHEMA:
        raise SamplerError(f"不是门禁清单（{GATES_SCHEMA}）：{manifest}")
    units = payload.get("units")
    if not isinstance(units, list):
        raise SamplerError("门禁清单没有 units 列表")
    for unit in units:
        if isinstance(unit, dict) and unit.get("unit_id") == unit_id:
            break
    else:
        raise SamplerError(f"清单里没有命令单元 {unit_id}（测试单元不由本工具采样）")
    argv, cwd = unit.get("argv"), unit.get("cwd")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        raise SamplerError(f"单元 {unit_id} 的 argv 非法")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise SamplerError(f"单元 {unit_id} 的 cwd 必须是绝对路径")
    env = unit.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise SamplerError(f"单元 {unit_id} 的环境变量非法")
    return unit


def split_launcher(argv: Sequence[str], launcher_json: str | None) -> tuple[list[str], list[str]]:
    """清单里命令单元的 argv 是 [*launcher, *命令]，两者没有分界标记，必须靠调用方告知或认出已知的启动前缀。

    显式给 ``--launcher-json`` 时要求 argv 以它开头；不给时只认已知的 ARM64 前缀。看起来像启动前缀（以 unshare 开头）
    却认不出来时拒绝：悄悄把 unshare 当成命令的一部分，trace 里会满是 mount 之类的未建模调用，整次采样白跑。
    """
    if launcher_json is None:
        launcher = list(KNOWN_LAUNCHER) if list(argv[:len(KNOWN_LAUNCHER)]) == KNOWN_LAUNCHER else []
        if not launcher and argv[0] == "unshare":
            raise SamplerError("清单命令以 unshare 开头但不是已知的启动前缀；请显式给出 --launcher-json（与生成清单时的值相同）")
    else:
        try:
            launcher = json.loads(launcher_json)
        except ValueError as error:
            raise SamplerError(f"--launcher-json 不是合法 JSON：{error}") from error
        if not isinstance(launcher, list) or not all(isinstance(part, str) and part for part in launcher):
            raise SamplerError("--launcher-json 必须是非空字符串组成的列表")
        if list(argv[:len(launcher)]) != launcher:
            raise SamplerError("清单里的命令不以 --launcher-json 给出的启动前缀开头")
    command = list(argv[len(launcher):])
    if not command:
        raise SamplerError("去掉启动前缀后没有命令")
    return list(launcher), command


def resolve_cwd(unit: Mapping[str, Any], tree: Path) -> tuple[str, str]:
    cwd = Path(unit["cwd"]).resolve()
    if cwd != tree and tree not in cwd.parents:
        raise SamplerError(f"单元 {unit['unit_id']} 的 cwd 不在采样树内：{cwd}（--tree 必须与生成清单时 plan 的 --tree 相同）")
    if not cwd.is_dir():
        raise SamplerError(f"单元 {unit['unit_id']} 的 cwd 不存在：{cwd}")
    return str(cwd), os.path.relpath(cwd, tree)


def build_environment(*, base: Mapping[str, str], unit: Mapping[str, Any], work: Path, out_dir: Path,
                      overrides: Mapping[str, str]) -> dict[str, str]:
    """与 Scheduler.launch 给命令单元的环境逐项对应：额度核数、GOMAXPROCS、清单自带 env、UNIT_EXECUTOR_* 标记、禁写字节码。

    base 是调度者按门禁白名单准备好的环境（env -i 加 PATH、LANG、HOME 等），这里不再剥离也不再补充，采样必须看到与正式
    运行相同的环境。Go 的三个缓存目录钉在 WORK 下的固定名字，环境里若已有别的值就拒绝（别名对不上，证据没法和正式运行比）。
    """
    env = dict(base)
    cores = unit.get("cores", 1)
    count = str(max(1, math.ceil(cores if isinstance(cores, (int, float)) and not isinstance(cores, bool) else 1)))
    env["UNIT_EXECUTOR_CORES"] = count
    env["GOMAXPROCS"] = count
    env.update(unit.get("env") or {})
    env["UNIT_EXECUTOR_UNIT"] = unit["unit_id"]
    env["UNIT_EXECUTOR_KIND"] = "formal"
    env["UNIT_EXECUTOR_RECORDS_MODULE"] = str(HERE.with_name("unit_records.py"))
    env["UNIT_EXECUTOR_PARENT_STATE_DIR"] = str(Path(out_dir) / "sampler-state")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    names = {name for name, _ in CACHE_DIRS}
    for name, sub in CACHE_DIRS:
        expected = str(work / sub)
        if env.get(name) not in (None, "", expected):
            raise SamplerError(f"环境变量 {name}={env[name]} 与 --work 下的固定位置 {expected} 不一致；去掉它，或让 --work 指向它的父目录")
        env[name] = expected
    for name, value in overrides.items():
        if name in names:
            raise SamplerError(f"--env 不能覆盖 {name}：Go 缓存位置必须固定在 --work 下")
        env[name] = value
    return env


def prepare_caches(work: Path, cache: str, *, no_go_cache: bool = False) -> list[str]:
    """冷缓存＝一个空的 GOCACHE；热缓存＝紧接着复用上一次写热的那个。返回需要告诉调度者的提示。

    不删除任何目录：冷缓存遇到非空旧目录时只把它改名保留（调度者之后用 Python 清理），改名可逆，删除不可逆。

    ``no_go_cache`` 给不用 Go 缓存的单元（前端的 lint／typecheck／vitest、Python 工具）：它们的冷热对比看的是自己的性能缓存
    （eslint、vitest、tsbuildinfo），与 GOCACHE 无关，所以热缓存不再要求 go-build-cache 非空，冷缓存也不改名这个目录——同一个
    --work 下可能还留着给 Go 单元写热的缓存，改名会白白毁掉它。目录不存在时仍建一个空的（环境变量 GOCACHE 指着它），
    符号链接照旧拒绝。
    """
    notes: list[str] = []
    build = work / "go-build-cache"
    if build.is_symlink():
        raise SamplerError(f"{build} 不能是符号链接")
    if no_go_cache:
        if build.exists() and not build.is_dir():
            raise SamplerError(f"{build} 不是目录")
        build.mkdir(mode=0o700, exist_ok=True)
        (work / "go-tmp").mkdir(mode=0o700, exist_ok=True)
        notes.append("--no-go-cache：未触碰 go-build-cache 的内容（冷缓存不改名旧目录，热缓存不要求它非空）；冷热差异只来自单元自己的缓存")
        return notes
    if cache == "cold":
        if build.exists():
            if not build.is_dir():
                raise SamplerError(f"{build} 不是目录")
            if any(build.iterdir()):
                renamed = work / f"go-build-cache.replaced-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')}-{os.getpid()}"
                os.rename(build, renamed)
                notes.append(f"冷缓存：旧的非空 GOCACHE 已改名保留为 {renamed}（未删除）")
        build.mkdir(mode=0o700, exist_ok=True)
    elif not build.is_dir() or not any(build.iterdir()):
        raise SamplerError("热缓存要求 --work 下的 go-build-cache 非空；请先对同一单元跑一次 --cache cold")
    (work / "go-tmp").mkdir(mode=0o700, exist_ok=True)
    if not (work / "go-mod-cache").is_dir():
        notes.append("警告：--work 下没有 go-mod-cache；离线环境下 Go 类单元会因缺依赖而失败")
    return notes


def filter_pipe(*, output: Path, unit_id: str, aliases: Mapping[str, str], chunk_lines: int) -> str:
    """strace -o '|…' 管道另一端的命令行。strace 会把它交给 sh 解释，所以任何空白或 shell 特殊字符都拒绝。"""
    parts = [sys.executable, "-B", str(HERE), "filter", "--output", str(output), "--unit-id", unit_id, "--chunk-lines", str(chunk_lines)]
    for name in ALIAS_NAMES:
        parts += ["--alias", f"{name}={aliases[name]}"]
    for part in parts:
        if not _PIPE_SAFE.fullmatch(part):
            raise SamplerError(f"过滤器命令里的片段带空白或 shell 特殊字符，不能交给 strace 的管道：{part}")
    return " ".join(parts)


def build_strace_argv(*, launcher: Sequence[str], strace: str, pipe: str, cwd: str, command: Sequence[str]) -> list[str]:
    """launcher 在最外侧，然后是 strace，被跟踪的是“切目录再 exec 命令”的 sh——与 Scheduler.launch 的命令单元包装一致。

    为什么 launcher 放 strace 外侧：它的 unshare、bash、mount 不属于命令本身，放进 trace 只会产生 unsupported-call；放外侧
    则命令仍然在同一个私有挂载命名空间里运行（launcher 最后 exec 的就是 strace），读到的文件系统与正式执行完全一样。
    """
    return [*launcher, strace, *STRACE_OPTIONS, "-o", "|" + pipe, "--", "/bin/sh", "-c", 'cd -- "$0" && exec "$@"', cwd, *command]


def git_status_lines(tree: Path) -> list[str]:
    """运行前后各取一次：非空说明命令改了跟踪文件或留下了未忽略的新文件，采样证据不可用。--no-optional-locks 避免刷新 index。"""
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(tree), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        return [f"<git status 失败 rc={result.returncode}>"]
    return [line for line in result.stdout.decode("utf-8", "surrogateescape").splitlines() if line]


def git_head(tree: Path) -> str:
    result = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    commit = result.stdout.strip()
    if result.returncode or not HEX40.fullmatch(commit):
        raise SamplerError(f"{tree} 不是有提交的 git 仓库")
    return commit


def strace_version(strace: str, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run([strace, "--version"], capture_output=True, text=True, env=dict(env), timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first = result.stdout.splitlines()[0] if result.stdout else ""
    found = re.search(r"version\s+(\S+)", first)
    return found.group(1) if found else (first or "unknown")


class DockerWatch:
    """integration 类单元的容器前后对比（docker ps -a 前后差集，加 docker events 的 create 事件）。

    只报告、不清理：自动 docker rm 在采样机上有误删风险，残留容器的 ID 与镜像写进 raw，由调度者决定怎么处理。Docker 不可用时
    只记录错误，不中断采样（命令本身没有 Docker 会自己失败）。
    """

    def __init__(self, env: Mapping[str, str], events_path: Path) -> None:
        self.env = dict(env)
        self.events_path = Path(events_path)
        self.before: dict[str, str] = {}
        self.error: str | None = None
        self._events: subprocess.Popen[bytes] | None = None
        self._sink = None

    def _ps(self) -> dict[str, str]:
        result = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}}"], env=self.env, capture_output=True, text=True,
                                timeout=60, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise SamplerError(f"docker ps 失败 rc={result.returncode}")
        rows: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if parts:
                rows[parts[0]] = parts[1] if len(parts) > 1 else ""
        return rows

    def start(self) -> None:
        try:
            self.before = self._ps()
            self._sink = self.events_path.open("wb")
            self._events = subprocess.Popen(
                ["docker", "events", "--filter", "type=container", "--filter", "event=create", "--format", "{{json .}}"],
                env=self.env, stdin=subprocess.DEVNULL, stdout=self._sink, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.5)  # 给 events 订阅一点就绪时间，漏掉的由前后差集补上
        except (OSError, subprocess.SubprocessError, SamplerError) as error:
            self.error = str(error)

    def finish(self) -> dict[str, Any]:
        created: dict[str, str] = {}
        after: dict[str, str] = {}
        if self._events is not None:
            time.sleep(0.5)
            self._events.terminate()
            try:
                self._events.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._events.kill()
                self._events.wait()
        if self._sink is not None:
            self._sink.close()
            for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                    actor = event.get("Actor") or {}
                    identifier = str(actor.get("ID") or event.get("id") or "")
                    image = str((actor.get("Attributes") or {}).get("image") or event.get("from") or "")
                except (ValueError, AttributeError):
                    continue
                if identifier:
                    created[identifier] = image
        if self.error is None:
            try:
                after = self._ps()
            except (OSError, subprocess.SubprocessError, SamplerError) as error:
                self.error = str(error)
        new = {identifier: image for identifier, image in after.items() if identifier not in self.before}
        return {"enabled": True, "error": self.error,
                "new_containers": [{"id": key, "image": new[key]} for key in sorted(new)],
                "created": [{"id": key, "image": created[key]} for key in sorted(created)]}


def _kill_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_sample(*, manifest: Path, unit_id: str, out_dir: Path, tree: Path, work: Path, cache: str, label: str | None,
               launcher_json: str | None, env_overrides: Mapping[str, str], docker_hooks: bool, timeout_factor: float,
               chunk_lines: int, base_env: Mapping[str, str] | None = None, no_go_cache: bool = False
               ) -> tuple[int, Path, dict[str, Any]]:
    """采样一次：返回（退出码, raw 路径, 摘要）。退出码语义见模块说明。``no_go_cache`` 见 ``prepare_caches``。"""
    tree, work, out_dir = Path(tree).resolve(), Path(work).resolve(), Path(out_dir).resolve()
    if cache not in ("cold", "warm"):
        raise SamplerError("--cache 只能是 cold 或 warm")
    label = label or cache
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise SamplerError("--label 只能含字母、数字、下划线、点和连字符")
    if not tree.is_dir() or not work.is_dir():
        raise SamplerError("--tree 与 --work 必须是已存在的目录")
    unit = load_unit(manifest, unit_id)
    launcher, command = split_launcher(unit["argv"], launcher_json)
    cwd, cwd_rel = resolve_cwd(unit, tree)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f"{safe_name(unit_id)}.{label}"
    raw_path, log_path = out_dir / f"{stem}.raw.json", out_dir / f"{stem}.log"
    if raw_path.exists() or log_path.exists():
        raise SamplerError(f"{raw_path.name} 或其日志已存在；同一单元同一标签不能重复采样（换 --label）")
    environment = build_environment(base=os.environ if base_env is None else base_env, unit=unit, work=work, out_dir=out_dir,
                                    overrides=env_overrides)
    aliases = {"TREE": str(tree), "WORK": str(work), "HOME": environment.get("HOME") or os.path.expanduser("~"),
               "TMP": environment.get("TMPDIR") or "/tmp"}
    AliasTable(aliases)  # 提前校验别名合法
    strace = shutil.which("strace", path=environment.get("PATH"))
    if strace is None:
        raise SamplerError("PATH 里找不到 strace")
    before = git_status_lines(tree)
    if before:
        raise SamplerError("采样树运行前就不干净（git status --porcelain --untracked-files=all 非空），拒绝采样：" + "; ".join(before[:5]))
    # 所有只读校验做完之后才动缓存目录（冷缓存会改名旧目录），免得后面的参数错误留下半截副作用。
    pipe = filter_pipe(output=raw_path, unit_id=unit_id, aliases=aliases, chunk_lines=chunk_lines)
    argv = build_strace_argv(launcher=launcher, strace=strace, pipe=pipe, cwd=cwd, command=command)
    commit, version = git_head(tree), strace_version(strace, environment)
    notes = prepare_caches(work, cache, no_go_cache=no_go_cache)
    watch = DockerWatch(environment, out_dir / f"{stem}.docker-events.jsonl") if docker_hooks else None
    if watch is not None:
        watch.start()
    timeout = float(unit.get("timeout_seconds") or DEFAULT_UNIT_TIMEOUT) * timeout_factor
    started_utc, started = utc_now(), time.monotonic()
    timed_out = False
    try:
        with log_path.open("xb") as log:
            try:
                process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            except OSError as error:
                raise SamplerError(f"启动失败（启动前缀或 strace 不可执行）：{error}") from error
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _kill_group(process, signal.SIGKILL)
                    process.wait()
    except BaseException:
        if watch is not None:
            watch.finish()  # 不管怎么失败，docker events 子进程都要收掉
        raise
    seconds = round(time.monotonic() - started, 3)
    finished_utc = utc_now()
    waited = time.monotonic()
    while not raw_path.exists() and time.monotonic() - waited < FILTER_WAIT_SECONDS:
        time.sleep(0.2)
    filter_wait = round(time.monotonic() - waited, 3)
    docker = watch.finish() if watch is not None else None
    after = git_status_lines(tree)
    summary: dict[str, Any] = {"unit_id": unit_id, "label": label, "raw": str(raw_path), "log": str(log_path), "seconds": seconds,
                               "notes": notes}
    if not raw_path.exists():
        summary["error"] = ("命令超时被终止，过滤器没有来得及产出 raw" if timed_out else "过滤器没有产出 raw；查看日志中过滤器的报错")
        return (1 if timed_out else 3), raw_path, summary
    raw = load_document(raw_path)
    problems = validate_raw(raw)
    if problems:
        summary["error"] = "过滤器产出的 raw 不合法：" + "；".join(problems[:3])
        return 3, raw_path, summary
    returncode = process.returncode
    raw["run"] = {
        "label": label, "cache": cache, "exit_code": returncode if returncode >= 0 else None,
        "signal": -returncode if returncode < 0 else None, "timed_out": timed_out, "seconds": seconds,
        "started_utc": started_utc, "finished_utc": finished_utc,
        "command": {"launcher": launcher, "argv": command, "cwd_rel": cwd_rel, "sha256": command_digest(command, cwd_rel)},
        "git_status_before": before, "git_status_after": after, "commit": commit, "kernel": platform.release(), "strace": version,
        "sampler_sha256": file_sha256(HERE), "env_overrides": dict(sorted(env_overrides.items())), "docker": docker,
        "filter_wait_seconds": filter_wait,
    }
    if no_go_cache:  # 可选键：只在打开时写，不带该开关的 raw 与从前逐字相同
        raw["run"]["no_go_cache"] = True
    write_document(raw_path, raw)
    summary.update(exit_code=raw["run"]["exit_code"], signal=raw["run"]["signal"], timed_out=timed_out, lines=raw["lines"],
                   unresolved=raw["coverage"]["unresolved"], git_status_after=len(after))
    return (0 if returncode == 0 and not timed_out else 1), raw_path, summary


# ---------------------------------------------------------------------------
# 仓库外路径的分类（merge 判写入合规、draft 起草 outside 共用同一张表）
# ---------------------------------------------------------------------------

OUTSIDE_CLASSES = ("toolchain", "dependency-content", "performance-cache", "scratch", "system", "virtual", "home-config")
# 这几类在 scope 里必须挂合同（home-config 的合同可以由路径派生，所以允许省略）。
CLASSES_NEEDING_CONTRACT = frozenset({"toolchain", "dependency-content", "performance-cache"})
# 允许被命令写入的仓库外类别：只影响速度的缓存和不绑定的暂存。往工具链、依赖内容、系统或 HOME 配置里写都说明命令在改变
# 自己的输入（例如 GOMODCACHE 被写意味着在联网下载），采样一律判无效。
WRITABLE_CLASSES = frozenset({"performance-cache", "scratch"})
# outside 前缀的宽度限制（只限 scratch 与 virtual 两类，其它类不动）：scratch 读写执行全放行、virtual 不绑定任何内容，所以前缀必须落在评审过的
# 窄范围内——否则 --class-prefix 可以把 /home/、/usr/ 登记成 scratch（等于放开读写执行），或把任意目录登记成 virtual（等于不绑定）。
# 两张表抄自登记侧 unit_records._DECLARED_SCRATCH_PREFIXES／_DECLARED_VIRTUAL_PREFIXES：采样器不 import 登记侧的私有常量，两边各自测试
# （test_ci_declared_sampler_followup 里用登记侧的同名常量交叉核对同值），改动必须同步评审。前缀等于表里某项，或在以 / 结尾的某项之下才合格
# （见 prefix_within）；/dev/null 是精确路径，只许它自己。依据：真实样本里这两类用到的前缀正好就是这些。
SCRATCH_CLASS_PREFIXES = ("/dev/null", "{TMP}/", "{WORK}/go-tmp/", "{HOME}/.config/go/telemetry/")
VIRTUAL_CLASS_PREFIXES = ("/proc/", "/sys/", "/dev/", "/run/")

# （前缀, 类别, 合同）。前缀以 / 结尾表示目录前缀，否则表示精确路径及其子路径。注意 /usr/local 与 /opt 下除工具链之外的
# 内容故意不归类：手装二进制不在 dpkg 包摘要的覆盖范围内，必须逐条人工评审，不能被一个宽前缀悄悄吞掉。
BUILTIN_CLASSES: tuple[tuple[str, str, str | None], ...] = (
    ("/usr/local/go/", "toolchain", "toolchain:go"),
    ("/opt/node-v20/", "toolchain", "toolchain:node"),
    ("{WORK}/go-build-cache/", "performance-cache", "cache:go-build-cache"),
    ("{WORK}/go-mod-cache/", "dependency-content", "cache:go-mod-cache"),
    ("{WORK}/go-tmp/", "scratch", None),
    ("{TMP}/", "scratch", None),
    ("/usr/lib/", "system", None), ("/usr/lib64/", "system", None), ("/usr/libexec/", "system", None),
    ("/usr/share/", "system", None), ("/usr/bin/", "system", None), ("/usr/sbin/", "system", None),
    ("/usr/include/", "system", None), ("/lib/", "system", None), ("/lib64/", "system", None),
    ("/bin/", "system", None), ("/sbin/", "system", None), ("/etc/", "system", None),
    # /run 是运行期状态目录（systemd 的解析器配置 /run/systemd/resolve/、nscd 套接字所在目录等）：读取它们与读 /proc、/sys 同属“宿主运行期
    # 状态”，不绑定内容，归 virtual（只许 read／stat／dir／list／missing，写入照旧违规）。
    ("/proc/", "virtual", None), ("/sys/", "virtual", None), ("/dev/", "virtual", None), ("/run/", "virtual", None),
    ("{HOME}/", "home-config", None),
)


class Classifier:
    """最长前缀匹配；额外登记的前缀（--class-prefix）与内置前缀等长时额外登记的优先。"""

    def __init__(self, extra: Sequence[tuple[str, str, str | None]] = ()) -> None:
        entries = [*BUILTIN_CLASSES, *extra]
        ordered = sorted(enumerate(entries), key=lambda item: (-len(item[1][0]), -item[0]))
        self.entries = [entry for _, entry in ordered]
        self.prefixes = [entry[0] for entry in self.entries]

    def classify(self, key: str) -> tuple[str, str | None, str] | None:
        probe = key if key.endswith("/") else key + "/"
        for prefix, cls, contract in self.entries:
            if probe.startswith(prefix if prefix.endswith("/") else prefix + "/"):
                return cls, contract, prefix
        return None


def prefix_within(prefix: str, allowed: Sequence[str]) -> bool:
    """前缀是否落在评审过的窄范围表里：等于表里某项，或在以 / 结尾的某项之下（只更窄，不更宽）才合格；不以 / 结尾的项（如 /dev/null）是
    精确路径，只许它自己。与登记侧 ``unit_records._declared_prefix_within`` 同口径（采样器自己的一份，两边各自测试）。"""
    return any(prefix == item or (item.endswith("/") and prefix.startswith(item)) for item in allowed)


def parse_class_prefix(spec: str) -> tuple[str, str, str | None]:
    """``前缀=类别[:合同]`` 的人工登记，用于内置表没有覆盖、但经评审认定可以接受的路径（例如 Go 的 telemetry 计数目录）。

    与登记侧一致的宽度限制：class=scratch 的前缀只许 ``SCRATCH_CLASS_PREFIXES`` 里的项或它们之下更窄的路径，class=virtual 的前缀只许
    ``VIRTUAL_CLASS_PREFIXES`` 里的项或它们之下更窄的路径（见 ``prefix_within``）；其它类别不限。不合格报 ``SamplerError``：这样的前缀登记
    表里写出来，登记侧的范围校验也会拒绝，不留到登记时才发现。"""
    prefix, separator, rest = spec.partition("=")
    cls, _, contract = rest.partition(":")
    if (not separator or cls not in OUTSIDE_CLASSES
            or not (prefix.startswith("/") or prefix.startswith(("{WORK}/", "{HOME}/", "{TMP}/")))):
        raise SamplerError(f"--class-prefix 必须是 前缀=类别[:合同]，前缀以 / 或 {{WORK}}/、{{HOME}}/、{{TMP}}/ 开头，类别取 {'、'.join(OUTSIDE_CLASSES)}：{spec!r}")
    if cls in CLASSES_NEEDING_CONTRACT and not contract:
        raise SamplerError(f"类别 {cls} 必须带合同（前缀=类别:合同）：{spec!r}")
    for class_name, allowed in (("scratch", SCRATCH_CLASS_PREFIXES), ("virtual", VIRTUAL_CLASS_PREFIXES)):
        if cls == class_name and not prefix_within(prefix, allowed):
            raise SamplerError(f"--class-prefix：class={class_name} 的前缀只许 {list(allowed)} 或它们之下更窄的路径"
                               f"（范围过宽会放开读写执行或不绑定任何内容，登记侧会拒绝）：{spec!r}")
    return prefix, cls, contract or None


def home_contract(key: str) -> str:
    """HOME 下配置按路径派生合同编号：{HOME}/.config/go/env 变成 host-config:config-go-env。"""
    relative = key[len("{HOME}"):].strip("/") if key.startswith("{HOME}") else key.strip("/")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", relative.lstrip(".")).strip("-").lower()
    return "host-config:" + (slug or "home")


def network_problem(family: str, target: str, allowed_unix: Iterable[str]) -> str | None:
    """网络目的地是否满足“只有本机和登记的 AF_UNIX”。目的地不可判（sockaddr 没解码出来、地址解析不了、认不出的地址族）的一律
    算问题：宁可误判无效，不可漏判，更不能当成没有网络。"""
    if family == "AF_UNSPEC":
        return None
    if family == "AF_UNIX":
        return None if target in set(allowed_unix) else f"AF_UNIX 目标未登记：{target}"
    if family in ("AF_INET", "AF_INET6"):
        host = target.rsplit(":", 1)[0].strip("[]")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return f"网络目的地不可判（地址解析不了）：{family} {target}"
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return None if address.is_loopback else f"非本机地址：{family} {target}"
    if family == "unparsed":
        return f"网络目的地不可判（sockaddr 没有解码成结构，可能仍是指针或路径被截断；检查 -e verbose=…,connect）：{target}"
    return f"网络目的地不可判（无法证明是本机连接）：{family} {target}"


# ---------------------------------------------------------------------------
# merge 的人工登记项：AF_UNIX 目标、依赖安装目录、自产暂存路径
# 校验规则与登记侧（unit_records.validate_declared_scope）对齐：merge 现在拒绝的写法，登记侧本来也会拒绝，不留到登记时才发现。
# ---------------------------------------------------------------------------

_CONTRACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def check_unix_socket(value: Any) -> str:
    """AF_UNIX 目标：以 / 开头、不以 / 结尾、无空／.／.. 分量、无反斜杠与控制字符的规范绝对路径（登记侧 ``unix_sockets`` 的写法）。

    抽象套接字（``@名字``）与别名路径登记侧都不认，所以这里也拒绝：与其合并出一份看似有效、到覆盖检查才被拒的采样，不如现在报错。
    """
    if (not isinstance(value, str) or not value.startswith("/") or value.endswith("/") or any(ch in value for ch in "\\\0\n\r")
            or any(part in ("", ".", "..") for part in value[1:].split("/"))):
        raise SamplerError("--allow-unix 的每项必须是以 / 开头、不以 / 结尾、无空／.／.. 分量的规范绝对路径（登记侧 unix_sockets 的写法，"
                           f"抽象套接字 @名字 无法登记）：{value!r}")
    return value


def check_repo_relative(value: Any, where: str, *, directory: bool | None = None) -> str:
    """仓库相对路径：非空、不以 / 开头、无反斜杠与控制字符、无空／.／.. 分量；``directory`` 为真要求以 / 结尾，为假要求不以 / 结尾。"""
    if not isinstance(value, str) or not value or value.startswith("/") or any(ch in value for ch in "\\\0\n\r"):
        raise SamplerError(f"{where} 必须是非空、不以 / 开头、不含反斜杠与控制字符的仓库相对路径：{value!r}")
    if directory is True and not value.endswith("/"):
        raise SamplerError(f"{where} 必须是以 / 结尾的目录前缀：{value!r}")
    if directory is False and value.endswith("/"):
        raise SamplerError(f"{where} 必须是不以 / 结尾的单个路径或 glob：{value!r}")
    parts = value.split("/")
    if value.endswith("/"):
        parts = parts[:-1]
    if any(part in ("", ".", "..") for part in parts):
        raise SamplerError(f"{where} 不得含空、. 或 .. 分量：{value!r}")
    return value


# 依赖安装目录：合同 → 它唯一允许的前缀。安装目录的豁免（读取不要求已跟踪）只对合同定义里写明的那个目录开放，否则登记表可以把任何不含
# 已声明输入的目录（backend/internal/、src/gen/）登记成「未跟踪读取豁免区」，等于自己放宽合同。抄自登记侧
# unit_records._DECLARED_INSTALLED_PREFIXES：采样器不 import 登记侧的私有常量，两边各自测试（test_ci_declared_sampler_followup 用登记侧的同名
# 常量交叉核对同值），改动必须同步评审——新增安装目录先在登记侧评审登记，再抄到这里。
INSTALLED_DIR_PREFIXES = {"cache:node-modules": "frontend/node_modules/"}


def check_installed_dir(prefix: Any, contract: Any) -> tuple[str, str]:
    """依赖安装目录：前缀是以 / 结尾的仓库相对路径，合同是 ``cache:<名字>``（登记侧要求安装目录的内容由缓存类合同绑定）。

    另与登记侧一致：合同必须是 ``INSTALLED_DIR_PREFIXES`` 里评审过的依赖安装合同，前缀必须恰等于该合同定义的安装目录（目前只有
    ``cache:node-modules`` 对应 ``frontend/node_modules/``）——前缀多一级、少一级、换个目录、没有尾斜杠都不行。"""
    check_repo_relative(prefix, "--installed-dir 的前缀", directory=True)
    kind, separator, name = contract.partition(":") if isinstance(contract, str) else ("", "", "")
    if not separator or kind != "cache" or not _CONTRACT_NAME.fullmatch(name):
        raise SamplerError(f"--installed-dir 的合同必须是 cache:<名字>（名字只含字母、数字、点、下划线、连字符）：{contract!r}")
    allowed = INSTALLED_DIR_PREFIXES.get(contract)
    if allowed is None:
        raise SamplerError(f"--installed-dir 的合同 {contract!r} 不是评审过的依赖安装合同（登记侧只认 {sorted(INSTALLED_DIR_PREFIXES)}，"
                           "否则登记表可以把任何目录登记成未跟踪读取的豁免区）")
    if prefix != allowed:
        raise SamplerError(f"--installed-dir 的前缀必须等于合同 {contract} 定义的安装目录 {allowed!r}：{prefix!r}")
    return prefix, contract


def parse_installed_dir(spec: str) -> tuple[str, str]:
    """``前缀=合同``，例如 ``frontend/node_modules/=cache:node-modules``。"""
    prefix, separator, contract = spec.rpartition("=")  # 合同里不会有 =，前缀里的 = 照字面算
    if not separator:
        raise SamplerError(f"--installed-dir 必须是 前缀=合同（如 frontend/node_modules/=cache:node-modules）：{spec!r}")
    return check_installed_dir(prefix, contract)


# glob 里 ``*``／``?`` 合计的个数上限，抄自登记侧 unit_records._DECLARED_MAX_GLOB_WILDCARDS（采样器自己的一份，两边各自测试同值）：
# WP1g 起匹配本身是线性的（见 PathMatcher：按 / 分段、每段双指针，不再是会回溯放大的正则），这个上限只剩登记表卫生的意义——
# 暂存路径模式是人评审过的，4 个通配符足够，更多的多半是写错了。
MAX_GLOB_WILDCARDS = 4


def check_transient(pattern: Any) -> str:
    """自产暂存路径的模式：仓库相对的目录前缀（以 / 结尾，字面匹配，不支持通配符），或不以 / 结尾的 glob。

    glob 只支持 ``*``（一段路径内任意个字符）与 ``?``（一段路径内一个字符），都不跨 /，整条相对路径必须完全匹配（不含它的
    子路径）；不支持 ``**`` 与方括号字符类；``*``／``?`` 合计最多 ``MAX_GLOB_WILDCARDS``（4）个。不含通配符的项就是一个精确路径。写法与
    登记侧 ``repo_ignored`` 的每一项一致。
    """
    if isinstance(pattern, str) and pattern.endswith("/"):
        check_repo_relative(pattern, "--transient", directory=True)
        if any(char in pattern for char in "*?[]"):
            raise SamplerError(f"--transient 以 / 结尾的是目录前缀，不支持通配符；要通配请写不以 / 结尾的 glob：{pattern!r}")
        return pattern
    check_repo_relative(pattern, "--transient", directory=False)
    if "**" in pattern:
        raise SamplerError(f"--transient 不支持 **（* 不跨 /，要整个目录请写以 / 结尾的前缀）：{pattern!r}")
    if "[" in pattern or "]" in pattern:
        raise SamplerError(f"--transient 的 glob 只支持 * 与 ?，不支持方括号字符类：{pattern!r}")
    wildcards = pattern.count("*") + pattern.count("?")
    if wildcards > MAX_GLOB_WILDCARDS:
        raise SamplerError(f"--transient 的 glob 通配符（* 与 ? 合计）最多 {MAX_GLOB_WILDCARDS} 个，实际 {wildcards} 个"
                           f"（登记表卫生上限，登记侧同样拒绝）：{pattern!r}")
    return pattern


def _glob_segment_matches(pattern: str, text: str) -> bool:
    """一段（不含 /）的通配符匹配：``*`` 是任意个字符（含零个）、``?`` 是恰好一个字符，其它字符按字面。经典的双指针算法——只记最近一个 ``*``
    的回退点，遇到不匹配就让那个 ``*`` 多吞一个字符再试；时间 O(len(pattern)×len(text))，没有正则那样随通配符个数多项式放大的回溯
    （WP1g：旧实现把 ``*`` 译成 ``[^/]*`` 的正则，``out/*a*a*a*b`` 对 1000 字符的路径要 25 秒以上）。与登记侧 ``unit_records._declared_glob`` 同一个
    算法（采样器自己的一份，两边不互相 import，测试里用旧正则和登记侧的实现对拍）。"""

    p = t = 0
    star = -1  # 最近一个 * 在 pattern 里的位置
    mark = 0  # 那个 * 目前吞到 text 的位置
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pattern) and (pattern[p] == "?" or pattern[p] == text[t]):
            p += 1
            t += 1
        elif star >= 0:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < len(pattern) and pattern[p] == "*":
        p += 1
    return p == len(pattern)


class PathMatcher:
    """仓库相对路径的匹配器，语义与登记侧 ``repo_ignored`` 一致：目录前缀（含目录自身）或不跨 / 的 glob（整条路径完全匹配）。

    不用 ``fnmatch``：它的 ``*`` 会跨 /，``frontend/*.mjs`` 就会误中 ``frontend/node_modules/x/y.mjs``。glob 的匹配是线性的：先按 / 切段，段数必须
    相同，再逐段做通配符匹配（``_glob_segment_matches``；没有通配符的段直接比相等），所以 ``*``／``?`` 永远不会跨 /。对外行为不变：``pattern``、
    ``directory`` 两个属性，``matches(相对路径) -> bool``。
    """

    def __init__(self, pattern: str) -> None:
        self.pattern = check_transient(pattern)
        self.directory = pattern.endswith("/")
        # glob 的每一段连同「有没有通配符」预先算好（匹配可能被调用几十万次）；目录前缀不需要。
        self._segments = None if self.directory else tuple((segment, "*" in segment or "?" in segment) for segment in pattern.split("/"))

    def matches(self, relative: str) -> bool:
        if self.directory:
            return (relative + "/").startswith(self.pattern)  # 加一个 / 才能把目录自身（a/b）也算进前缀 a/b/
        parts = relative.split("/")
        if len(parts) != len(self._segments):  # type: ignore[arg-type]
            return False
        return all(part == segment if not wild else _glob_segment_matches(segment, part)
                   for (segment, wild), part in zip(self._segments, parts))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# merge：多次 raw 取并集，标 tracked，判有效性
# ---------------------------------------------------------------------------


def repo_rel(path: str) -> str:
    """别名路径换成相对仓库根的路径（{TREE} 本身是空串）。"""
    return "" if path == "{TREE}" else path[len("{TREE}/"):]


def tracked_sets(tree: Path) -> tuple[set[str], set[str]]:
    """git 跟踪的文件集合，和含跟踪文件的目录集合（含根的空串）。范围摘要只覆盖跟踪文件，所以 tracked 是核心判据。"""
    result = subprocess.run(["git", "-C", str(tree), "ls-files", "-z"], capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise SamplerError(f"git ls-files 失败：{tree} 不是 git 仓库？")
    files = {name.decode("utf-8", "surrogateescape") for name in result.stdout.split(b"\0") if name}
    directories = {""}
    for name in files:
        parent = os.path.dirname(name)
        while parent and parent not in directories:
            directories.add(parent)
            parent = os.path.dirname(parent)
    return files, directories


def _check_ignore(tree: Path, relatives: Sequence[str]) -> tuple[int, set[str]]:
    """一次 git check-ignore（--no-index：不看是否跟踪，只评估规则）。目录规则 out/ 对已删除的路径要带斜杠才命中，
    所以文件形式和目录形式各问一次，任一命中即算被忽略。"""
    payload = b"".join(rel.encode("utf-8", "surrogateescape") + b"\0" + rel.encode("utf-8", "surrogateescape") + b"/\0" for rel in relatives)
    result = subprocess.run(["git", "-C", str(tree), "check-ignore", "--no-index", "--stdin", "-z"], input=payload, capture_output=True)
    hits = {name.decode("utf-8", "surrogateescape").rstrip("/") for name in result.stdout.split(b"\0") if name}
    return result.returncode, {rel for rel in relatives if rel in hits}


def git_ignored(tree: Path, relatives: Sequence[str]) -> set[str]:
    """哪些路径被 .gitignore 规则忽略。

    批量询问时只要有一条路径让 git 直接报错（例如越过了仓库里的符号链接，返回 128），整批都会没有结果；这时逐条重试，
    报错的那条当作“没有被忽略”——它会作为违规写入进入判定，而不是让一条坏路径拖垮整次合并。
    """
    if not relatives:
        return set()
    code, ignored = _check_ignore(Path(tree), relatives)
    if code in (0, 1):
        return ignored
    ignored = set()
    for rel in relatives:
        code, single = _check_ignore(Path(tree), [rel])
        if code in (0, 1):
            ignored |= single
    return ignored


def _top(relative: str, depth: int = 2) -> str:
    parts = relative.split("/")
    return "/".join(parts[:depth]) + ("/" if len(parts) > depth else "")


def _differences(sets: Sequence[set[str]], limit: int = 200) -> dict[str, Any]:
    """不是每次运行都出现的路径，附出现在哪几次（按 raw 顺序的序号）。冷热差异必须逐条评审。"""
    union: set[str] = set().union(*sets) if sets else set()
    partial = sorted(item for item in union if not all(item in each for each in sets))
    return {"count": len(partial), "items": [[item, [index for index, each in enumerate(sets) if item in each]] for item in partial[:limit]]}


def merge_raws(raws: Sequence[Mapping[str, Any]], *, tree: Path, allow_unix: Sequence[str] = (),
               class_prefixes: Sequence[tuple[str, str, str | None]] = (),
               installed_dirs: Sequence[tuple[str, str]] = (), transient: Sequence[str] = ()) -> dict[str, Any]:
    """若干次 run 的 raw 合并成 ``declared-sample/v1``。

    并集语义：仓库内路径取 kinds 并集；仓库外目录取 kinds 并集，计数取各次的最大值（两次运行读的是同一批文件，求和会翻倍）。
    有效性判据一条不放宽：每一条不满足都写进 invalid_reasons（带 [规则] 前缀，便于测试和评审按子串匹配）。

    人工登记项都是可选的，不给就与从前逐字相同；给了就原样留痕在 ``review.allowances``，``draft`` 据此写出草案：

    * ``allow_unix``：允许连接的 AF_UNIX 目标（规范绝对路径，不合规直接拒绝）；
    * ``installed_dirs``：``(前缀, 合同)`` 列表，依赖安装目录（如 ``frontend/node_modules/``，合同 ``cache:node-modules``）。前缀下的
      读取不产生 untracked-read；写入仍按原规则判；前缀下若有已跟踪的路径被读取或枚举，抛 ``SamplerError``（安装目录不能盖住
      已跟踪的输入，与登记侧同一条规则）；
    * ``transient``：命令自产的暂存路径模式（目录前缀或不跨 / 的 glob）。落在其内的未跟踪路径只要本次采样里被写过，写入和读回
      都不判无效；只读不写的不豁免。运行前后 git status 为空的判据照旧——“写完即删”由它兜底。
    """
    tree = Path(tree).resolve()
    if not raws:
        raise SamplerError("至少需要一份 raw")
    # 人工登记项先校验：非法写法在做任何合并工作之前就拒绝。
    allowed_unix = [check_unix_socket(item) for item in dict.fromkeys(allow_unix)]
    installed: dict[str, str] = {}
    for prefix, contract in installed_dirs:
        check_installed_dir(prefix, contract)
        if installed.setdefault(prefix, contract) != contract:
            raise SamplerError(f"--installed-dir 的前缀 {prefix} 登记了两个不同的合同：{installed[prefix]}、{contract}")
    matchers = [PathMatcher(pattern) for pattern in sorted(dict.fromkeys(transient))]
    for index, raw in enumerate(raws):
        problems = validate_raw(raw)
        if problems:
            raise SamplerError(f"第 {index + 1} 份 raw 不合法：{'；'.join(problems[:3])}")
        if raw["run"] is None:
            raise SamplerError(f"第 {index + 1} 份 raw 缺少 run 信息：它不是 run 子命令的产物（没有退出码就无法判断采样是否有效）")
    first = raws[0]
    runs = [raw["run"] for raw in raws]
    reasons: list[str] = []

    def note(rule: str, text: str) -> None:
        item = f"[{rule}] {text}"
        if item not in reasons:
            reasons.append(item)

    # 一致性：同一单元、同一条命令、同一提交、同一别名、同一采样器；任何不一致都说明不是同一次采样会话。
    if any(raw["unit_id"] != first["unit_id"] for raw in raws):
        raise SamplerError("raw 来自不同的单元，不能合并")
    if any(run["command"] != runs[0]["command"] for run in runs):
        raise SamplerError("raw 记录的命令（启动前缀、argv、cwd）不一致，不能合并")
    for key, text in (("commit", "采样提交"), ("kernel", "内核"), ("strace", "strace 版本"), ("sampler_sha256", "采样器摘要")):
        if len({run[key] for run in runs}) > 1:
            note("consistency", f"{text}在各次运行间不一致：{sorted({run[key] for run in runs})}")
    if any(raw["aliases"] != first["aliases"] for raw in raws):
        note("consistency", "各次运行的别名（采样根）不一致，不是同一次采样会话")

    # 并集。
    repo: dict[str, int] = {}
    outside: dict[str, list[int]] = {}
    repo_sets: list[set[str]] = []
    outside_sets: list[set[str]] = []
    for raw in raws:
        repo_sets.append({path for path, _kinds in raw["repo_accesses"]})
        outside_sets.append({key for key, _kinds, _count in raw["outside_accesses"]})
        for path, kinds in raw["repo_accesses"]:
            repo[path] = repo.get(path, 0) | mask_of(kinds)
        for key, kinds, count in raw["outside_accesses"]:
            entry = outside.setdefault(key, [0, 0])
            entry[0] |= mask_of(kinds)
            entry[1] = max(entry[1], count)
    git_commands: dict[tuple[str, ...], None] = {}
    network: dict[tuple[str, str], None] = {}
    for raw in raws:
        for argv in raw["git_commands"]:
            git_commands.setdefault(tuple(argv), None)
        for item in raw["network"]:
            network.setdefault((item["family"], item["target"]), None)

    # tracked 标注。.git 内部不是跟踪文件，由 git_history 规则另行处理。
    files, directories = tracked_sets(tree)

    def is_tracked(path: str) -> bool:
        relative = repo_rel(path)
        if relative == ".git" or relative.startswith(".git/"):
            return False
        return relative in files or relative in directories

    # 依赖安装目录：最长前缀优先（目录自身也算在前缀下）。安装目录只放未跟踪的依赖安装结果，已跟踪的输入必须走仓库范围——前缀下
    # 若有已跟踪的路径被读取或枚举，草案里就会出现落在安装目录下的 range 或单文件，登记侧会拒绝（安装目录的豁免不能吞掉已声明的输入）。
    installed_prefixes = sorted(installed, key=len, reverse=True)

    def installed_owner(relative: str) -> str | None:
        probe = relative + "/"
        return next((prefix for prefix in installed_prefixes if probe.startswith(prefix)), None)

    if installed:
        conflicts = sorted(repo_rel(path) for path, mask in repo.items()
                           if mask & (_READ_EXEC | _LIST) and is_tracked(path) and installed_owner(repo_rel(path)) is not None)
        if conflicts:
            raise SamplerError(f"--installed-dir 的前缀盖住了已跟踪的路径（共 {len(conflicts)} 个被读取或枚举，例如 {'、'.join(conflicts[:5])}）："
                               "安装目录只能放未跟踪的依赖安装结果，已跟踪的输入必须走仓库范围；请收窄前缀或去掉这个选项")

    # 运行信息与流完整性。
    for run, raw in zip(runs, raws):
        label = run["label"]
        if run["timed_out"]:
            note("exit", f"{label} 超时被终止")
        elif run["signal"] is not None:
            note("exit", f"{label} 被信号 {run['signal']} 终止")
        elif run["exit_code"] != 0:
            note("exit", f"{label} 退出码 {run['exit_code']}，不是 0")
        if run["git_status_before"] or run["git_status_after"]:
            note("git-status", f"{label} 运行前后 git status --porcelain --untracked-files=all 非空：前 {len(run['git_status_before'])} 条、"
                               f"后 {len(run['git_status_after'])} 条（命令改了跟踪文件或留下未忽略的新文件）")
        if not raw["coverage"]["stream_complete"]:
            note("stream", f"{label} 的 strace 流不完整")
        if raw["lines"] <= 0:
            note("stream", f"{label} 没有任何 strace 行（strace 没有跟踪到命令）")
    coverage_reasons: dict[str, int] = {}
    unresolved_samples: dict[str, list[str]] = {}
    unfinished_samples: list[str] = []
    # 被丢弃的垃圾行样例：各次运行按顺序合并，相同的行只留一条；副作用类（side_effect_call，按样例文本重新分类）优先留样——副作用类在前、其它在后，
    # 合起来最多 MAX_DETACHED_SAMPLES 条。没有副作用类的行时与从前逐字相同（先到先得、最多 8 条）。
    samples_side: list[str] = []
    samples_other: list[str] = []
    for raw in raws:
        raw_coverage = raw["coverage"]
        for item in raw.get("detached_samples", []):  # 旧 raw 没有这个键，缺省为空
            if item in samples_side or item in samples_other:
                continue
            bucket = samples_side if side_effect_call(item) else samples_other
            if len(bucket) < MAX_DETACHED_SAMPLES:
                bucket.append(item)
        for reason, count in raw_coverage["unresolved_reasons"].items():
            coverage_reasons[reason] = coverage_reasons.get(reason, 0) + count
        # 诊断样例（旧 raw 没有这两个键，缺省为空）：各次运行按顺序合并，相同的行只留一条，上限与 filter 里的一致。
        for reason, items in raw_coverage.get("unresolved_samples", {}).items():
            if reason not in unresolved_samples and len(unresolved_samples) >= MAX_SAMPLE_REASONS:
                continue
            bucket = unresolved_samples.setdefault(reason, [])
            for item in items:
                if item not in bucket and len(bucket) < MAX_SAMPLES_PER_REASON:
                    bucket.append(item)
        for item in raw_coverage.get("unfinished_samples", []):
            if item not in unfinished_samples and len(unfinished_samples) < MAX_UNFINISHED_SAMPLES:
                unfinished_samples.append(item)
    intolerable = {reason: count for reason, count in coverage_reasons.items() if not reason_tolerated(reason)}
    if intolerable:
        text = "存在不在容忍集合内的未解析原因：" + "、".join(f"{reason}×{count}" for reason, count in sorted(intolerable.items()))
        # 把第一条原始行样例拼在后面（整条原因的总长限 INVALID_REASON_CHARS）：光有计数很难定位偶发的原因（lsetxattr 那次就因为
        # 没留样例而无从查起）。原因清单本身不截，样例放不下就少放。
        sampled = next((reason for reason in sorted(intolerable) if unresolved_samples.get(reason)), None)
        if sampled is not None:
            lead = f"；{sampled} 的原始行样例："
            room = INVALID_REASON_CHARS - len("[unresolved] ") - len(text)
            if room >= len(lead) + 20:
                text += (lead + unresolved_samples[sampled][0])[:room]
        note("unresolved", text)
    at_eof = coverage_reasons.get("unfinished-calls-at-eof", 0)
    if at_eof > DECLARED_MAX_UNFINISHED_AT_EOF:
        note("unresolved", f"unfinished-calls-at-eof×{at_eof} 超过容忍上限 {DECLARED_MAX_UNFINISHED_AT_EOF}"
                           "（少量悬着的调用是多线程进程退出时的正常现象，大面积出现说明 strace 流被截断或丢行）")
    # 被丢弃的标记行（裸标记与垃圾行）也有软上限：个位数是 Go 运行时线程被杀或退出的正常现象，大面积出现说明流被截断或大批线程消失。
    detached_total = sum(raw["coverage"]["detached_markers"] for raw in raws)
    if detached_total > DETACHED_MARKER_LIMIT:
        note("detached", f"被丢弃的 strace 标记行（detached、unfinished 裸标记与线程中途消失留下的垃圾行）共 {detached_total} 条，"
                         f"超过容忍上限 {DETACHED_MARKER_LIMIT}（个位数是 Go 运行时线程被杀或退出的正常现象，大面积出现说明 strace 流被截断或大批线程消失，读集可能不完整）")

    # 自产暂存路径（--transient）：命中模式的未跟踪路径，只要本次采样里写过，它的写入和读回都是命令自己的产物，不判无效。
    # 只读不写的路径不在 transient_paths 里，仍按未跟踪读取判——外部放进来的东西不能借模式蒙混。
    transient_hits: dict[str, list[str]] = {matcher.pattern: [] for matcher in matchers}
    transient_read_back: dict[str, int] = {matcher.pattern: 0 for matcher in matchers}
    transient_paths: set[str] = set()
    if matchers:
        for path, mask in repo.items():
            relative = repo_rel(path)
            if not mask & _WRITE or relative == ".git" or relative.startswith(".git/") or is_tracked(path):
                continue
            for matcher in matchers:
                if matcher.matches(relative):
                    transient_paths.add(path)
                    transient_hits[matcher.pattern].append(relative)
                    if mask & _READ_EXEC:
                        transient_read_back[matcher.pattern] += 1

    # 未跟踪读取：命令读了摘要覆盖不到的文件，依赖就没有进输入。例外只有两类：本次采样里命令自己写过的暂存路径读回，
    # 以及登记的依赖安装目录下的读取（内容由对应的 cache 合同绑定）。
    installed_hits: dict[str, list[str]] = {prefix: [] for prefix in installed}
    untracked = []
    for path in sorted(path for path, mask in repo.items()
                       if mask & _READ_EXEC and not is_tracked(path) and not repo_rel(path).startswith(".git/") and repo_rel(path) != ".git"):
        if path in transient_paths:
            continue
        owner = installed_owner(repo_rel(path))
        if owner is not None:
            installed_hits[owner].append(repo_rel(path))
            continue
        untracked.append(path)
    if untracked:
        top: dict[str, int] = {}
        for path in untracked:
            top[_top(repo_rel(path))] = top.get(_top(repo_rel(path)), 0) + 1
        note("untracked-read", f"读取了 {len(untracked)} 个未跟踪路径，主要在：" +
             "、".join(f"{prefix}（{count}）" for prefix, count in sorted(top.items(), key=lambda item: -item[1])[:5]))

    # 网络：只有本机与登记的 AF_UNIX（登记项在函数开头已按登记侧规范校验过）。
    loopback = 0
    network_violations: list[str] = []
    for family, target in sorted(network):
        problem = network_problem(family, target, allowed_unix)
        if problem is not None:
            network_violations.append(problem)
        elif family in ("AF_INET", "AF_INET6"):
            loopback += 1
    if network_violations:
        note("network", "网络目的地不满足“只有本机与登记的 AF_UNIX”：" + "；".join(network_violations[:5]))

    # git 子进程 argv 不可见：读了哪个历史提交说不清。
    hidden = sum(1 for argv in git_commands if argv[1:2] == (GIT_ARGV_HIDDEN,))
    if hidden:
        note("git-argv", f"{hidden} 个 git 子进程的 argv 不可见（strace 没有以 verbose 解码 execve），无法确定读了哪些历史提交；"
                         "检查 strace 选项 -e verbose=execve,execveat 是否生效后重采")

    # 写入集合：仓库内只许落在被忽略的未跟踪路径；仓库外只许落在 performance-cache、scratch，或 /dev/null。
    classifier = Classifier(class_prefixes)
    violations: list[dict[str, str]] = []
    write_candidates: list[str] = []
    for path, mask in sorted(repo.items()):
        if not mask & _WRITE:
            continue
        relative = repo_rel(path)
        if relative == ".git" or relative.startswith(".git/"):
            violations.append({"path": path, "reason": "写入 .git 内部"})
        elif is_tracked(path):
            violations.append({"path": path, "reason": "写入了被跟踪的路径"})
        else:
            write_candidates.append(relative)
    ignored = git_ignored(tree, write_candidates)
    for relative in write_candidates:
        # 被 --transient 命中的暂存路径放行（上面已确认它是未跟踪的、本次采样里写过的）；其余写入必须被 .gitignore 忽略。
        if relative not in ignored and "{TREE}/" + relative not in transient_paths:
            violations.append({"path": "{TREE}/" + relative, "reason": "写入了未被 .gitignore 忽略的未跟踪路径"})
    sinks: list[str] = []
    outside_writes: list[list[str]] = []
    for key in sorted(outside):
        if not outside[key][0] & _WRITE:
            continue
        if key in DECLARED_WRITE_SINKS:
            sinks.append(key)
            continue
        found = classifier.classify(key)
        outside_writes.append([key, found[0] if found else "未归类"])
        if found is None:
            violations.append({"path": key, "reason": "写入了未归类的仓库外路径"})
        elif found[0] not in WRITABLE_CLASSES:
            violations.append({"path": key, "reason": f"写入了 {found[0]} 类路径（只允许 performance-cache、scratch）"})
    if violations:
        note("write", f"{len(violations)} 处写入不在 scratch、performance-cache 或被忽略的路径内，例如：" +
             "；".join(f"{item['path']}（{item['reason']}）" for item in violations[:3]))

    # 组装。
    repo_rows = [[path, kinds_of(repo[path]), is_tracked(path)] for path in sorted(repo)]
    outside_rows = [[key, kinds_of(outside[key][0]), outside[key][1]] for key in sorted(outside)]
    coverage = {
        "stream_complete": all(raw["coverage"]["stream_complete"] for raw in raws),
        "unresolved": sum(coverage_reasons.values()), "unresolved_reasons": dict(sorted(coverage_reasons.items())),
        "unfinished_calls": sum(raw["coverage"]["unfinished_calls"] for raw in raws),
        "unresolved_process_events": sum(raw["coverage"]["unresolved_process_events"] for raw in raws),
        "lines": sum(raw["coverage"]["lines"] for raw in raws),
        "detached_markers": sum(raw["coverage"]["detached_markers"] for raw in raws),
    }
    if unfinished_samples:  # 可选键：登记侧认识它（最多 8 条、每条 ≤300 字符）；没有悬着的调用时不写
        coverage["unfinished_samples"] = unfinished_samples
    exit_code = next((run["exit_code"] for run in runs if run["exit_code"] != 0), 0)
    allowances: dict[str, Any] = {"unix_sockets": allowed_unix,
                                  "class_prefixes": [[prefix, cls, contract] for prefix, cls, contract in class_prefixes]}
    if installed:  # 登记过才写：每个前缀命中的读取数与前几条样例；一条都没命中标 unused，draft 不会把它并进 scope
        allowances["installed_dirs"] = [
            {"prefix": prefix, "contract": installed[prefix], "reads": len(installed_hits[prefix]),
             "sample": installed_hits[prefix][:ALLOWANCE_SAMPLE_PATHS], "unused": not installed_hits[prefix]}
            for prefix in sorted(installed)]
    if matchers:  # 同上：每个模式命中的被写路径数、其中读回的个数、前几条样例；没命中任何被写路径标 unused
        allowances["transient"] = [
            {"pattern": matcher.pattern, "paths": len(transient_hits[matcher.pattern]), "read_back": transient_read_back[matcher.pattern],
             "sample": sorted(transient_hits[matcher.pattern])[:ALLOWANCE_SAMPLE_PATHS], "unused": not transient_hits[matcher.pattern]}
            for matcher in matchers]
    review = {
        "runs": [{"label": run["label"], "cache": run["cache"], "signal": run["signal"], "timed_out": run["timed_out"],
                  "started_utc": run["started_utc"], "finished_utc": run["finished_utc"], "env_overrides": run["env_overrides"],
                  "filter_wait_seconds": run["filter_wait_seconds"], **({"no_go_cache": True} if run.get("no_go_cache") else {})}
                 for run in runs],
        "git_status": [{"label": run["label"], "before": run["git_status_before"], "after": run["git_status_after"]} for run in runs],
        "untracked_reads": {"count": len(untracked), "sample": [repo_rel(path) for path in untracked[:200]],
                            "by_prefix": _prefix_counts(untracked)},
        "writes": {"repo_ignored": sorted({os.path.dirname(rel) + "/" for rel in ignored if os.path.dirname(rel)})[:200],
                   "violations": {"count": len(violations), "items": violations[:200]},
                   "outside": outside_writes[:200], "sinks": sinks},
        "network": {"loopback": loopback, "unix_allowed": [target for family, target in sorted(network)
                                                          if family == "AF_UNIX" and target in allowed_unix],
                    "violations": network_violations[:50]},
        "run_differences": {"repo": _differences(repo_sets), "outside": _differences(outside_sets)},
        "docker": [{"label": run["label"], **run["docker"]} for run in runs if run["docker"] is not None],
        "allowances": allowances,
        "git_argv_hidden": hidden,
    }
    if unresolved_samples:  # 可选键：按原因归类的原始行样例，登记侧不解释 review，所以放这里而不是 sample.coverage
        review["unresolved_samples"] = {reason: unresolved_samples[reason] for reason in sorted(unresolved_samples)}
    detached_samples = (samples_side + samples_other)[:MAX_DETACHED_SAMPLES]
    if detached_samples:  # 可选键：被丢弃的带名字的垃圾行（形态 A、B）的原样样例（副作用类在前），同样放 review
        review["detached_samples"] = detached_samples
    side_effects = sum(raw.get("detached_side_effects", 0) for raw in raws)
    if side_effects:  # 可选键：被丢弃的垃圾行里名字属于副作用类的条数（各次运行求和），只给评审看，不影响有效性
        review["detached_side_effects"] = side_effects
    sample = {
        "schema_version": SAMPLE_SCHEMA, "unit_id": first["unit_id"], "command": dict(runs[0]["command"]),
        "sampled": {"commit": runs[0]["commit"], "utc": max(run["finished_utc"] for run in runs), "kernel": runs[0]["kernel"],
                    "strace": runs[0]["strace"], "sampler_sha256": runs[0]["sampler_sha256"],
                    "runs": [{"cache": run["cache"], "exit_code": run["exit_code"], "lines": raw["lines"], "seconds": run["seconds"]}
                             for run, raw in zip(runs, raws)]},
        "aliases": dict(first["aliases"]), "coverage": coverage, "repo_accesses": repo_rows, "outside_accesses": outside_rows,
        "git_commands": [list(argv) for argv in git_commands],
        "network": [{"family": family, "target": target} for family, target in sorted(network)],
        "exit_code": exit_code, "valid": not reasons, "invalid_reasons": reasons, "review": review,
    }
    problems = validate_sample(sample)
    if problems:
        raise SamplerError("内部错误：合并结果自检不通过：" + "；".join(problems[:3]))
    return sample


def _prefix_counts(untracked: Sequence[str], limit: int = 20) -> list[list[Any]]:
    counts: dict[str, int] = {}
    for path in untracked:
        prefix = _top(repo_rel(path))
        counts[prefix] = counts.get(prefix, 0) + 1
    return [[prefix, count] for prefix, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


# ---------------------------------------------------------------------------
# draft：sample 归并成 scope 草案和人工评审清单
# ---------------------------------------------------------------------------

HIT_RATIO = 0.5         # 目录里被读到的跟踪文件占比达到这个值，整个目录进 include
LEAF_MAX = 20           # 被读过且跟踪文件不超过这个数的目录整体进 include
MAX_RANGES = 12         # 范围数、单文件数超过上限只是标记待评审，草案仍照常输出
MAX_FILES = 40
LIST_LIMIT = 200        # review.md 每节最多逐条列出多少项
EVIDENCE_DIR = "tools/ci/command_input_samples"
ROOT_FILES = "(根目录文件)"
# 登记侧规定：scope.contracts 声明了某个工具链（toolchain:go、toolchain:pnpm），就必须在 repo.files 里单独列出它的依赖锁文件
# （消费者拿安装结果与锁文件交叉核对，聚合在范围摘要里的锁文件没法单独核对）。draft 据此补入，评审清单注明是“由合同要求补入”的。
TOOLCHAIN_LOCK_FILES = {"go": ("backend/go.mod", "backend/go.sum"), "pnpm": ("frontend/pnpm-lock.yaml", "frontend/package.json")}
# 同理，声明了下面这些缓存合同，也必须把它的锁文件作为单文件条目列进 repo.files（WP1e 起登记侧的范围校验强制：消费者核验 lock-derived-pnpm／
# lock-derived-go-sum 时要求锁文件是输入里单独的 ``file:`` 条目，安装结果摘要要等于它；漏列会拖到记录核验才失败）。抄自登记侧
# unit_records._DECLARED_CACHE_CONTRACT_LOCKS：采样器不 import 登记侧的私有常量，两边各自测试同值，改动必须同步评审。draft 的做法与工具链锁文件
# 一致：合同在 scope.contracts 里（含安装目录带来的合同）就补入，文件必须是仓库里跟踪的，不存在或未跟踪的不凭空添加，只在评审清单里标“缺失”。
CACHE_CONTRACT_LOCK_FILES = {"cache:node-modules": "frontend/pnpm-lock.yaml", "cache:go-mod-cache": "backend/go.sum"}
_COMMIT_IN_ARGV = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_LEDGER_COMMIT = re.compile(r'"(?:before_commit|after_commit)"\s*:\s*"([0-9a-f]{40})"')
# 登记侧保留的 range 名：repo.root_listing 为真时输入里多一个叫 scope:root-listing 的条目，所以任何 range 都不能叫 root-listing。
RESERVED_RANGE_NAMES = frozenset({"root-listing"})
# 树根下沿祖先链探测的名字（Go 找模块与工作区、各种版本控制目录）：登记侧按内置规则放行这些“不存在”的探测，不要求任何声明，所以 draft
# 既不为它们补 range，也不为它们写 repo.root_listing。名单与登记侧 unit_records._DECLARED_ANCESTOR_PROBES 一致；万一两边漂移，
# 覆盖自检会把多出来的未覆盖项报出来（该单元转 rerun-only），不会悄悄放行。
ANCESTOR_PROBES = frozenset({"go.mod", "go.work", ".hg", ".svn", ".bzr", ".fossil-settings"})
# 覆盖自检不通过时 rerun-only 原因的固定开头：总览统计和测试都按它识别“因覆盖自检转 rerun-only”的条目。
COVERAGE_REASON_PREFIX = "声明级覆盖自检不通过"
# 覆盖闭合只处理这几种访问类型的仓库内路径（missing 探测、被 stat／dir 的目录）；读、写、执行、枚举有各自的归并或判无效规则。
_PROBE_KINDS = frozenset({"missing", "stat", "dir"})
_RANGE_NAME_UNSAFE = re.compile(r"[^a-z0-9._-]")


def range_name(directory: str) -> str:
    """目录 → range 名。登记侧要求名字只含小写字母、数字、点、下划线、连字符，且以字母或数字开头（大写字母、@、空格、前导点都会让整份
    scope 被拒绝）：先转小写，斜杠和其它不合规的字符换成连字符，再去掉开头的点、下划线、连字符；去空了就叫 dir。已经合规的目录
    名（docs/egress/foundation → docs-egress-foundation）结果与从前逐字相同。"""
    return _RANGE_NAME_UNSAFE.sub("-", directory.lower()).lstrip("._-") or "dir"


def name_ranges(directories: Sequence[str]) -> list[str]:
    """给一组 include 目录（已按路径排序）逐个起名；重名加数字后缀，保留名不用。"""
    names: list[str] = []
    for directory in directories:
        base, name, number = range_name(directory), range_name(directory), 2
        while name in names or name in RESERVED_RANGE_NAMES:
            name, number = f"{base}-{number}", number + 1
        names.append(name)
    return names


def directory_totals(files: Iterable[str]) -> dict[str, int]:
    """每个目录（含根的空串）下的跟踪文件总数。"""
    totals: dict[str, int] = {}
    for name in files:
        parent = os.path.dirname(name)
        while True:
            totals[parent] = totals.get(parent, 0) + 1
            if not parent:
                break
            parent = os.path.dirname(parent)
    return totals


def derive_repo_scope(sample: Mapping[str, Any], files: set[str], totals: Mapping[str, int],
                      skip_listed: Any = None) -> dict[str, Any]:
    """仓库内路径归并成 ranges 与 files。纯机械：

    从根往下逐层看每个目录 D：被 list 枚举（枚举结果依赖目录项集合，目录里增删文件命令行为就可能变）、或被读过且跟踪文件
    不超过 LEAF_MAX、或被读到的跟踪文件占比达到 HIT_RATIO，就整个 D 进 include，不再下钻；否则下钻到有读取的子目录，
    D 下直接被读的文件进 files。根目录永不整体 include（那等于重新声明整个仓库）。过宽与否不在这里判断，度量全部进评审清单。

    ``skip_listed``（可选，相对路径 → 布尔）：被枚举但不该成为范围的目录。依赖安装目录（node_modules）与自产暂存目录里的未跟踪
    目录被枚举时，登记侧另有规则放行（安装目录读取不要求已跟踪、暂存目录里自己写过的路径可以读回），把它们写成 range 反而会被
    登记侧拒绝（安装目录前缀不得包含任何 range），所以不进 listed。不给就与从前逐字相同。
    """
    reads: set[str] = set()
    listed: set[str] = set()
    for path, kinds, _tracked in sample["repo_accesses"]:
        relative, mask = repo_rel(path), mask_of(kinds)
        if relative == ".git" or relative.startswith(".git/"):
            continue  # .git 内部由 git_history 规则处理，不进 ranges
        if mask & _READ_EXEC and relative in files:
            reads.add(relative)
        if mask & _LIST and not (skip_listed is not None and skip_listed(relative)):
            listed.add(relative)
    dir_read: dict[str, int] = {}
    direct: dict[str, list[str]] = {}
    for relative in reads:
        direct.setdefault(os.path.dirname(relative), []).append(relative)
        parent = os.path.dirname(relative)
        while True:
            dir_read[parent] = dir_read.get(parent, 0) + 1
            if not parent:
                break
            parent = os.path.dirname(parent)
    interesting = set(dir_read)
    for directory in listed:
        while True:
            interesting.add(directory)
            if not directory:
                break
            directory = os.path.dirname(directory)
    children: dict[str, set[str]] = {}
    for directory in interesting:
        if directory:
            children.setdefault(os.path.dirname(directory), set()).add(directory)
    include: list[str] = []
    picked: list[str] = []

    def decide(directory: str) -> None:
        total, read = totals.get(directory, 0), dir_read.get(directory, 0)
        if directory and (directory in listed or (read and total <= LEAF_MAX) or (total and read / total >= HIT_RATIO)):
            include.append(directory)
            return
        for child in sorted(children.get(directory, ())):
            decide(child)
        picked.extend(sorted(direct.get(directory, ())))

    decide("")
    include.sort()  # 登记表要确定性：范围与单文件都按路径排序，不依赖遍历顺序
    picked.sort()
    names = name_ranges(include)  # 名字按登记侧的写法规范化（大写、@、前导点等不合规字符不会再让整份 scope 被拒绝）
    return {"ranges": [{"name": name, "include": [directory + "/"], "exclude": []} for name, directory in zip(names, include)],
            "files": picked, "reads": len(reads), "listed": sorted(listed), "include_dirs": include, "dir_read": dir_read,
            "read_files": reads}


# ---------------------------------------------------------------------------
# 覆盖闭合：让起草的 scope 通过登记侧的覆盖检查
# ---------------------------------------------------------------------------
#
# derive_repo_scope 只为“读到的跟踪文件”和“被枚举的目录”声明范围。可是登记侧的覆盖检查（unit_records.declared_coverage）还要求：
#   * 仓库内的 missing 探测（命令找一个不存在的文件，例如 TypeScript 的模块解析候选、eslint 的配置候选）必须落在某个 range 里——
#     “它不存在”本身就是输入：将来有人把它建出来，命令行为就变；range 的摘要覆盖目录下全部跟踪文件，正好绑定这件事；
#   * 只被 stat／dir 的仓库内目录，必须是已声明内容的上级目录（有 range 或单文件在它下面），否则要被某个 range 覆盖；
#   * 树根目录自身被枚举或读取（make 在树根找隐含规则，pnpm 向上找工作区根），以及树根直接子项的 missing 探测，登记侧用
#     ``repo.root_listing`` 绑定（树根永远不能成为 range：那等于重新声明整个仓库）。
# 下面的函数机械地把这些补齐（close_repo_scope），并让 repo_ignored 不盖住安装目录（build_repo_ignored）；补不齐的（例如被 stat 的已跟踪
# 文件）留给覆盖自检报告，不去猜。


def _ancestor_dirs(path: str, *, itself: bool) -> set[str]:
    """路径的各级上级目录（仓库相对、无尾斜杠）；``itself`` 为真时路径自身（目录）也算。与登记侧 add_ancestors 同构。"""
    parts = path.rstrip("/").split("/")
    return {"/".join(parts[:count]) for count in range(1, len(parts) + (1 if itself else 0))}


def declared_directory_set(include_dirs: Iterable[str], files: Iterable[str], ignored_items: Iterable[str],
                           installed_prefixes: Iterable[str]) -> set[str]:
    """“声明内容的上级目录”集合：只被 stat／dir 的访问落在这些目录上，登记侧算覆盖通过（与仓库内“声明内容的上级目录只看元数据”同构）。
    来源：range 的 include（含自身）、单文件的各级父目录、repo_ignored 的目录前缀（含自身）与 glob 的字面目录部分、安装目录前缀（含自身）。"""
    directories: set[str] = set()
    for directory in include_dirs:
        directories |= _ancestor_dirs(directory + "/", itself=True)
    for path in files:
        directories |= _ancestor_dirs(path, itself=False)
    for item in ignored_items:
        if item.endswith("/"):
            directories |= _ancestor_dirs(item, itself=True)
            continue
        literal: list[str] = []  # glob 的目录部分里，第一个带通配符的分量之前的字面目录
        for part in item.split("/")[:-1]:
            if "*" in part or "?" in part:
                break
            literal.append(part)
        if literal:
            directories |= _ancestor_dirs("/".join(literal), itself=True)
    for prefix in installed_prefixes:
        directories |= _ancestor_dirs(prefix, itself=True)
    return directories


def _ignored_matcher(item: str) -> PathMatcher | None:
    """repo_ignored 一项的匹配器；写法非法的（含方括号之类，登记侧也会拒绝）返回 None，交给覆盖自检去报，不在这里抛异常。"""
    try:
        return PathMatcher(item)
    except SamplerError:
        return None


def close_repo_scope(accesses: Sequence[Sequence[Any]], *, tracked: set[str], include_dirs: Sequence[str], declared_files: Iterable[str],
                     ignored_items: Sequence[str], installed_prefixes: Sequence[str]) -> dict[str, Any]:
    """按上面的三条补齐 range 与 ``root_listing``。纯函数，只读采样的仓库内访问。返回::

        {"extra": [补入的 range 目录（无尾斜杠，排序）], "why": {目录: {"missing": [探测路径…], "stat": [被 stat 的目录…]}},
         "root_listing": 是否要写 repo.root_listing, "root_listed": 树根被枚举或读取, "root_missing": [树根直接子项的 missing 探测…]}

    * **missing 探测**：路径所在的最近的、不是树根的父目录补一个 range（``node_modules/@types`` 补 ``node_modules/``）；目录不存在
      或下面没有跟踪文件也照补——range 的摘要正是“这个目录下没有（这些）跟踪文件”的绑定。已被现有 range 或单文件覆盖的不重复补，
      浅层目录先补（它常常已经盖住深层目录的探测）。树根直接子项的 missing 探测改走 ``root_listing``；``go.mod`` 一类祖先探测名
      （``ANCESTOR_PROBES``）登记侧本来就放行，不补也不触发。
    * **只被 stat／dir 的目录**（没有读、没有枚举、没有写）：它既不是已声明内容的上级目录，也不在任何 range 里，就为**目录自身**补一个
      range（``docs/legal``、``src/__tests__``）；深层的先补，补了之后它的上级目录就成了“已声明内容的上级”，不必再为它们补。
      被 stat 的已跟踪**文件**不处理（单文件条目是“读过”的语义，宁可让覆盖自检报出来）。
    * **树根**：``{TREE}`` 自身的访问类型含 list 或 read（且只有 stat／dir／list／read），或树根直接子项有 missing 探测 → ``root_listing``。

    ``.git`` 下的路径（走 git_history）、安装目录前缀与 repo_ignored 命中的路径（登记侧另有放行规则）一概不碰。
    """
    files_declared = set(declared_files)
    matchers = [matcher for matcher in (_ignored_matcher(item) for item in ignored_items) if matcher is not None]
    dirs: list[str] = list(include_dirs)

    def excluded(relative: str) -> bool:
        return (any((relative + "/").startswith(prefix) for prefix in installed_prefixes)
                or any(matcher.matches(relative) for matcher in matchers))

    def covered(relative: str) -> bool:
        return relative in files_declared or any(relative == directory or relative.startswith(directory + "/") for directory in dirs)

    root_listed = False
    root_missing: list[str] = []
    missing_by_parent: dict[str, list[str]] = {}
    stat_only: list[str] = []
    for path, raw_kinds, _tracked in accesses:
        kinds = set(raw_kinds)
        if path == "{TREE}":
            root_listed = bool(kinds & {"list", "read"}) and kinds <= {"stat", "dir", "list", "read"}
            continue
        if not path.startswith("{TREE}/") or path.endswith("/"):
            continue  # {TREE}/.git/objects/ 这个聚合键不在这里处理
        relative = path[len("{TREE}/"):]
        if relative == ".git" or relative.startswith(".git/") or not kinds <= _PROBE_KINDS or excluded(relative) or covered(relative):
            continue
        if "missing" in kinds:
            if relative in ANCESTOR_PROBES:
                continue  # 名字里没有斜杠，只会出现在树根下；登记侧放行纯 missing 的，混合类型的它也不放行，由覆盖自检去报
            parent = os.path.dirname(relative)
            if parent:
                missing_by_parent.setdefault(parent, []).append(relative)
            elif kinds == {"missing"}:
                root_missing.append(relative)
        else:
            stat_only.append(relative)

    why: dict[str, dict[str, list[str]]] = {}
    for parent in sorted(missing_by_parent, key=lambda item: (item.count("/"), item)):
        if not covered(parent):
            dirs.append(parent)
            why[parent] = {"missing": [], "stat": []}
    declared = declared_directory_set(dirs, files_declared, ignored_items, installed_prefixes)
    for relative in sorted(set(stat_only), key=lambda item: (-item.count("/"), item)):
        if covered(relative) or relative in tracked or relative in declared:
            continue
        dirs.append(relative)
        declared |= _ancestor_dirs(relative + "/", itself=True)
        why[relative] = {"missing": [], "stat": []}
    # 每个探测路径归到第一个盖住它的补入 range 名下，评审清单据此说明“为什么补这个目录”。
    for relative in sorted(item for paths in missing_by_parent.values() for item in paths):
        owner = next((directory for directory in why if relative.startswith(directory + "/")), None)
        if owner is not None:
            why[owner]["missing"].append(relative)
    for relative in sorted(set(stat_only)):
        if relative in why:
            why[relative]["stat"].append(relative)
    return {"extra": sorted(why), "why": why, "root_listing": root_listed or bool(root_missing), "root_listed": root_listed,
            "root_missing": sorted(root_missing)}


def build_repo_ignored(written_ignored: Iterable[str], written_untracked: Iterable[str], transient: Iterable[str],
                       installed_prefixes: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """``repo_ignored`` 草案：被 .gitignore 忽略的写入路径所在目录，加上命中过的 --transient 模式。返回 ``(条目, 改用精确路径的记录)``。

    目录前缀不得与依赖安装目录前缀重叠或包含它（登记侧的覆盖检查里 repo_ignored 先于范围判定：一个盖住 ``frontend/node_modules/`` 或
    ``frontend/`` 的输出区，会把安装目录、乃至 range 里已跟踪的输入统统吞掉）。目录自身等于安装前缀、是它的上级、或写入发生在树根下
    （没有可用的目录）时，改用被写文件的**精确路径**（不含通配符的项就是精确路径）；目录严格位于安装前缀之内的（``node_modules/.vite/``）
    照旧用目录前缀——那正是登记侧允许的“安装目录里的输出区”。--transient 的目录模式盖住安装前缀时同样改成命中它的被写路径。
    ``(原目录, 精确路径)`` 记录供评审清单说明为什么这些项是精确路径。"""
    def overlaps(directory: str) -> bool:
        return not directory or any(prefix.startswith(directory) for prefix in installed_prefixes)

    items: set[str] = set()
    exact: list[tuple[str, str]] = []
    for relative in sorted(written_ignored):
        parent = os.path.dirname(relative)
        directory = parent + "/" if parent else ""
        if overlaps(directory):
            items.add(relative)
            exact.append((directory or "(树根)", relative))
        else:
            items.add(directory)
    untracked = sorted(written_untracked)
    for pattern in sorted(set(transient)):
        if pattern.endswith("/") and overlaps(pattern):
            for relative in untracked:
                if (relative + "/").startswith(pattern):
                    items.add(relative)
                    exact.append((pattern, relative))
        else:
            items.add(pattern)
    # 本函数改用的精确路径，若已被另一项目录前缀盖住（目录前缀连目录自身也算）就是多余的：连同记录一起去掉。
    directories = [item for item in items if item.endswith("/")]
    swapped = {path for _directory, path in exact}
    items = {item for item in items if item not in swapped or not any((item + "/").startswith(directory) for directory in directories)}
    return sorted(items), [(directory, path) for directory, path in exact if path in items]


def rollup(read_files: Iterable[str], depth: int) -> list[tuple[str, int]]:
    """被读到的跟踪文件按前 depth 层目录汇总（目录, 被读文件数），被读多的在前；根目录下的文件归入“(根目录文件)”。

    真实读集常常横跨很多目录（backend 全量 go test 实测会读 frontend、docs、deploy 等处几千个文件），机械归并出的范围会很碎，
    评审者要决定合并到多粗，所以评审清单里附这张纯度量表。
    """
    counts: dict[str, int] = {}
    for relative in read_files:
        directory = os.path.dirname(relative)
        key = "/".join(directory.split("/")[:depth]) if directory else ROOT_FILES
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def covering_include(directory: str, include: Sequence[str]) -> str | None:
    for item in include:
        if directory == item or directory.startswith(item + "/"):
            return item
    return None


def scan_ledger_commits(tree: Path, limit_files: int = 5000) -> set[str]:
    """扫描 docs/egress 下台账里的 before_commit 与 after_commit，供与采样到的 git 调用交叉核对（只提示，不自动并入）。"""
    root = Path(tree) / "docs" / "egress"
    commits: set[str] = set()
    if not root.is_dir():
        return commits
    for count, path in enumerate(sorted(root.rglob("*.json"))):
        if count >= limit_files:
            break
        try:
            if path.stat().st_size <= 8 * 1024 * 1024:
                commits.update(_LEDGER_COMMIT.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return commits


def _is_ancestor_probe(key: str, kinds: Sequence[str], prefixes: Sequence[str]) -> bool:
    """仅元数据（stat、dir、missing）的上层目录探测：例如 PATH 搜索会探测 /、/usr/；仍列出评审，但不算“未归类”。"""
    if set(kinds) - {"stat", "dir", "missing"}:
        return False
    probe = key if key.endswith("/") else key + "/"
    return key == "/" or any(prefix.startswith(probe) for prefix in prefixes)


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _quote(text: Any) -> str:
    """放进行内代码段的样例原文：转义竖线与换行，反引号换成单引号（原文里夹着反引号会提前截断代码段）。"""
    return _md(text).replace("`", "'")


def _items(lines: list[str], items: Sequence[Any], render: Any, total: int | None = None) -> None:
    for item in items[:LIST_LIMIT]:
        lines.append("- " + render(item))
    total = len(items) if total is None else total
    if total > LIST_LIMIT:
        lines.append(f"- ……共 {total} 项，只列前 {LIST_LIMIT} 项（完整内容见证据文件）")


# ---------------------------------------------------------------------------
# 覆盖自检：draft 起草完 scope，用登记侧自己的覆盖检查核对一遍
# ---------------------------------------------------------------------------
#
# 起草靠一条条机械规则，登记侧的覆盖检查（unit_records.declared_coverage）又有它自己的一套放行规则，两边只要有一条对不上，草案就会在
# 登记时才被拒绝——而且往往要靠人逐条发现。这里让 draft 在写出 declared 条目之前，把起草好的 scope 连同 sample 原样交给登记侧的同一个
# 函数核对：通过才写 declared；不通过就写成 rerun-only，原因里带规则统计和前几处，评审清单列出全部未覆盖项。这样 draft 产出的 declared
# 条目必然能通过登记时的覆盖检查，不再靠人工发现。
#
# 登记侧的模块与本文件同目录（驱动目录里一起部署），按文件路径加载、只读使用；找不到、加载失败或没有这两个函数（登记侧还没合进这棵树）
# 时跳过自检并在评审清单里注明，条目保持原状。

_UNIT_RECORDS: tuple[Any, str] | None = None


def unit_records_module() -> tuple[Any, str]:
    """按文件路径加载同目录的 unit_records.py，返回 ``(模块, 说明)``：不可用时模块为 None，说明写明原因。结果缓存在进程内。

    和 ``read_audit_module`` 一样强制不写字节码（在驱动目录或测试树里留下 __pycache__ 会让门禁前置检查直接退出）；模块要先放进
    ``sys.modules`` 才能 exec（它的 dataclass 要按模块名查自己）。需要 ``declared_coverage`` 与 ``validate_declared_scope`` 两个函数。"""
    global _UNIT_RECORDS
    if _UNIT_RECORDS is None:
        path = HERE.with_name("unit_records.py")
        name = "_declared_sampler_unit_records"
        module, note = None, ""
        spec = importlib_util.spec_from_file_location(name, path) if path.is_file() else None
        if spec is None or spec.loader is None:
            note = f"同目录没有 {path.name}"
        else:
            candidate = importlib_util.module_from_spec(spec)
            previous = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                sys.modules[name] = candidate
                spec.loader.exec_module(candidate)
            except Exception as error:  # 任何加载失败都只是“自检不可用”，不能让 draft 本身崩掉
                sys.modules.pop(name, None)
                note = f"加载 {path.name} 失败：{type(error).__name__}：{error}"
            else:
                if all(callable(getattr(candidate, attr, None)) for attr in ("declared_coverage", "validate_declared_scope")):
                    module = candidate
                else:
                    sys.modules.pop(name, None)
                    note = f"{path.name} 里没有 declared_coverage 与 validate_declared_scope（登记侧的覆盖检查还没有合进这棵树）"
            finally:
                sys.dont_write_bytecode = previous
        _UNIT_RECORDS = (module, note)
    return _UNIT_RECORDS


def coverage_self_check(sample: Mapping[str, Any], scope: Mapping[str, Any]) -> dict[str, Any]:
    """用登记侧的 ``declared_coverage(sample, scope, command_sha256=…)`` 核对起草的 scope。返回 ``{"state": …}``：

    * ``passed``：通过，带登记侧返回的计数 ``counts``；
    * ``failed``：不通过，带 ``total``（未覆盖总数）、``findings``（登记侧返回的明细，最多 200 条，按规则、路径排序）、``counts``；
    * ``skipped``：登记侧模块不可用，带 ``note``；
    * ``error``：核对函数自己抛了异常，带 ``note``——无法证明覆盖，调用方按不通过处理（宁可 rerun-only，不能带着未核对的 scope 写 declared）。

    采样证据原样传入（登记侧只读它，不改）；命令摘要用证据自己的 ``command.sha256``，与写进登记表的 ``command_sha256`` 同一个值。"""
    module, note = unit_records_module()
    if module is None:
        return {"state": "skipped", "note": note}
    try:
        report = module.declared_coverage(sample, scope, command_sha256=sample["command"]["sha256"])
    except Exception as error:  # 核对函数自己出错：无法证明覆盖，由调用方按不通过处理
        return {"state": "error", "note": f"{type(error).__name__}：{error}"}
    counts = dict(report.get("counts", {}))
    if report["ok"]:
        return {"state": "passed", "counts": counts}
    return {"state": "failed", "total": counts.get("undeclared", len(report["undeclared"])), "findings": list(report["undeclared"]), "counts": counts}


def _finding_text(item: Mapping[str, Any], limit: int = 160) -> str:
    """一条未覆盖项压成一行：规则、路径、访问类型。路径很长时截断（go 构建缓存的路径一百多字符）。"""
    text = f"{item['rule']} {item['path']} [{','.join(item['kinds'])}]" if item["path"] else f"{item['rule']} {item['reason']}"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def coverage_failure_reason(check: Mapping[str, Any]) -> str:
    """覆盖自检不通过时写进登记表的 rerun-only 原因：未覆盖总数、规则统计、前 3 处。"""
    findings = check["findings"]
    stats: dict[str, int] = {}
    for item in findings:
        stats[item["rule"]] = stats.get(item["rule"], 0) + 1
    stat_text = "、".join(f"{rule}×{count}" for rule, count in sorted(stats.items()))
    if check["total"] > len(findings):
        stat_text += f"，登记侧只返回前 {len(findings)} 处明细，统计按这些算"
    shown = "；".join(_finding_text(item) for item in findings[:3])
    return f"{COVERAGE_REASON_PREFIX}：{check['total']} 处未覆盖（规则统计 {stat_text}；前 3 处：{shown}）"


# 覆盖检查返回的计数里，通过时写进评审清单的几项（登记侧版本不同，计数键可能多可能少，没有的就不写）。
_COVERAGE_COUNT_LABELS = (("repo_paths", "仓库内路径"), ("outside_paths", "仓库外目录"), ("exempt_paths", "祖先探测等豁免"), ("ignored_paths", "输出区路径"),
                          ("installed_paths", "安装目录路径"), ("root_listing_paths", "树根枚举与探测"), ("git_paths", ".git 路径"),
                          ("git_commands", "git 命令"), ("network", "网络目的地"))


def coverage_review_lines(check: Mapping[str, Any]) -> list[str]:
    """评审清单第 13 节（覆盖自检）的正文：通过、不通过（按规则分组列出全部明细）、未执行、出错、被关闭、不需要，各一种写法。"""
    state = check["state"]
    if state == "off":
        return ["- 覆盖闭合与覆盖自检已被 `--no-coverage-check` 关闭：草案只按读集归并，没有为 missing 探测和只被 stat 的目录补 range，也没有用登记侧的覆盖检查核对；"
                "登记前必须自己核对。"]
    if state == "not-needed":
        return ["- 本单元的采样无效或 git 用法未固定，登记状态草案已是 `rerun-only`，没有做覆盖自检。"]
    if state == "skipped":
        return [f"- **覆盖自检未执行**：{_md(check['note'])}；条目保持原状，登记前必须用登记侧的覆盖检查（unit_records.declared_coverage）自己核对一遍。"]
    if state == "error":
        return [f"- **覆盖自检无法执行**：核对函数抛出异常（{_md(check['note'])}），无法证明声明范围覆盖了采样的读集，登记状态草案已改为 `rerun-only`。"]
    if state == "passed":
        counts = check["counts"]
        shown = "、".join(f"{label} {counts[key]}" for key, label in _COVERAGE_COUNT_LABELS if key in counts)
        return [f"- **覆盖自检通过**：起草的 scope 通过了登记侧的覆盖检查（unit_records.declared_coverage）" + (f"：{shown}。" if shown else "。")]
    findings = check["findings"]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in findings:
        groups.setdefault(item["rule"], []).append(item)
    lines = [f"- **覆盖自检不通过**：共 {check['total']} 处未覆盖，登记状态草案已改为 `rerun-only`。规则统计："
             + "、".join(f"`{rule}`×{len(items)}" for rule, items in sorted(groups.items()))]
    if check["total"] > len(findings):
        lines.append(f"- 登记侧只返回前 {len(findings)} 处明细（总数 {check['total']}），规则统计和下面的清单都只按这些算。")
    for rule in sorted(groups):
        items = groups[rule]
        reasons = sorted({item["reason"] for item in items})
        lines.append(f"- `{_md(rule)}` ×{len(items)}" + (f"：{_md(reasons[0])}" if len(reasons) == 1 else ""))
        for item in items[:LIST_LIMIT]:
            where = f"`{_quote(item['path'])}` {','.join(item['kinds'])}" if item["path"] else "（无路径）"
            lines.append(f"  - {where}" + ("" if len(reasons) == 1 else f"：{_md(item['reason'])}"))
        if len(items) > LIST_LIMIT:
            lines.append(f"  - ……共 {len(items)} 项，只列前 {LIST_LIMIT} 项")
    lines.append("- 处理方向（按规则前缀，各条的具体理由见上）：`C0-` 起草的 scope 本身过不了登记侧的校验（缺依赖锁文件、登记侧不认的键……），先修它；"
                 "`C2-`／`C3-` 里的仓库外路径多半是内置分类表没有覆盖的前缀，确认可以接受后用 `draft --class-prefix 前缀=类别[:合同]` 登记再起草；"
                 "仓库内的 `C1-`／`C3-` 看上面的路径，最常见的是只被 stat 的已跟踪文件（覆盖闭合不替它声明，要人工决定）；`C7-` 是采样证据本身不合格，需要重新采样。")
    return lines


# ---------------------------------------------------------------------------
# git 用法是否“固定”：不固定的单元不做声明级承接
# ---------------------------------------------------------------------------
#
# 声明级承接的前提是命令的结果只取决于范围内的文件内容。命令里的 git 子进程若按 HEAD、分支、标签、远端引用寻址，结果就随
# 仓库状态变化（HEAD 一变，“哪个提交是祖先”的答案就变），而 scope 的 repo.head 只能是 false——读 HEAD 的单元登记为
# rerun-only 并保留 head 输入。采样器看不出 git 命令作用在哪个仓库（测试常在临时合成仓库里造提交、打标签），所以一律按字面判：
#
#   固定＝所有修订参数都是完整 40 位小写十六进制提交号（可带 ``:路径``、``^{tree}``、``^{commit}`` 后缀），且子命令属于只读、
#   按对象寻址的一类：show、cat-file、rev-parse（选项只有 --verify、-q，修订都固定；--show-toplevel、--abbrev-ref 这类问仓库
#   状态的用法不在其中）、merge-base、ls-tree、diff-tree，以及 log／rev-list（给的范围两端也都得是完整提交号）。
#   其余一律“未固定”：修订参数是 HEAD、refs/…、origin、main、FETCH_HEAD、含 @{ 的写法、分支名、标签名、
#   缩写或相对写法（HEAD~1、<提交>^）；子命令是 tag、worktree、branch、for-each-ref、ls-remote、remote、config、status、stash
#   等读引用库或写仓库状态的一类；用了 -C、-c、--git-dir 这类会换仓库或改行为的全局选项；放行清单之外的选项（例如 --all、
#   --stdin、--since 会把引用库或标准输入、当前时间带进来）；没给修订（缺省落到 HEAD）；参数不可见。拿不准一律算“未固定”——
#   误判的代价只是该单元保持 rerun-only（每次重跑），漏判的代价是登记表自相矛盾。
#
# 规格 v2（步骤 5 WP4d；独立验收 #3、#4 之后调度者统一的口径。登记侧 unit_records._declared_git_usage_problem 按同一份规格改，采样器与登记侧各抄一份常量、
# 不互相 import，由测试交叉核对；采样器这一侧的期望值写在 test_ci_declared_sampler_git_spec 里）相对此前的变化：
#   1. 选项表里没有 --oneline（show、log）：它等于 --pretty=oneline --abbrev-commit，输出里的缩写长度随对象库大小与 core.abbrev 配置变化。
#   2. --format=／--pretty= 的取值只放行两种：精确的 ``raw``，以及“普通字符＋白名单占位符”的自定义格式串（见 git_format_problem）；其余一律不固定。
#   3. merge-base、cat-file、rev-parse 的 argv 里出现 ``--`` 一律不固定：它在这三个子命令里只是“选项结束”，其后仍是修订（实测
#      ``git merge-base A B -- HEAD`` 的输出随 HEAD 变化）；show、log、rev-list、ls-tree、diff-tree 里 ``--`` 之后才是路径（沿用）。
#   4. 所有“数字”判定（-n <数字>、-<数字>、--max-count=<数字>）一律 ASCII 数字（isascii() 且 isdigit()），拒绝 ``²`` 之类的 Unicode 数字。
#   5. ls-tree 的 --abbrev=<n> 去掉（WP1g）：<n> 只是缩写长度的下限，对象库里出现碰撞时输出会更长；拿不准一律算不固定，没有任何已登记单元用它。
# 已知残余：log／show 默认会显示 refs/notes/commits 里的 notes（规格不要求 --no-notes）；notes 在被测仓库里极少见，写进“放宽项汇总”那一类。

# 一个固定的修订：完整 40 位小写十六进制提交号，可带 ^{tree}、^{commit} 或 :路径（路径是对象内的相对路径，里面出现 HEAD 之类的字样不算引用）。
_GIT_REV = re.compile(r"[0-9a-f]{40}(?:\^\{(?:tree|commit)\}|:.*)?", re.S)
# log／rev-list 范围两端：完整提交号（可带 ^{commit}）。
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}(?:\^\{commit\})?")
_GIT_OBJECT_TYPES = frozenset({"blob", "tree", "commit", "tag"})
# 子命令名之前允许出现的全局选项：只有这两个不改变命令作用的仓库与结果。
_GIT_PLAIN_GLOBALS = frozenset({"--no-pager", "--no-optional-locks"})
# 这三个子命令里 ``--`` 只是“选项结束”，其后的参数仍是修订（不是路径分隔）：argv 里出现 ``--`` 一律不固定。
_GIT_DASHDASH_REVISION_SUBCOMMANDS = frozenset({"merge-base", "cat-file", "rev-parse"})
# 取值必须是数字的选项前缀（log／rev-list 的 --max-count=；ls-tree 的 --abbrev= 已在 WP1g 去掉）。
_GIT_NUMERIC_PREFIXES = ("--max-count=",)
# 子命令 -> (放行的选项（精确匹配）, 放行的选项前缀, 修订参数的处理方式)。不在表里的子命令、选项一律未固定。
#   all：``--`` 之前的位置参数全是修订，至少一个；first：只有第一个位置参数是修订，后面是路径（ls-tree）；
#   range：log、rev-list，位置参数是固定提交、两端都是完整提交号的 A..B／A...B、或 ^固定提交，至少一个正向修订。
_GIT_FIXED_SUBCOMMANDS: dict[str, tuple[frozenset[str], tuple[str, ...], str]] = {
    "show": (frozenset({"-s", "--no-patch", "--quiet", "--stat", "--name-only", "--name-status", "--no-color", "-z"}),
             ("--format=", "--pretty="), "all"),
    "cat-file": (frozenset({"-t", "-s", "-e", "-p"}), (), "all"),
    "rev-parse": (frozenset({"--verify", "-q", "--quiet"}), (), "all"),
    "merge-base": (frozenset({"--is-ancestor", "--all"}), (), "all"),
    "ls-tree": (frozenset({"-r", "-d", "-t", "-l", "--long", "--name-only", "--name-status", "--full-name", "--full-tree", "-z"}),
                (), "first"),
    "diff-tree": (frozenset({"-r", "-t", "-z", "-M", "-C", "--root", "--no-commit-id", "--name-only", "--name-status", "--raw", "--stat",
                             "--no-renames", "--find-renames", "--quiet"}), (), "all"),
    "log": (frozenset({"--parents", "--no-walk", "--reverse", "--topo-order", "--date-order", "--first-parent", "--no-merges",
                       "--name-only", "--name-status", "--stat", "--quiet", "--no-color", "--count", "-z"}),
            ("--format=", "--pretty=", "--max-count="), "range"),
    "rev-list": (frozenset({"--parents", "--reverse", "--topo-order", "--date-order", "--first-parent", "--no-merges", "--count", "--quiet",
                            "--objects", "--not", "-z"}), ("--max-count=",), "range"),
}


# --format=／--pretty= 的取值（规格 v2 §2）只放行两种，其余一律不固定：
#   1. 精确的 ``raw``（--pretty=raw、--format=raw）；
#   2. 自定义格式串：``format:<串>``、``tformat:<串>``，或不带前缀但含 ``%`` 的值（git 把它当隐含的 tformat；--format= 与 --pretty= 在 git 里走同一个解析，
#      所以两个选项的取值规则相同）。``<串>`` 里允许普通字符（任意不含 ``%`` 的字符：空格、字母、标点……），和下面白名单里的占位符；不在表里的 ``%…`` 序列一律不固定：
#        %H（完整提交号） %T（完整树号） %P（完整父提交号） %an %ae %aI（严格 ISO 作者日期） %at（作者时间戳） %cn %ce %cI %ct %s %b %B %n %%，
#        以及 %x 加恰好两位十六进制数字（如 %x00）。
#      明确禁止：%D %d（带出 HEAD／分支／标签）、%N（notes）、%S、%g*（reflog）、%(…) 全部、%h %t %p（缩写长度随对象库大小变）、%ad %cd %aD %cD（随 log.date
#      配置变）、%ar %cr（相对当前时间）、%aN %aE %cN %cE（读 .mailmap）、%f %e %G* %C %w %< %> %m %+ %- 等——凡不在白名单里的都不固定。
# 不含 % 又没有 format:／tformat: 前缀、也不是精确 raw 的值，在 git 里是命名格式（oneline、short、medium、full、fuller、reference、email、mboxrd）或
# pretty.<名字> 配置别名：它们的输出带缩写提交号、受 log.date 影响的日期，或展开结果看不见，所以不固定（规格 v2 没有明说这一类，按“只放行 raw”保守处理）。
# 注意：这里只管“值”。``--format=``／``--pretty=`` 这两个前缀是否放行由子命令表决定（show、log 放行；rev-list、cat-file 等不放行）。
_GIT_FORMAT_PLACEHOLDERS = frozenset({"H", "T", "P", "an", "ae", "aI", "at", "cn", "ce", "cI", "ct", "s", "b", "B", "n", "%"})
_GIT_FORMAT_HEX = re.compile(r"x[0-9a-fA-F]{2}")  # %x 加恰好两位十六进制数字
_GIT_FORMAT_NAMES = frozenset({"raw"})
_GIT_FORMAT_PREFIXES = ("format:", "tformat:")


def _ascii_digits(text: str) -> bool:
    """非空，且全是 ASCII 数字 0-9。规格 v2 §4：所有“数字”判定一律用它（``isascii() and isdigit()``），拒绝 ``²``、``٣`` 这类 Unicode 数字。"""
    return text.isascii() and text.isdigit()


def git_format_problem(value: str) -> str | None:
    """``--format=``／``--pretty=`` 的取值是否是精确的 raw，或“普通字符＋白名单占位符”的自定义格式串：是返回 None，否则返回一句话原因。规则见上面的整体说明。"""
    if value in _GIT_FORMAT_NAMES:
        return None
    body = value
    prefixed = False
    for prefix in _GIT_FORMAT_PREFIXES:
        if value.startswith(prefix):
            body, prefixed = value[len(prefix):], True
            break
    if not prefixed and "%" not in body:
        return (f"格式名 {value!r} 不是精确的 raw，也不是 format:／tformat: 自定义格式串（命名格式 oneline、short、medium、full、fuller、reference、email、mboxrd "
                "和 pretty.<名字> 配置别名的输出带缩写提交号、受 log.date 影响的日期或看不见的展开）")
    index = body.find("%")
    while index >= 0:
        rest = body[index + 1:]
        if rest[:2] in _GIT_FORMAT_PLACEHOLDERS and len(rest[:2]) == 2:
            index += 3
        elif rest[:1] in _GIT_FORMAT_PLACEHOLDERS and rest[:1]:
            index += 2
        elif _GIT_FORMAT_HEX.match(rest):
            index += 4
        else:
            return (f"占位符 {body[index:index + 4]!r} 不在白名单内（只放行 %H %T %P %an %ae %aI %at %cn %ce %cI %ct %s %b %B %n %% 和 %x 加两位十六进制；"
                    "%D %d 带出引用名，%N 读 notes，%g* 读 reflog，%(…) 读引用库，%h %t %p 缩写长度随对象库变，%ad %cd %aD %cD 受 log.date 影响，%ar %cr 随当前时间变，%aN 等读 mailmap）")
        index = body.find("%", index)
    return None


def _git_range_arg_ok(arg: str) -> bool:
    if _GIT_COMMIT.fullmatch(arg):
        return True
    if arg.startswith("^"):
        return _GIT_COMMIT.fullmatch(arg[1:]) is not None
    for separator in ("...", ".."):  # 先试三点，免得 A...B 被两点从中间切开
        left, found, right = arg.partition(separator)
        if found:
            return _GIT_COMMIT.fullmatch(left) is not None and _GIT_COMMIT.fullmatch(right) is not None
    return False


def git_usage_problem(argv: Sequence[str]) -> str | None:
    """一条 git 子进程命令行是否“固定”：固定返回 None，否则返回一句话原因（写进评审清单）。规则见上面的整体说明。"""
    if not argv or os.path.basename(argv[0]) != "git":
        return "不是 git 命令行，无法判定"
    if GIT_ARGV_HIDDEN in argv:
        return "参数不可见（strace 没有解码 execve），无法判定读了哪些修订"
    args = list(argv[1:])
    while args and args[0] in _GIT_PLAIN_GLOBALS:
        del args[0]
    if not args:
        return "没有子命令"
    if args[0].startswith("-"):
        return f"子命令之前有全局选项 {args[0]}（会换仓库或改变命令行为；拿不准按未固定）"
    name, args = args[0], args[1:]
    rule = _GIT_FIXED_SUBCOMMANDS.get(name)
    if rule is None:
        return f"子命令 {name} 不在固定清单内（读引用库、写仓库状态或按工作区寻址；清单只有 {'、'.join(_GIT_FIXED_SUBCOMMANDS)}）"
    exact, prefixes, mode = rule
    positional: list[str] = []
    options: set[str] = set()
    after_separator = False
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if after_separator:
            continue  # “--” 之后是路径，不是修订
        if arg == "--":
            if name in _GIT_DASHDASH_REVISION_SUBCOMMANDS:
                return (f"{name} 的 argv 里有 --：它在 {name} 里只是“选项结束”，其后仍是修订，不是路径分隔"
                        "（git merge-base A B -- HEAD 的输出随 HEAD 变化），一律按不固定判")
            after_separator = True
        elif mode == "range" and arg == "-n":
            if index >= len(args) or not _ascii_digits(args[index]):
                return "-n 后面不是数字（只接受 ASCII 数字 0-9）"
            index += 1
        elif mode == "range" and arg.startswith("-") and _ascii_digits(arg[1:]):
            continue  # -<数字>
        elif arg.startswith("-"):
            if arg not in exact and not arg.startswith(prefixes):
                return f"选项 {arg} 不在放行清单内（可能把引用库、标准输入或当前时间带进来；拿不准按未固定）"
            if arg.startswith(("--format=", "--pretty=")):
                problem = git_format_problem(arg.partition("=")[2])
                if problem is not None:
                    return f"选项 {arg} 的取值不是精确的 raw，也不是“普通字符＋白名单占位符”的格式串：{problem}"
            for numeric in _GIT_NUMERIC_PREFIXES:
                if arg.startswith(numeric) and not _ascii_digits(arg[len(numeric):]):
                    return f"选项 {arg} 的取值不是数字（只接受 ASCII 数字 0-9）"
            options.add(arg)
        else:
            positional.append(arg)
    if name == "cat-file" and positional and positional[0] in _GIT_OBJECT_TYPES:
        positional = positional[1:]  # cat-file blob <对象> 的类型词
    if mode == "first":
        positional = positional[:1]  # ls-tree <树> <路径…>
    if not positional:
        return f"{name} 没有给出修订（缺省会落到 HEAD）"
    for arg in positional:
        if not (_git_range_arg_ok(arg) if mode == "range" else _GIT_REV.fullmatch(arg)):
            return f"修订参数 {arg!r} 不是完整 40 位提交号（HEAD、引用、分支、标签、缩写、相对写法都不固定）"
    if mode == "range" and all(arg.startswith("^") for arg in positional):
        return f"{name} 只给了排除的修订，没有正向修订（缺省会落到 HEAD）"
    if name == "rev-parse" and "--verify" in options and len(positional) != 1:
        return "rev-parse --verify 只接一个修订"
    if name == "merge-base" and len(positional) < 2:
        return "merge-base 需要两个固定修订"
    if name == "cat-file" and len(positional) != 1:
        return "cat-file 只放行一个固定对象"
    return None


def draft_unit(sample: Mapping[str, Any], *, evidence_sha: str, safe: str, tree: Path, files: set[str], totals: Mapping[str, int],
               classifier: Classifier, head: str | None, ledger: set[str] | None,
               coverage_check: bool = True) -> tuple[dict[str, Any], list[str]]:
    """一个 sample 的登记表条目和评审小节。无效采样只给 rerun-only，评审清单照样把原因和证据摘要列全。

    ``coverage_check``（缺省开）：起草后做覆盖闭合（为 missing 探测与只被 stat 的目录补 range，树根被枚举或有 missing 探测时写
    ``repo.root_listing``），再用登记侧的覆盖检查自检起草好的 scope，不通过就写成 rerun-only。关掉就不做这两件事，草案只按读集归并；
    不受它影响的是分类与输出区的修正（``/run/`` 归 virtual、被写入的 ``/dev/null`` 补 scratch 行、``repo_ignored`` 不盖住安装目录）
    和 range 名的规范化——它们没有“关掉”的理由。"""
    review, coverage = sample["review"], sample["coverage"]
    unit_id = sample["unit_id"]
    allowances = review["allowances"]
    # merge 的人工登记项（--installed-dir、--transient；旧 sample 没有这两个键）。只有命中过的才进草案：没用上的豁免不该留在
    # scope 里（它会白白绑上一个合同，或把输出区放宽到从未观察到的路径），未命中的在评审清单里列出让人看。
    installed_used = sorted((item["prefix"], item["contract"]) for item in allowances.get("installed_dirs", []) if not item["unused"])
    transient_used = sorted({item["pattern"] for item in allowances.get("transient", []) if not item["unused"]})
    # 旧 sample（命令行校验收紧之前合并出来的）里的模式写法可能已经不合规（例如通配符超过 4 个）：这里不抛异常、不让整批起草崩掉，匹配器取 None 跳过，
    # 模式本身照常进 repo_ignored 草案，交给覆盖自检按登记侧的规则报 C0-scope（该单元转 rerun-only）。
    transient_matchers = [matcher for matcher in (_ignored_matcher(pattern) for pattern in transient_used) if matcher is not None]
    installed_prefixes = tuple(prefix for prefix, _contract in installed_used)

    def skip_listed(relative: str) -> bool:
        return (relative + "/").startswith(installed_prefixes) or any(matcher.matches(relative) for matcher in transient_matchers)

    repo = derive_repo_scope(sample, files, totals, skip_listed=skip_listed if installed_prefixes or transient_matchers else None)

    # 写入且被忽略的路径所在目录，加上命中过的自产暂存路径模式，作为 repo_ignored 草案。目录前缀不得与依赖安装目录前缀重叠或包含它
    # （见 build_repo_ignored）：落在安装目录里、或把安装目录整个盖住的，改用被写文件的精确路径。
    writes = [repo_rel(path) for path, kinds, tracked in sample["repo_accesses"]
              if "write" in kinds and not tracked and not repo_rel(path).startswith(".git")]
    ignored = git_ignored(tree, writes)
    repo_ignored, exact_ignored = build_repo_ignored(ignored, writes, transient_used, installed_prefixes)

    # 仓库外：按分类前缀归并；HOME 下逐条（要登记存在性与内容）；归不进去的逐条列未归类。
    outside: dict[str, tuple[str, str | None]] = {}
    unclassified: list[list[Any]] = []
    ancestors: list[list[Any]] = []
    by_class: dict[str, int] = {}
    etc_items: list[list[Any]] = []
    home_items: list[list[Any]] = []
    dev_null_written = False
    for key, kinds, count in sample["outside_accesses"]:
        # 被写入的 /dev/null（丢弃输出，没有可观察状态；merge 按 DECLARED_WRITE_SINKS 豁免）：/dev/ 归 virtual 类，登记侧不许往 virtual 类里
        # 写，所以在原有分类之外，再为这个精确路径补一行 scratch（最长前缀优先，它盖过 /dev/ 那一行；scratch 类允许写，也不绑定内容）。
        if "write" in kinds and key in DECLARED_WRITE_SINKS:
            outside[key] = ("scratch", None)
            dev_null_written = True
        found = classifier.classify(key)
        # HOME 根目录自身的 stat 也是祖先探测，不是一份要登记的配置文件。
        if found is not None and key in ("{HOME}", "{HOME}/") and not set(kinds) - {"stat", "dir", "missing"}:
            ancestors.append([key, kinds, count])
            continue
        if found is None:
            (ancestors if _is_ancestor_probe(key, kinds, classifier.prefixes) else unclassified).append([key, kinds, count])
            continue
        cls, contract, prefix = found
        by_class[cls] = by_class.get(cls, 0) + 1
        if cls == "home-config":
            outside[key] = (cls, contract or home_contract(key))
            home_items.append([key, kinds, count])
        else:
            outside[prefix] = (cls, contract)
            if prefix == "/etc/":
                etc_items.append([key, kinds, count])
    outside_rows = [{"prefix": prefix, "class": cls, **({"contract": contract} if contract else {})}
                    for prefix, (cls, contract) in sorted(outside.items())]
    contracts = sorted({contract for _cls, contract in outside.values() if contract} | {contract for _prefix, contract in installed_used})

    # 合同要求的依赖锁文件：登记侧规定声明了某个工具链合同（toolchain:go、toolchain:pnpm）或缓存合同（cache:node-modules、cache:go-mod-cache），
    # 就必须在 repo.files 里单独列出它的锁文件，即使范围已经覆盖也不去重。上面的 contracts 已经并入了安装目录（installed_used）带来的合同，
    # 所以 --installed-dir 的 cache:node-modules 也在这里判。文件必须是仓库里跟踪的；不存在或未跟踪的不凭空添加，只在评审清单里标出。
    requirements = [(f"toolchain:{tool}", lock) for tool, locks in TOOLCHAIN_LOCK_FILES.items() if f"toolchain:{tool}" in contracts for lock in locks]
    requirements += [(contract, lock) for contract, lock in CACHE_CONTRACT_LOCK_FILES.items() if contract in contracts]
    lock_rows: list[tuple[str, str, str]] = []  # (合同, 文件, 已在 files／补入／缺失)
    added_locks: list[str] = []
    for contract, lock in requirements:
        state = "已在 files" if lock in repo["files"] else "补入" if lock in files else "缺失"
        if state == "补入" and lock not in added_locks:  # 同一个锁文件被两个合同同时要求（backend/go.sum）只补一次
            added_locks.append(lock)
        lock_rows.append((contract, lock, state))
    if added_locks:
        repo["files"] = sorted({*repo["files"], *added_locks})

    # 覆盖闭合（在锁文件补入之后：锁文件也算已声明的单文件）：为仓库内的 missing 探测和只被 stat／dir 的目录补 range，树根被枚举或树根直接子项
    # 有 missing 探测时标记 root_listing。补入的 range 与读集归并出的 range 合在一起重新排序、重新起名（没有补入时与从前逐字相同）。
    closure: dict[str, Any] | None = None
    if coverage_check:
        closure = close_repo_scope(sample["repo_accesses"], tracked=files, include_dirs=repo["include_dirs"], declared_files=repo["files"],
                                   ignored_items=repo_ignored, installed_prefixes=installed_prefixes)
        if closure["extra"]:
            merged = sorted([*repo["include_dirs"], *closure["extra"]])
            repo["ranges"] = [{"name": name, "include": [directory + "/"], "exclude": []} for name, directory in zip(name_ranges(merged), merged)]

    # git 历史：从采样到的 git 子进程 argv 里机械提取 40 位提交号，符号引用（HEAD 等）只提示。
    commits: list[str] = []
    symbolic: list[str] = []
    for argv in sample["git_commands"]:
        for part in argv[1:]:
            for found in _COMMIT_IN_ARGV.findall(part):
                if found not in commits:
                    commits.append(found)
            if re.search(r"\bHEAD\b|refs/", part) and part not in symbolic:
                symbolic.append(part)
    git_internal = [path for path, kinds, _tracked in sample["repo_accesses"]
                    if repo_rel(path).startswith(".git/") and ("read" in kinds or "exec" in kinds)]
    scope_repo: dict[str, Any] = {"ranges": repo["ranges"], "files": repo["files"], "list_ok": [], "repo_ignored": repo_ignored, "head": False}
    if closure is not None and closure["root_listing"]:  # 可选键：树根被枚举／读取，或树根直接子项有 missing 探测（登记侧用顶层名字摘要绑定）
        scope_repo["root_listing"] = True
    if installed_used:
        scope_repo["installed_dirs"] = [{"prefix": prefix, "contract": contract} for prefix, contract in installed_used]
    if commits:
        scope_repo["git_history"] = {"commits": commits, "reason": "由采样到的 git 子进程 argv 机械提取；须与 docs/egress 台账里的提交号交叉核对"}
    scope = {"schema_version": SCOPE_SCHEMA, "repo": scope_repo, "make_expansion": [], "outside": outside_rows, "contracts": contracts,
             "network": "loopback-only"}
    # 本单元实际连过且已登记（--allow-unix）的 AF_UNIX 目标写成顶层 unix_sockets（排序，登记侧要求互不重复）；没有就不写这个键。
    unix_sockets = sorted(set(review["network"]["unix_allowed"]))
    if unix_sockets:
        scope["unix_sockets"] = unix_sockets

    # git 用法是否固定：只要有一条 git 子进程命令按 HEAD、引用、分支、标签寻址（或子命令不在固定清单内、参数不可见），结果就依赖仓库状态，
    # 而登记侧规定 repo.head 只能是 false——这样的单元不能做声明级承接，写成 rerun-only 并带 head: true（保留旧的 HEAD 输入）。
    # 逐条判定见 git_usage_problem；这里只负责汇总。git_history 的机械提取（上面）保持原样，不另外存放提交号。
    unfixed = [(argv, problem) for argv in sample["git_commands"] if (problem := git_usage_problem(argv)) is not None]
    git_reason = None
    if unfixed:
        shown = "；".join(" ".join(argv)[:120] for argv, _problem in unfixed[:3])
        more = f"；……共 {len(unfixed)} 条" if len(unfixed) > 3 else ""
        git_reason = f"git 子进程使用了未固定的修订（{shown}{more}）：结果依赖 HEAD 或引用库，不可声明级承接"

    # 覆盖自检：采样有效、git 用法固定（这样的单元才可能写 declared）才核对；已经是 rerun-only 的没有必要。自检不通过或核对函数出错 → rerun-only，
    # head 为 false（原因不是读 HEAD）。登记侧模块不可用时（check["state"] == "skipped"）条目保持原状，评审清单注明没有自检。
    check: dict[str, Any] = {"state": "not-needed"} if coverage_check else {"state": "off"}
    coverage_reason: str | None = None
    if coverage_check and sample["valid"] and git_reason is None:
        check = coverage_self_check(sample, scope)
        if check["state"] == "failed":
            coverage_reason = coverage_failure_reason(check)
        elif check["state"] == "error":
            coverage_reason = f"{COVERAGE_REASON_PREFIX}：核对函数自己抛出异常（{check['note']}），无法证明声明范围覆盖了采样的读集"

    if sample["valid"] and git_reason is None and coverage_reason is None:
        entry: dict[str, Any] = {"status": "declared", "scope": scope, "sample": {
            "evidence": f"{EVIDENCE_DIR}/{safe}.sample.json", "evidence_sha256": evidence_sha, "command_sha256": sample["command"]["sha256"],
            "launcher_sha256": sha256_json(sample["command"]["launcher"]), "sampled_commit": sample["sampled"]["commit"],
            "sampled_at_utc": sample["sampled"]["utc"], "sampler_sha256": sample["sampled"]["sampler_sha256"],
            "runs": len(sample["sampled"]["runs"])}}
    else:
        # 两个原因都成立时并列写在 reason 里；head 取或（只有 git 用法不固定才带 head: true，采样无效本身不带）。
        parts = [] if sample["valid"] else [("采样无效：" + "；".join(sample["invalid_reasons"]))[:800]]
        if git_reason is not None:
            parts.append(git_reason)
        if coverage_reason is not None:  # 只在前两个原因都不成立时才会有（有了就不自检）
            parts.append(coverage_reason)
        entry = {"status": "rerun-only", "reason": "；".join(parts)}
        if git_reason is not None:
            entry["head"] = True

    lines = [f"## 单元 `{_md(unit_id)}`", ""]
    lines += [f"- 采样有效：**{'是' if sample['valid'] else '否'}**；登记状态草案：`{entry['status']}`；证据文件 `{safe}.sample.json`，摘要 `{evidence_sha}`"]
    lines += [f"- 采样提交 `{sample['sampled']['commit']}`；命令 `{_md(' '.join(sample['command']['argv']))}`，cwd 相对测试树 `{_md(sample['command']['cwd_rel'])}`；"
              f"共 {len(sample['sampled']['runs'])} 次运行（{'、'.join(str(item['cache']) for item in sample['sampled']['runs'])}）"]
    if head is not None and head != sample["sampled"]["commit"]:
        lines.append(f"- **注意**：当前树 HEAD `{head}` 与采样提交不同，过宽度量按当前树计算，评审时留意。")
    lines += ["", "### 1 采样有效性", ""]
    lines += [f"- {_md(reason)}" for reason in sample["invalid_reasons"]] or ["- 全部判据满足：退出码 0、运行前后 git status 为空、流完整、"
                                                                              "unresolved 都在容忍集合内、无未跟踪读取、网络只有本机、写入都在允许范围。"]
    if unfixed:
        lines.append(f"- **git 用法未固定**：{len(unfixed)} 条 git 子进程命令的修订不是完整 40 位提交号（HEAD、引用、分支、标签等），或子命令读引用库、"
                     "写仓库状态，或参数不可见；结果依赖 HEAD 或引用库，不可声明级承接，登记状态草案已改为 `rerun-only` 并带 `head: true`"
                     "（保留旧的 HEAD 输入），全部命令见第 11 节。")
    if coverage_reason is not None:
        lines.append(f"- **覆盖自检不通过**：起草的 scope 没能通过登记侧的覆盖检查，登记状态草案已改为 `rerun-only`（reason 以“{COVERAGE_REASON_PREFIX}”开头），"
                     "全部未覆盖项见第 13 节。")
    lines += ["", "### 2 过宽度量（每个 include）", ""]
    if repo["include_dirs"]:
        lines += ["| include | 跟踪文件数 | 被读文件数 | 过宽（未读） | 命中率 | 被枚举 |", "|---|---|---|---|---|---|"]
        for directory in repo["include_dirs"]:
            total, read = totals.get(directory, 0), repo["dir_read"].get(directory, 0)
            ratio = f"{read / total:.3f}" if total else "无跟踪文件"
            lines.append(f"| `{_md(directory)}/` | {total} | {read} | {total - read} | {ratio} | {'是' if directory in repo['listed'] else '否'} |")
    else:
        lines.append("没有 include 目录。")
    root_files = sum(1 for name in files if "/" not in name)
    for title, depth, limit in (("顶层目录", 1, None), ("被读文件最多的二级目录（前 10）", 2, 10)):
        lines += ["", f"按{title}汇总（决定把碎片范围合并到多粗时的参考；只是度量，不是建议）：", "",
                  "| 目录 | 跟踪文件数 | 被读文件数 | 命中率 |", "|---|---|---|---|"]
        for name, read in rollup(repo["read_files"], depth)[:limit]:
            total = root_files if name == ROOT_FILES else totals.get(name, 0)
            shown = name if name == ROOT_FILES else name + "/"
            lines.append(f"| `{_md(shown)}` | {total} | {read} | {f'{read / total:.3f}' if total else '无跟踪文件'} |")
    lines += ["", f"- 单文件（files）共 {len(repo['files'])} 个，被读到的跟踪文件共 {repo['reads']} 个。"]
    if lock_rows:
        lines.append("- 依赖锁文件（登记侧规定声明了工具链合同 toolchain:*，或缓存合同 cache:node-modules／cache:go-mod-cache，就必须在 files 里单独列出它的锁文件，"
                     "范围已覆盖也不去重；“补入”＝由合同要求补入）："
                     + "；".join(f"`{_md(lock)}`（{_md(contract)}：{state}）" for contract, lock, state in lock_rows))
        missing = list(dict.fromkeys(lock for _contract, lock, state in lock_rows if state == "缺失"))  # 两个合同要同一个文件时只列一次
        if missing:
            lines.append("- **依赖锁文件缺失**：" + "、".join(f"`{_md(item)}`" for item in missing)
                         + " 不是仓库里跟踪的文件，没有补入；登记前必须人工处理，否则登记侧会拒绝这份范围。")
    if closure is not None and closure["extra"]:
        lines.append(f"- **覆盖闭合补入的 range** {len(closure['extra'])} 个（登记侧要求仓库内的 missing 探测、以及只被 stat／dir 且其下没有任何声明内容的目录"
                     "必须落在某个 range 里；range 的摘要覆盖目录下全部跟踪文件，所以“这里没有这个文件”“这个目录里没有跟踪文件”也被绑定——"
                     "目录不存在或下面没有跟踪文件也照补，将来有人在里面新增跟踪文件，摘要就变；上面的汇总表不含这些）：")
        for directory in closure["extra"]:
            reasons = closure["why"][directory]
            parts = []
            if reasons["missing"]:
                parts.append(f"missing 探测 {len(reasons['missing'])} 个，例如 " + "、".join(f"`{_md(item)}`" for item in reasons["missing"][:3]))
            if reasons["stat"]:
                parts.append("目录自身被 stat／dir，其下没有声明内容")
            lines.append(f"  - `{_md(directory)}/`（跟踪文件 {totals.get(directory, 0)} 个）：" + "；".join(parts))
    if closure is not None and closure["root_listing"]:
        why = []
        if closure["root_listed"]:
            why.append("测试树根目录被枚举或读取")
        if closure["root_missing"]:
            why.append(f"树根直接子项有 {len(closure['root_missing'])} 个 missing 探测（" + "、".join(f"`{_md(item)}`" for item in closure["root_missing"][:8])
                       + ("……" if len(closure["root_missing"]) > 8 else "") + "）")
        lines.append("- **`repo.root_listing: true`**：" + "；".join(why) + "。树根不能成为 range（那等于重新声明整个仓库），登记侧用“树根下被跟踪的顶层名字”的"
                     "摘要绑定这类访问：同名的跟踪文件一出现摘要就变。这个键只有登记侧支持 root_listing 的版本才认，不认的版本会在第 13 节的覆盖自检里报 C0-scope。")
    if len(scope_repo["ranges"]) > MAX_RANGES or len(repo["files"]) > MAX_FILES:
        lines.append(f"- **超限待评审**：ranges {len(scope_repo['ranges'])}（上限 {MAX_RANGES}）、files {len(repo['files'])}（上限 {MAX_FILES}）；"
                     "可参考上面的汇总表决定合并到多粗。")
    _items(lines, repo["files"], lambda item: f"`{_md(item)}`" + ("（由合同要求补入）" if item in added_locks else ""))
    lines += ["", "### 3 被 list 枚举的目录（逐个决定保持 include 还是降为 list_ok）", ""]
    if repo["listed"]:
        for directory in repo["listed"]:
            cover = covering_include(directory, repo["include_dirs"]) if directory else None
            state = ("本身已 include" if cover == directory else f"被 include `{_md(cover)}/` 覆盖" if cover is not None
                     else "根目录，不会整体 include，需决定是否列入 list_ok" if not directory else "未被任何 include 覆盖")
            total, read = totals.get(directory, 0), repo["dir_read"].get(directory, 0)
            lines.append(f"- `{_md(directory or '.')}/`：跟踪文件 {total}、被读 {read}；{state}")
    else:
        lines.append("没有被枚举的仓库内目录。")
    lines += ["", "### 4 写入集合", ""]
    wr = review["writes"]
    lines += [f"- 仓库内被忽略的写入目录与自产暂存路径（repo_ignored 草案）：{('、'.join('`' + _md(item) + '`' for item in repo_ignored)) or '无'}"]
    if exact_ignored:
        lines.append(f"- **repo_ignored 里有 {len(exact_ignored)} 项改用了被写文件的精确路径**：这些路径所在的目录与依赖安装目录前缀重叠或把它整个盖住"
                     "（或写入发生在树根下，没有可用的目录前缀）；输出区若用目录前缀，会把安装目录、乃至 range 里已跟踪的输入统统吞掉（登记侧先判输出区，再判范围）。"
                     "原目录与精确路径（前 10 项）：" + "、".join(f"`{_md(directory)}`→`{_md(path)}`" for directory, path in exact_ignored[:10]))
    if allowances.get("transient"):
        lines.append("- 自产暂存路径（merge --transient）：落在其内的未跟踪路径只要本次采样里写过，写入与读回都不判无效；只读不写的不豁免；"
                     "运行前后 git status 必须为空的判据照旧，“写完即删”靠它兜底：")
        for item in allowances["transient"]:
            tail = ("；**没有命中任何被写路径，未并入 repo_ignored 草案**" if item["unused"]
                    else "；样例：" + "、".join(f"`{_md(path)}`" for path in item["sample"][:5]))
            lines.append(f"  - `{_md(item['pattern'])}`：命中被写路径 {item['paths']} 个，其中读回 {item['read_back']} 个{tail}")
    lines += [f"- 违规写入 {wr['violations']['count']} 处" + ("" if not wr["violations"]["count"] else "（下列为前几处）")]
    _items(lines, wr["violations"]["items"], lambda item: f"`{_md(item['path'])}`：{_md(item['reason'])}", wr["violations"]["count"])
    lines.append(f"- 仓库外写入目录与类别：{('、'.join('`' + _md(key) + '`→' + _md(cls) for key, cls in wr['outside'])) or '无'}")
    lines.append(f"- 已豁免的无害写入：{('、'.join('`' + _md(item) + '`' for item in wr['sinks'])) or '无'}（写入 /dev/null 没有可观察状态）")
    if dev_null_written:
        lines.append("- `/dev/null` 被写入：草案 outside 在 `/dev/`（virtual 类）之外，另补了精确前缀 `/dev/null`（class=scratch）——virtual 类不许写，"
                     "而写空设备没有可观察状态。")
    lines += ["", "### 5 untracked_reads（读取了未跟踪路径，必须为空）", ""]
    untracked = review["untracked_reads"]
    lines.append(f"- 共 {untracked['count']} 个" + ("；按前两级目录：" + "、".join(f"`{_md(prefix)}`（{count}）" for prefix, count in untracked["by_prefix"])
                                                 if untracked["count"] else ""))
    _items(lines, untracked["sample"], lambda item: f"`{_md(item)}`", untracked["count"])
    if allowances.get("installed_dirs"):
        lines.append("- 依赖安装目录（merge --installed-dir）：前缀下的读取不要求已跟踪，内容由对应的 cache 合同绑定；前缀下的写入仍按原规则判：")
        for item in allowances["installed_dirs"]:
            tail = ("；**没有读到任何路径，未并入 installed_dirs 草案**" if item["unused"]
                    else "；样例：" + "、".join(f"`{_md(path)}`" for path in item["sample"][:5]))
            lines.append(f"  - `{_md(item['prefix'])}` → 合同 `{_md(item['contract'])}`：豁免未跟踪读取 {item['reads']} 个{tail}")
    lines += ["", "### 6 network", ""]
    net = review["network"]
    lines.append(f"- 本机连接目的地 {net['loopback']} 个；已登记的 AF_UNIX：{('、'.join('`' + _md(x) + '`' for x in net['unix_allowed'])) or '无'}")
    _items(lines, sample["network"], lambda item: f"{_md(item['family'])} `{_md(item['target'])}`")
    lines += [f"- 违规 {len(net['violations'])} 条" + ("：" + "；".join(_md(item) for item in net["violations"]) if net["violations"] else "")]
    if unix_sockets:
        lines.append("- 草案 `scope.unix_sockets`：" + "、".join(f"`{_md(item)}`" for item in unix_sockets)
                     + "（本单元实际连过、且 merge 时用 --allow-unix 登记的目标）")
    lines += ["", "### 7 冷热差异（不是每次运行都出现的路径）", ""]
    for name, key in (("仓库内路径", "repo"), ("仓库外目录", "outside")):
        diff = review["run_differences"][key]
        lines.append(f"- {name}：{diff['count']} 项")
        _items(lines, diff["items"], lambda item: f"`{_md(item[0])}` 出现在第 {'、'.join(str(i + 1) for i in item[1])} 次", diff["count"])
    lines += ["", "### 8 git status 前后（必须都为空）", ""]
    for item in review["git_status"]:
        lines.append(f"- {_md(item['label'])}：前 {len(item['before'])} 条、后 {len(item['after'])} 条"
                     + ("：" + "；".join(f"`{_md(line)}`" for line in (item['before'] + item['after'])[:5]) if item["before"] or item["after"] else ""))
    for item in review["docker"]:
        lines.append(f"- docker（{_md(item['label'])}）：新增容器 {len(item['new_containers'])} 个、create 事件 {len(item['created'])} 个"
                     + (f"，错误 {_md(item['error'])}" if item["error"] else ""))
    lines += ["", "### 9 unresolved_reasons", ""]
    lines.append(f"- unresolved 总数 {coverage['unresolved']}；strace 标记行（detached 或 unfinished）{coverage['detached_markers']} 条已单独计数")
    lines += [f"- `{_md(reason)}`：{count}" for reason, count in coverage["unresolved_reasons"].items()]
    # 容忍的悬着调用：给出个数、上限和起始行样例，评审时看一眼这些被打断的调用是什么。
    at_eof = coverage["unresolved_reasons"].get("unfinished-calls-at-eof", 0)
    if at_eof:
        lines.append(f"- `unfinished-calls-at-eof` 共 {at_eof} 个在容忍集合内（合并后总数上限 {DECLARED_MAX_UNFINISHED_AT_EOF}；多线程进程退出时 strace "
                     "不为还在系统调用里的线程打印 resumed，被打断的调用没有返回成功，不贡献读集）"
                     + ("；悬着调用的起始行样例（采样根路径已换成别名，每条截到 300 字符，最多 8 条）：" if coverage.get("unfinished_samples") else ""))
        lines += [f"  - `{_quote(item)}`" for item in coverage.get("unfinished_samples", [])]
    # 不容忍的原因：逐条列出保留下来的原始 strace 行（每个原因最多 3 条），用来判断是偶发还是系统性缺口。
    unresolved_samples = review.get("unresolved_samples", {})
    if unresolved_samples:
        lines.append("- 不在容忍集合内的未解析原因的原始 strace 行样例（采样根路径已换成别名，每条截到 300 字符；偶发的原因靠它定位）：")
        for reason in sorted(unresolved_samples):
            lines.append(f"  - `{_md(reason)}`")
            lines += [f"    - `{_quote(item)}`" for item in unresolved_samples[reason]]
    lines += ["", "### 10 仓库外目录归类", ""]
    lines.append("- 已归类：" + ("、".join(f"{_md(cls)} {count}" for cls, count in sorted(by_class.items())) or "无"))
    lines += [f"- 未归类 {len(unclassified)} 项（必须逐条评审，或用 `--class-prefix` 登记后重新 merge）"]
    _items(lines, unclassified, lambda item: f"`{_md(item[0])}` {_md(item[1])} ×{item[2]}")
    lines += [f"- 仅元数据的上层目录探测 {len(ancestors)} 项（PATH 搜索、Go 往上找 go.mod 等）：{('、'.join('`' + _md(item[0]) + '`' for item in ancestors[:30])) or '无'}"]
    # 祖先链目录（测试树的各级祖先、别名根）的聚合键丢了文件名：键上的 read 可能是打开祖先目录本身（WP4c 之前的采样器记成 read，之后记成 dir），
    # 也可能是读了祖先目录下的直接子项（文件，或不是祖先的目录）。登记侧对这类键放行 read，所以单独点出来，重采后仍剩 read 的才需要逐个确认。
    chain = ancestor_keys(sample["aliases"])
    ancestor_reads = [item for item in sample["outside_accesses"] if item[0] in chain and "read" in item[1]]
    if ancestor_reads:
        lines.append(f"- **祖先链目录的聚合键上有 read** {len(ancestor_reads)} 个：" + "、".join(f"`{_md(item[0])}`" for item in ancestor_reads[:30])
                     + "。别名根与祖先目录自身被打开的 read，WP4c 起的采样器记成 dir；所以用新采样器采的 sample 里剩下的 read 是读了祖先目录下的直接子项"
                       "（文件，或不是祖先的目录），聚合键丢了文件名，而登记侧对这类键放行 read——请确认读的不是意外的宿主文件。旧采样器采的 sample 里也可能只是打开了目录本身。")
    else:
        lines.append("- 祖先链目录的聚合键上没有 read（别名根与祖先目录自身被打开的 read，WP4c 起的采样器记成 dir）。")
    lines += [f"- HOME 下配置 {len(home_items)} 项（要逐路径登记存在性与内容）"]
    _items(lines, home_items, lambda item: f"`{_md(item[0])}` {_md(item[1])}")
    lines += [f"- /etc 下读取 {len(etc_items)} 项（环境指纹只覆盖包管理器管理的文件，手改的文件要评审）"]
    _items(lines, etc_items, lambda item: f"`{_md(item[0])}` {_md(item[1])}")
    lines += ["", "### 11 git 历史与其它提示", ""]
    lines.append(f"- 采样到 {len(sample['git_commands'])} 条 git 子进程命令；机械提取的提交号 {len(commits)} 个；"
                 f"读取 .git 内部的路径 {len(git_internal)} 个")
    if unfixed:
        lines.append(f"- **未固定的 git 命令 {len(unfixed)} 条**（修订不是完整 40 位提交号、子命令不在固定清单内、选项不在放行清单内或参数不可见；"
                     "拿不准一律算未固定）：")
        _items(lines, unfixed, lambda item: f"`{_md(' '.join(item[0]))}`：{_md(item[1])}")
    _items(lines, [" ".join(argv) for argv in sample["git_commands"]], lambda item: f"`{_md(item)}`")
    if symbolic:
        lines.append("- **符号引用**（HEAD、refs/ 等）：" + "、".join(f"`{_md(item)}`" for item in symbolic[:10])
                     + ("；读 HEAD 或引用的单元不能声明级承接，已按“未固定的 git 用法”转 rerun-only" if unfixed
                        else "；它们不在修订参数的位置上（例如 :路径 或格式串），判为固定，仍请人工确认"))
    if ledger is not None and (commits or sample["git_commands"] or git_internal):
        extra = sorted(ledger - set(commits))
        lines.append(f"- 台账交叉核对：docs/egress 台账里出现 {len(ledger)} 个提交号，其中 {len(extra)} 个没有出现在采样的 git 调用里（只提示，不自动并入）")
        _items(lines, extra, lambda item: f"`{item}`")
    if git_internal and not commits:
        lines.append("- **注意**：读取了 .git 内部却没有提取到提交号，需要人工确认 git_history。")
    if sample["command"]["argv"][:1] == ["make"]:
        lines.append("- 该单元经 make 运行：要决定是用整个 `Makefile` 文件做输入，还是用 make_expansion 合同（草案里 make_expansion 为空）。")
    # 被丢弃的 strace 标记行：没有就不写这一节，免得每个单元的清单都多一段空话。
    markers, garbled = coverage["detached_markers"], review.get("detached_samples", [])
    if markers or garbled:
        lines += ["", "### 12 被丢弃的 strace 标记行（线程中途消失）", ""]
        lines.append(f"- 共 {markers} 条（含裸标记 `???( <detached ...>`、`???( <unfinished ...>`，和下面这种带名字的垃圾行）；"
                     f"合并后总数的容忍上限是 {DETACHED_MARKER_LIMIT}，超过判采样无效。")
        lines.append("- 它们描述的是“中途消失的线程”：Go 运行时的线程被杀或退出时，strace 把它当作还在系统调用里，用残留寄存器解码出一行名字、"
                     "参数都乱七八糟的东西（形态一以 `<detached ...>` 结尾；形态二以 ` = ?` 结尾，第一个参数是指针常量）。不携带读取信息，"
                     "已丢弃并计数，没有交给解析器。")
        side_effects = review.get("detached_side_effects", 0)
        if side_effects:
            lines.append(f"- **其中名字属于副作用类的有 {side_effects} 条**（会改文件系统或对外连接的调用名：unlink／rename／link／mkdir／truncate／chmod／chown／setxattr／creat、"
                         "带写入标志的 open／openat、connect／sendto 等；名字随机或解析不出来的不算）：线程已经消失、调用结果不可知，若它真的生效（写入、删除、改名、外连），"
                         "trace 里不会有任何痕迹——这是丢弃垃圾行的已知代价，评审时留意；下面的样例里副作用类排在前面。")
        if garbled:
            lines.append(f"- 带名字的垃圾行原样样例（采样根路径已换成别名，每条截到 {SAMPLE_LINE_CHARS} 字符，最多 {MAX_DETACHED_SAMPLES} 条，副作用类优先留样）：")
            lines += [f"  - `{_quote(item)}`" + (" ← 副作用类" if side_effect_call(item) else "") for item in garbled]
        else:
            lines.append("- 没有带名字的垃圾行，只有裸标记。")
    lines += ["", "### 13 覆盖自检（登记侧 declared_coverage）", ""]
    lines += coverage_review_lines(check)
    lines.append("")
    return entry, lines


def draft_registry(samples_dir: Path, tree: Path, *, class_prefixes: Sequence[tuple[str, str, str | None]] = (),
                   coverage_check: bool = True) -> tuple[dict[str, Any], str]:
    """samples_dir 下全部 ``*.sample.json`` → (登记表草案, 评审清单文本)。

    ``coverage_check``（缺省开）：覆盖闭合加登记侧覆盖检查自检，见 ``draft_unit``；``--no-coverage-check`` 对应关掉这两件事。"""
    tree, samples_dir = Path(tree).resolve(), Path(samples_dir)
    paths = sorted(samples_dir.glob("*.sample.json"))
    if not paths:
        raise SamplerError(f"{samples_dir} 里没有 *.sample.json")
    loaded: list[tuple[dict[str, Any], str, str]] = []
    seen: dict[str, str] = {}
    for path in paths:
        document = load_document(path)
        problems = validate_sample(document)
        if problems:
            raise SamplerError(f"{path.name} 不合法：{'；'.join(problems[:3])}")
        safe = safe_name(document["unit_id"])
        if path.name != f"{safe}.sample.json":
            raise SamplerError(f"{path.name} 的文件名必须是 {safe}.sample.json（登记表按这个名字引用证据）")
        if safe in seen:
            raise SamplerError(f"单元 {document['unit_id']} 与 {seen[safe]} 的安全文件名冲突")
        seen[safe] = document["unit_id"]
        loaded.append((document, safe, file_sha256(path)))
    files, _directories = tracked_sets(tree)
    totals = directory_totals(files)
    try:
        head: str | None = git_head(tree)
    except SamplerError:
        head = None
    ledger = scan_ledger_commits(tree) if any(doc["git_commands"] or any(r[0].startswith("{TREE}/.git/") for r in doc["repo_accesses"])
                                              for doc, _safe, _sha in loaded) else None
    units: dict[str, Any] = {}
    sections: list[list[str]] = []
    rows: list[str] = []
    for document, safe, evidence_sha in loaded:
        # 采样时用 --class-prefix 登记过的前缀随 sample 留痕，draft 自动沿用，命令行追加的再叠加。
        try:
            recorded = [tuple(item) for item in document["review"]["allowances"]["class_prefixes"]]
            classifier = Classifier([*recorded, *class_prefixes])  # type: ignore[list-item]
            entry, section = draft_unit(document, evidence_sha=evidence_sha, safe=safe, tree=tree, files=files, totals=totals,
                                        classifier=classifier, head=head, ledger=ledger, coverage_check=coverage_check)
        except (KeyError, TypeError, IndexError, ValueError) as error:
            # validate_sample 只校验到 review 的顶层键；内层结构被手工改坏时给出明确错误，而不是回溯。
            raise SamplerError(f"单元 {document['unit_id']} 的 sample 内层结构不符（review 被改动过？）：{type(error).__name__} {error}") from error
        units[document["unit_id"]] = entry
        sections.append(section)
        scope = entry.get("scope")
        rows.append(f"| `{_md(document['unit_id'])}` | {'是' if document['valid'] else '**否**'} | {len(document['repo_accesses'])} | "
                    f"{len(document['outside_accesses'])} | {len(scope['repo']['ranges']) if scope else '-'} | "
                    f"{len(scope['repo']['files']) if scope else '-'} | {_md(entry['status'])} |")
    valid = sum(1 for doc, _s, _h in loaded if doc["valid"])
    unfixed_units = sum(1 for entry in units.values() if entry.get("head"))  # 只有 git 用法未固定的条目带 head: true
    uncovered_units = sum(1 for entry in units.values() if entry["status"] == "rerun-only" and entry["reason"].startswith(COVERAGE_REASON_PREFIX))
    text = ["# 声明级承接采样评审清单", "",
            "> 由 `declared_sampler.py draft` 机械生成：只按目录命中率等规则归并，**不做任何语义判断的自动放行**。每一节都需要人工确认；",
            "> 草案里的 `list_ok` 与 `make_expansion` 一律为空，由评审者决定。", "",
            f"- 采样器摘要：`{file_sha256(HERE)}`", f"- 当前树 HEAD：`{head or '未知'}`",
            f"- 单元 {len(loaded)} 个，采样有效 {valid} 个，无效 {len(loaded) - valid} 个"
            + (f"；另有 {unfixed_units} 个因 git 用法未固定（按 HEAD、引用、分支、标签寻址）转 rerun-only 并带 head: true" if unfixed_units else "")
            + (f"；另有 {uncovered_units} 个因覆盖自检不通过（起草的 scope 过不了登记侧的覆盖检查）转 rerun-only" if uncovered_units else ""), "",
            "## 总览", "", "| 单元 | 采样有效 | 仓库内路径 | 仓库外目录 | ranges | files | 登记状态草案 |", "|---|---|---|---|---|---|---|", *rows, ""]
    for section in sections:
        text += section
    registry = {"schema_version": REGISTRY_SCHEMA, "units": dict(sorted(units.items()))}
    return registry, "\n".join(text) + "\n"


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="declared_sampler.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter", help="读标准输入的 strace 输出，流式输出 declared-sample-raw/v1（作为 strace -o '|…' 管道的另一端）")
    filt.add_argument("--output", type=Path, required=True)
    filt.add_argument("--unit-id", required=True)
    filt.add_argument("--alias", action="append", default=[], help="NAME=绝对路径，TREE、WORK、HOME、TMP 都要给")
    filt.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES, help="每处理多少行排空一次解析器的路径字典；0 表示只在结束时排空")
    run = sub.add_parser("run", help="从门禁清单取命令单元，在 strace 下执行一次并保存 raw")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--unit", required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--tree", type=Path, required=True, help="采样用的测试树；必须与生成清单时 plan 的 --tree 相同")
    run.add_argument("--work", type=Path, required=True, help="工作根；Go 的三个缓存固定在其下 go-build-cache、go-mod-cache、go-tmp")
    run.add_argument("--cache", choices=("cold", "warm"), default="cold",
                     help="cold：为 GOCACHE 准备空目录（旧的非空目录改名保留，不删除）；warm：复用上一次写热的目录。前端的 tsbuildinfo、vitest 缓存需自行准备")
    run.add_argument("--no-go-cache", action="store_true",
                     help="单元不用 Go 缓存（前端、Python 工具）：热缓存不再要求 go-build-cache 非空，冷缓存也不改名旧目录；raw 的 run 记录里留痕 no_go_cache")
    run.add_argument("--label", help="文件名标签，默认等于 --cache；同一单元要多采几次（例如离线验证）时用不同标签")
    run.add_argument("--launcher-json", help="生成清单时的启动前缀（JSON 字符串列表）；不给则只认已知的 ARM64 前缀")
    run.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="追加或覆盖环境变量（例如 GOPROXY=off），记入 raw 留痕")
    run.add_argument("--docker-hooks", action="store_true", help="记录 docker ps -a 前后差集与 docker events（integration 类单元用）")
    run.add_argument("--timeout-factor", type=float, default=DEFAULT_TIMEOUT_FACTOR, help="超时 = 清单超时 × 该系数")
    run.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES)
    merge = sub.add_parser("merge", help="多份 raw 取并集，标 tracked，判有效性，生成 declared-sample/v1")
    merge.add_argument("--raw", type=Path, action="append", required=True)
    merge.add_argument("--tree", type=Path, required=True)
    merge.add_argument("--out", type=Path, required=True)
    merge.add_argument("--allow-unix", action="append", default=[], metavar="PATH",
                       help="登记允许连接的 AF_UNIX 目标（如 /var/run/docker.sock、/var/run/nscd/socket），须是规范绝对路径；draft 把本单元实际连过的写成 scope.unix_sockets")
    merge.add_argument("--class-prefix", action="append", default=[], metavar="前缀=类别[:合同]",
                       help="登记内置表没有覆盖、但经评审可以接受的仓库外前缀；会随 sample 留痕，draft 自动沿用")
    merge.add_argument("--installed-dir", action="append", default=[], metavar="前缀=合同",
                       help="登记依赖安装目录（如 frontend/node_modules/=cache:node-modules）：前缀下的读取不再判 untracked-read，写入仍按原规则判，"
                            "前缀下若有已跟踪路径被读取就拒绝；draft 写出 scope.repo.installed_dirs 并把合同并入 scope.contracts")
    merge.add_argument("--transient", action="append", default=[], metavar="模式",
                       help="登记命令自产的暂存路径：仓库相对的目录前缀（以 / 结尾）或 glob（只支持 * 与 ?，不跨 /），如 frontend/.eslintcache、"
                            "frontend/vitest.config.ts.timestamp-*；落在其内的未跟踪路径本次采样里写过，写入与读回都不判无效，只读不写的不豁免；"
                            "draft 把命中过的模式并入 scope.repo.repo_ignored")
    draft = sub.add_parser("draft", help="把 sample 起草成登记表草案和评审清单（git 用法未固定的单元转 rerun-only 并带 head: true）")
    draft.add_argument("--samples", type=Path, required=True)
    draft.add_argument("--tree", type=Path, required=True)
    draft.add_argument("--registry-out", type=Path, required=True)
    draft.add_argument("--review-out", type=Path, required=True)
    draft.add_argument("--class-prefix", action="append", default=[], metavar="前缀=类别[:合同]")
    draft.add_argument("--no-coverage-check", action="store_true",
                       help="关闭覆盖闭合与覆盖自检（草案只按读集归并）。缺省会为 missing 探测和只被 stat 的目录补 range、树根被枚举时写 repo.root_listing，"
                            "再用登记侧的覆盖检查（unit_records.declared_coverage）核对起草的 scope，不通过就写成 rerun-only")
    return parser.parse_args(list(argv))


def _parse_env(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SamplerError(f"--env 必须是 KEY=VALUE：{item!r}")
        values[name] = value
    return values


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "filter":
            # 过滤器是 strace 的管道子进程，与被跟踪命令在同一个进程组：超时终止命令时整组都会收到 SIGTERM，交互运行时
            # 按 Ctrl-C 也是整组 SIGINT。忽略这两个信号，让它读到 strace 退出后关闭管道的 EOF，再把已有的读集写出来——
            # 超时的采样虽然无效，读集仍有诊断价值，而不是让 run 误报成“过滤器故障”。SIGKILL 不可忽略，兜底仍在。
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            stream = (line.decode("utf-8", "surrogateescape") for line in sys.stdin.buffer)
            document = filter_stream(stream, parse_aliases(args.alias), unit_id=args.unit_id, chunk_lines=args.chunk_lines)
            write_document(args.output, document)
            return 0
        if args.command == "run":
            code, _raw, summary = run_sample(
                manifest=args.manifest, unit_id=args.unit, out_dir=args.out, tree=args.tree, work=args.work, cache=args.cache,
                label=args.label, launcher_json=args.launcher_json, env_overrides=_parse_env(args.env), docker_hooks=args.docker_hooks,
                timeout_factor=args.timeout_factor, chunk_lines=args.chunk_lines, no_go_cache=args.no_go_cache)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            if code == 1:
                print("declared_sampler 警告：命令退出码非零或超时，raw 已写出但 merge 会判采样无效", file=sys.stderr)
            elif code == 3:
                print("declared_sampler 错误：" + str(summary.get("error")), file=sys.stderr)
            return code
        if args.command == "merge":
            raws = [load_document(path) for path in args.raw]
            sample = merge_raws(raws, tree=args.tree, allow_unix=args.allow_unix,
                                class_prefixes=[parse_class_prefix(item) for item in args.class_prefix],
                                installed_dirs=[parse_installed_dir(item) for item in args.installed_dir],
                                transient=[check_transient(item) for item in args.transient])
            write_document(args.out, sample)
            print(json.dumps({"unit_id": sample["unit_id"], "valid": sample["valid"], "invalid_reasons": sample["invalid_reasons"],
                              "repo_paths": len(sample["repo_accesses"]), "outside_directories": len(sample["outside_accesses"])},
                             ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "draft":
            registry, review = draft_registry(args.samples, args.tree, class_prefixes=[parse_class_prefix(item) for item in args.class_prefix],
                                              coverage_check=not args.no_coverage_check)
            write_document(args.registry_out, registry)
            args.review_out.parent.mkdir(parents=True, exist_ok=True)
            args.review_out.write_text(review, encoding="utf-8")
            declared = sum(1 for entry in registry["units"].values() if entry["status"] == "declared")
            print(json.dumps({"units": len(registry["units"]), "declared": declared, "rerun_only": len(registry["units"]) - declared},
                             ensure_ascii=False, sort_keys=True))
            return 0
    except SamplerError as error:
        print(f"declared_sampler 拒绝：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
