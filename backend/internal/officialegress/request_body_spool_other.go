//go:build !linux && !darwin

package officialegress

import "os"

// 未验证的平台保留原分段正文，不能假设支持解除打开文件的目录链接。
func requestBodySpoolAllowed(string) bool { return false }

func configureRequestBodySpool(*os.File) error { return nil }

func finishRequestBodySpool(*os.File) error { return nil }

func flushRequestBodySpoolWriteCache(*os.File, int64, int64) error { return nil }

func releaseRequestBodySpoolReadCache(*os.File, int64, int64) error { return nil }
