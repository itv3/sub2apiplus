package officialegress

import "testing"

// 窗口号随上下文压缩推进（CodexIdentityFacts.WindowAdvances，由画像 TurnMetadata.WindowTracking 触发）：
//   - 不声明时 invocation 摘要仍绑定窗口标识（旧行为不变），窗口号不同的两份事实摘要不同、帧身份校验失败；
//   - 声明后 invocation 摘要不绑定窗口标识（同一次调用的新 attempt 允许窗口号不同），帧里的窗口号可以与建连时不同，
//     但线程部分必须不变，且只允许数字后缀变化。

func windowFacts(t *testing.T, window string, advances bool) CodexIdentityFacts {
	t.Helper()
	value, err := NewCodexIdentityValue(window, IdentitySourceInvocation, IdentityLifecycleSession)
	if err != nil {
		t.Fatal(err)
	}
	return CodexIdentityFacts{WindowID: value, WindowAdvances: advances}
}

func TestInvocationDigestBindsWindowOnlyWhenItDoesNotAdvance(t *testing.T) {
	if windowFacts(t, "thread-1:0", false).invocationDigest() == windowFacts(t, "thread-1:1", false).invocationDigest() {
		t.Fatal("不推进窗口号时 invocation 摘要必须绑定窗口标识")
	}
	if windowFacts(t, "thread-1:0", true).invocationDigest() != windowFacts(t, "thread-1:1", true).invocationDigest() {
		t.Fatal("推进窗口号时 invocation 摘要不得绑定窗口标识")
	}
	// WindowAdvances 取 false 时序列化形态与旧结构逐字节相同（omitempty），旧画像的身份事实摘要不变。
	legacy := windowFacts(t, "thread-1:0", false)
	if legacy.Digest() != (CodexIdentityFacts{WindowID: legacy.WindowID}).Digest() {
		t.Fatal("WindowAdvances=false 不得改变身份事实摘要")
	}
}

func TestValidateWebSocketFrameIdentityAllowsOnlyWindowNumberAdvance(t *testing.T) {
	cases := []struct {
		name    string
		base    CodexIdentityFacts
		current CodexIdentityFacts
		wantErr bool
	}{
		{"不推进：窗口号变化失败", windowFacts(t, "thread-1:0", false), windowFacts(t, "thread-1:1", false), true},
		{"推进：数字后缀变化允许", windowFacts(t, "thread-1:0", true), windowFacts(t, "thread-1:1", true), false},
		{"推进：线程变化失败", windowFacts(t, "thread-1:0", true), windowFacts(t, "thread-2:1", true), true},
		{"推进：后缀不是数字失败", windowFacts(t, "thread-1:0", true), windowFacts(t, "thread-1:x", true), true},
		{"只有一侧声明推进失败", windowFacts(t, "thread-1:0", true), windowFacts(t, "thread-1:1", false), true},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			err := validateWebSocketFrameIdentity(item.base, item.current)
			if (err != nil) != item.wantErr {
				t.Fatalf("期望出错=%v，实际 %v", item.wantErr, err)
			}
		})
	}
}
