package service

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"golang.org/x/net/dns/dnsmessage"
)

// 本包测试的 DNS 桩。
//
// 背景：渠道监控的 validateEndpoint、图片下载的 isPrivateOrLoopbackHost 等生产代码直接用 net.DefaultResolver 解析域名，
// 单元测试里凡是写了 https://api.openai.com 这类真实域名并走到解析的用例（例如 TestValidateCreateParams_CheckModeMatrix），
// 都在真实访问外网 DNS：断网环境（ARM64 沙箱、无网络的 CI 分片）下会报 "endpoint hostname could not be resolved"，
// 联网环境下则依赖运行机的 DNS 是否可用、是否被透明代理改写成假 IP，结果不确定。
//
// 做法：测试进程启动时把 net.DefaultResolver 换成一个只回答固定域名表的解析器。它走 Go 自带的纯 Go 解析器，
// 只替换“拨号到名字服务器”这一步，换成进程内的 net.Pipe 加一个最小的 DNS 应答协程，所以生产代码一行不动，
// 解析、搜索域、A/AAAA 合并等流程仍然是真实的 Go 标准库实现。表里没有的域名一律应答 NXDOMAIN，与断网时的
// 失败语义一致（“解析失败”而不是“超时”），所以期望解析失败的用例照常通过。
//
// 在 init 里一次性替换，不在用例里临时改全局变量，避免与并行用例读取 net.DefaultResolver 产生数据竞争。
// 表里的地址都是文档保留网段 203.0.113.0/24（不属于私网、回环、链路本地，所以对 SSRF 判定来说是“公网地址”）。
var stubDNSHosts = map[string][]string{
	"api.openai.com":          {"203.0.113.11"},
	"api.anthropic.com":       {"203.0.113.12"},
	"api.deepseek.com":        {"203.0.113.13"},
	"api.kimi.com":            {"203.0.113.14"},
	"api.x.ai":                {"203.0.113.15"},
	"api.z.ai":                {"203.0.113.16"},
	"open.bigmodel.cn":        {"203.0.113.17"},
	"notbigmodel.cn":          {"203.0.113.18"},
	"birdapi.up.railway.app":  {"203.0.113.19"},
	"example.com":             {"203.0.113.20"},
	"api.example.com":         {"203.0.113.21"},
	"cw.example.com":          {"203.0.113.22"},
	"relay.example.com":       {"203.0.113.23"},
	"public-and-private.test": {"203.0.113.24", "10.0.0.5"},
}

func init() {
	net.DefaultResolver = newStubDNSResolver(stubDNSHosts)
}

// newStubDNSResolver 返回只回答 hosts 表的纯 Go 解析器；表外的名字应答 NXDOMAIN。
func newStubDNSResolver(hosts map[string][]string) *net.Resolver {
	table := make(map[string][]netip.Addr, len(hosts))
	for name, addresses := range hosts {
		for _, text := range addresses {
			table[strings.ToLower(name)] = append(table[strings.ToLower(name)], netip.MustParseAddr(text))
		}
	}
	return &net.Resolver{
		PreferGo: true,
		Dial: func(context.Context, string, string) (net.Conn, error) {
			client, server := net.Pipe()
			go serveStubDNS(server, table)
			return client, nil
		},
	}
}

// serveStubDNS 在管道另一端按“两字节长度加报文”的流式格式应答查询（Go 解析器对非 PacketConn 的连接使用该格式）。
func serveStubDNS(connection net.Conn, table map[string][]netip.Addr) {
	defer connection.Close()
	for {
		var prefix [2]byte
		if _, err := io.ReadFull(connection, prefix[:]); err != nil {
			return
		}
		request := make([]byte, binary.BigEndian.Uint16(prefix[:]))
		if _, err := io.ReadFull(connection, request); err != nil {
			return
		}
		var parser dnsmessage.Parser
		header, err := parser.Start(request)
		if err != nil {
			return
		}
		question, err := parser.Question()
		if err != nil {
			return
		}
		addresses, known := table[strings.ToLower(strings.TrimSuffix(question.Name.String(), "."))]
		code := dnsmessage.RCodeSuccess
		if !known {
			code = dnsmessage.RCodeNameError
		}
		builder := dnsmessage.NewBuilder(nil, dnsmessage.Header{ID: header.ID, Response: true, Authoritative: true, RecursionAvailable: true, RCode: code})
		builder.EnableCompression()
		if builder.StartQuestions() != nil || builder.Question(question) != nil || builder.StartAnswers() != nil {
			return
		}
		if known && question.Type == dnsmessage.TypeA {
			for _, address := range addresses {
				if !address.Is4() {
					continue
				}
				resource := dnsmessage.ResourceHeader{Name: question.Name, Class: dnsmessage.ClassINET, TTL: 60}
				if builder.AResource(resource, dnsmessage.AResource{A: address.As4()}) != nil {
					return
				}
			}
		}
		response, err := builder.Finish()
		if err != nil {
			return
		}
		reply := make([]byte, 2, 2+len(response))
		binary.BigEndian.PutUint16(reply, uint16(len(response)))
		if _, err := connection.Write(append(reply, response...)); err != nil {
			return
		}
	}
}

func TestStubDNSResolverAnswersOnlyListedHosts(t *testing.T) {
	ctx := context.Background()
	addresses, err := net.DefaultResolver.LookupIPAddr(ctx, "api.openai.com")
	require.NoError(t, err)
	require.Len(t, addresses, 1)
	require.Equal(t, "203.0.113.11", addresses[0].IP.String())

	_, err = net.DefaultResolver.LookupIPAddr(ctx, "no-such-host.invalid")
	var dnsErr *net.DNSError
	require.ErrorAs(t, err, &dnsErr)
	require.True(t, dnsErr.IsNotFound, "表外的域名应答 NXDOMAIN，与断网时的“解析失败”语义一致")
}

func TestStubDNSResolverKeepsSSRFDecisionsReal(t *testing.T) {
	ctx := context.Background()
	blocked, err := isPrivateOrLoopbackHost(ctx, "api.openai.com")
	require.NoError(t, err)
	require.False(t, blocked, "表里的公网文档地址不应被当作私网")
	blocked, err = isPrivateOrLoopbackHost(ctx, "public-and-private.test")
	require.NoError(t, err)
	require.True(t, blocked, "任一解析结果落在私网段即应拒绝，这条判定仍由生产代码给出")
	_, err = isPrivateOrLoopbackHost(ctx, "no-such-host.invalid")
	require.Error(t, err)
	require.ErrorIs(t, validateEndpoint("https://no-such-host.invalid"), ErrChannelMonitorEndpointUnreachable)
	require.NoError(t, validateEndpoint("https://api.openai.com"))
	require.ErrorIs(t, validateEndpoint("https://public-and-private.test"), ErrChannelMonitorEndpointPrivate)
}
