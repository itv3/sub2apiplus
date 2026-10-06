package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
	"weak"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// 正式 Forward 与 bridge 都先收到上游响应头，上传只读完前缀。上游完成响应后，
// 完整转发先返回并清空工作区，随后才继续读取剩余正文和 GetBody 重放。文件正文也必须
// 保留到实际 HTTP 上传读者关闭，不能把“响应读取结束”等同于“上传已经结束”。
func TestOfficialForwardBodyLifecycleEarlyResponseKeepsIndependentUpload(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, test := range []struct {
		bridge bool
		file   bool
	}{
		{}, {bridge: true}, {file: true}, {bridge: true, file: true},
	} {
		t.Run(fmt.Sprintf("bridge_%t_file_%t", test.bridge, test.file), func(t *testing.T) {
			t.Setenv("TMPDIR", t.TempDir())
			upstream := newOfficialForwardLifecycleUpstream()
			t.Cleanup(upstream.release)
			service := officialEgressWSHTTPBridgeTestService(upstream)
			size := 128 << 10
			if test.file {
				size = 3 << 20
			}
			body := buildOfficialEgressMemoryProfileBody(t, size)
			if test.bridge {
				body = officialEgressWSHTTPBridgePayload(t, body)
			}
			c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
			account := newOfficialOpenAIHTTPTestAccount(94)
			finished := make(chan error, 1)
			go func() {
				var err error
				if test.bridge {
					_, err = service.proxyOpenAIWSHTTPBridgeTurn(context.Background(), c, account,
						"oauth-token", body, len(body), "gpt-5.6-luna", "", "", "", "", 1,
						func([]byte) error { return nil })
				} else {
					_, err = service.Forward(context.Background(), c, account, body)
				}
				finished <- err
			}()
			select {
			case <-upstream.responseStarted:
			case err := <-finished:
				t.Fatalf("尚未进入响应读取便提前返回：%v", err)
			case <-time.After(10 * time.Second):
				t.Fatal("转发未进入受控响应读取")
			}
			select {
			case <-upstream.uploadStarted:
			case <-time.After(10 * time.Second):
				t.Fatal("上传未开始读取正文前缀")
			}
			workspace := officialForwardHTTPBodyFromContext(upstream.request.Context())
			require.NotNil(t, workspace)
			require.NotNil(t, workspace.releaseStorage, "响应头之后、完整转发结束之前必须保留范围")
			require.NotEmpty(t, workspace.ingress, "响应流仍处理中不得提前清空工作区")
			if test.file {
				require.Greater(t, upstream.request.ContentLength, int64(1<<20), "必须实际跨过压缩 wire 的落盘阈值")
				require.Equal(t, "zstd", upstream.request.Header.Get("Content-Encoding"))
				require.Equal(t, "*officialegress.requestBodySpoolReader", fmt.Sprintf("%T", upstream.request.Body), "必须覆盖普通文件，不能由内存回退冒充")
			} else {
				require.Less(t, upstream.request.ContentLength, int64(1<<20))
			}
			upstream.responseOnce.Do(func() { close(upstream.allowResponse) })
			select {
			case err := <-finished:
				require.NoError(t, err)
			case <-time.After(10 * time.Second):
				t.Fatal("完整响应之后 Forward/bridge 未返回")
			}
			assertOfficialForwardWorkspaceReleased(t, workspace)
			// Forward 已返回但原始 HTTP Body 仍在上传：此时 GetBody 也应能取得独立读者。
			replay, err := upstream.request.GetBody()
			require.NoError(t, err)
			defer replay.Close()
			hash := sha256.New()
			_, err = io.CopyN(hash, replay, 31)
			require.NoError(t, err)
			upstream.uploadOnce.Do(func() { close(upstream.allowUpload) })
			select {
			case err := <-upstream.uploadResult:
				require.NoError(t, err, "清空工作区不应改变已移交的只读正文")
			case <-time.After(10 * time.Second):
				t.Fatal("剩余上传未结束")
			}
			// 原上传已关闭；独立 GetBody 读者仍必须完成后半段，并在 Close 时释放最后租约。
			_, err = io.Copy(hash, replay)
			require.NoError(t, err)
			require.Equal(t, upstream.digest[:], hash.Sum(nil), "完整返回后的重放不得被工作区清理污染")
			require.NoError(t, replay.Close())
			if test.file {
				_, err := upstream.request.GetBody()
				require.ErrorIs(t, err, context.Canceled, "最后读者关闭后必须同步释放文件，即使 request/GetBody 仍被诊断保留")
			}
		})
	}
}

func TestOfficialForwardBodyLifecycleRetryKeepsWorkspaceUntilFinalReturn(t *testing.T) {
	var workspaces []*officialForwardHTTPBody
	tc := officialForwardBodyCase{
		name: "lifecycle_retry",
		context: func(_ *testing.T, body []byte) *gin.Context {
			return newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
		},
		responses: []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
			`{"error":{"code":"unsupported_parameter","message":"Unsupported parameter: input[3].status","param":"input[3].status","type":"invalid_request_error"}}`)},
		onBusiness: func(request *http.Request) {
			workspace := officialForwardHTTPBodyFromContext(request.Context())
			require.NotNil(t, workspace)
			require.NotNil(t, workspace.releaseStorage, "第二次 attempt 必须仍在本轮存储范围内")
			require.NotEmpty(t, workspace.ingress)
			workspaces = append(workspaces, workspace)
		},
	}
	outcome := officialForwardBodyRun(t, tc, newOfficialOpenAIHTTPTestBody(t, true, false, true), false)
	require.NoError(t, outcome.err)
	require.Len(t, workspaces, 2)
	require.Same(t, workspaces[0], workspaces[1], "同次 Forward 的重试共用工作区")
	assertOfficialForwardWorkspaceReleased(t, workspaces[0])
}

