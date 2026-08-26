// Command egressruntimedump 确定性导出正式版本数据的内容寻址目录。
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func main() {
	output := flag.String("output", "", "正式版本数据输出目录")
	flag.Parse()
	if *output == "" {
		fmt.Fprintln(os.Stderr, "导出失败：必须提供 -output")
		os.Exit(2)
	}
	files, err := officialegress.DefaultReleaseCatalog().RuntimeCatalogArchiveFiles()
	if err == nil {
		err = writeRuntimeCatalog(*output, files)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "导出失败：%v\n", err)
		os.Exit(1)
	}
	fmt.Printf("已确定性导出 %d 个正式版本数据文件到 %s\n", len(files), *output)
}

func writeRuntimeCatalog(output string, files []officialegress.RuntimeCatalogFile) error {
	if output == "" || len(files) == 0 {
		return errors.New("输出目录或正式版本数据为空")
	}
	for _, file := range files {
		if file.Path == "release-catalog.json" {
			continue
		}
		if err := writeImmutableRuntimeBlob(output, file); err != nil {
			return err
		}
	}
	for _, file := range files {
		if file.Path == "release-catalog.json" {
			return writeRuntimeSelector(output, file)
		}
	}
	return errors.New("正式版本数据缺少 release-catalog.json selector")
}

func runtimeCatalogTarget(output, relativePath string) (string, error) {
	if !filepath.IsLocal(relativePath) || filepath.Clean(relativePath) != relativePath {
		return "", fmt.Errorf("正式版本数据路径非法：%s", relativePath)
	}
	return filepath.Join(output, filepath.FromSlash(relativePath)), nil
}

func writeImmutableRuntimeBlob(output string, file officialegress.RuntimeCatalogFile) error {
	target, err := runtimeCatalogTarget(output, file.Path)
	if err != nil {
		return err
	}
	if existing, readErr := os.ReadFile(target); readErr == nil {
		if bytes.Equal(existing, file.Data) {
			return nil
		}
		return fmt.Errorf("内容寻址 blob 已存在但字节不同：%s", file.Path)
	} else if !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	handle, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err = handle.Write(file.Data); err == nil {
		err = handle.Close()
	} else {
		_ = handle.Close()
	}
	if err != nil {
		_ = os.Remove(target)
	}
	return err
}

func writeRuntimeSelector(output string, file officialegress.RuntimeCatalogFile) error {
	target, err := runtimeCatalogTarget(output, file.Path)
	if err != nil {
		return err
	}
	if existing, readErr := os.ReadFile(target); readErr == nil && bytes.Equal(existing, file.Data) {
		return nil
	} else if readErr != nil && !errors.Is(readErr, os.ErrNotExist) {
		return readErr
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(target), ".release-catalog-*.tmp")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer func() { _ = os.Remove(temporaryPath) }()
	if err := temporary.Chmod(0o644); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(file.Data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryPath, target)
}
