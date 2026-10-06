package service

import "context"

type openAIBorrowedIngressBodyContextKey struct{}

// WithOpenAIBorrowedIngressBody 表明入口正文由 handler 的显式租约管理。
// 官方 HTTP JSON 编译同步生成独立 wire，允许在 handler 全部处理结束后释放原文；
// 其余转发路径必须先取得独立堆副本，不能让异步上传或 GetBody 借用入口租约。
// 这里只保存标记，不把正文、租约或请求工作区放入可能被后台任务持有的 context。
func WithOpenAIBorrowedIngressBody(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, openAIBorrowedIngressBodyContextKey{}, true)
}

func openAIBorrowsIngressBody(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	borrowed, _ := ctx.Value(openAIBorrowedIngressBodyContextKey{}).(bool)
	return borrowed
}