func TestOfficialForwardBodyLifecycleRetainedContextReleasesWorkspaceBacking(t *testing.T) {
	type payloadOwner struct{ body []byte }
	var retainedContext context.Context
	var ownerReference weak.Pointer[payloadOwner]
	var backingReference weak.Pointer[byte]
	func() {
		owner := &payloadOwner{body: bytes.Repeat([]byte("正文生命周期"), 4096)}
		ownerReference = weak.Make(owner)
		backingReference = weak.Make(&owner.body[0])
		ctx, workspace := newOfficialForwardHTTPBody(context.Background(), owner.body)
		workspace.scans = 3
		workspace.deferredBody = owner.body
		workspace.finalizerBody = owner.body
		workspace.finalizerPayload = map[string]any{"owner": owner}
		workspace.indexBody = owner.body
		workspace.rebuilt = owner.body
		workspace.members = []officialegress.JSONObjectMember{{Name: "input", ValueSegments: [][]byte{owner.body}}}
		workspace.spans = []officialForwardBodySpan{{source: owner.body}}
		workspace.viewState.body = owner.body
		retainedContext = context.WithoutCancel(ctx)
		workspace.closeStorage()
		workspace.closeStorage()
		assertOfficialForwardWorkspaceReleased(t, workspace)
		require.Equal(t, 3, workspace.scans)
	}()
	// 仅用 GC 证明诊断 context 的所有权，不是内存达标测量，也不进入生产代码。
	for attempt := 0; attempt < 5; attempt++ {
		runtime.GC()
		if ownerReference.Value() == nil && backingReference.Value() == nil {
			break
		}
	}
	require.Nil(t, ownerReference.Value(), "诊断 context 仍保活已结束的工作区对象树")
	require.Nil(t, backingReference.Value(), "诊断 context 仍保活已结束的工作区原文 backing")
	require.NotNil(t, officialForwardHTTPBodyFromContext(retainedContext), "仅清理内容，不破坏 context 中的工作区身份")
	runtime.KeepAlive(retainedContext)
}

func assertOfficialForwardWorkspaceReleased(t *testing.T, workspace *officialForwardHTTPBody) {
	t.Helper()
	require.Equal(t, officialForwardHTTPBody{scans: workspace.scans}, *workspace,
		"完整返回后仅保留扫描计数，释放原文、索引、对象树、成员、变量地址及存储范围")
}

type officialForwardLifecycleUpstream struct {
	request         *http.Request
	digest          [sha256.Size]byte
	responseStarted chan struct{}
	uploadStarted   chan struct{}
	allowResponse   chan struct{}
	allowUpload     chan struct{}
	uploadResult    chan error
	responseOnce    sync.Once
	uploadOnce      sync.Once
}

func newOfficialForwardLifecycleUpstream() *officialForwardLifecycleUpstream {
	return &officialForwardLifecycleUpstream{
		responseStarted: make(chan struct{}), uploadStarted: make(chan struct{}),
		allowResponse: make(chan struct{}), allowUpload: make(chan struct{}), uploadResult: make(chan error, 1),
	}
}

func (u *officialForwardLifecycleUpstream) release() {
	u.responseOnce.Do(func() { close(u.allowResponse) })
	u.uploadOnce.Do(func() { close(u.allowUpload) })
}

func (u *officialForwardLifecycleUpstream) Do(request *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	return u.DoWithTLS(request, "", 0, 0, nil)
}

func (u *officialForwardLifecycleUpstream) DoWithTLS(request *http.Request, _ string, _ int64, _ int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	if strings.Contains(request.URL.Path, "/codex/models") {
		return &http.Response{StatusCode: http.StatusOK,
			Header: http.Header{"Content-Type": []string{"application/json"}},
			Body:   io.NopCloser(strings.NewReader(codexModelsRecorderManifest))}, nil
	}
	u.request = request
	replay, err := request.GetBody()
	if err != nil {
		return nil, err
	}
	wire, err := io.ReadAll(replay)
	_ = replay.Close()
	if err != nil {
		return nil, err
	}
	u.digest = sha256.Sum256(wire)
	go func() {
		hash := sha256.New()
		prefix := make([]byte, 17)
		n, err := io.ReadFull(request.Body, prefix)
		_, _ = hash.Write(prefix[:n])
		close(u.uploadStarted)
		<-u.allowUpload
		if err == nil {
			_, err = io.Copy(hash, request.Body)
		}
		if err == nil && !bytes.Equal(hash.Sum(nil), u.digest[:]) {
			err = fmt.Errorf("异步正文摘要与定型重放不一致")
		}
		_ = request.Body.Close()
		u.uploadResult <- err
	}()
	response := newOfficialOpenAIHTTPSSECompletedResponse("resp_lifecycle")
	response.Body = &officialForwardLifecycleResponse{
		ReadCloser: response.Body, started: u.responseStarted, proceed: u.allowResponse,
	}
	return response, nil
}

type officialForwardLifecycleResponse struct {
	io.ReadCloser
	started chan struct{}
	proceed <-chan struct{}
	once    sync.Once
}

func (r *officialForwardLifecycleResponse) Read(p []byte) (int, error) {
	r.once.Do(func() { close(r.started) })
	<-r.proceed
	return r.ReadCloser.Read(p)
}
