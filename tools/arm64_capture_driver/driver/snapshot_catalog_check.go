// 快照合并使用候选源码里的 Go 合同校验，避免 Python 猜测 DTO 字段顺序或官方 Digest 算法。
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	p "github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

type catalogCheck struct {
	Name    string          `json:"name"`
	Catalog json.RawMessage `json:"catalog"`
	Roots   []string        `json:"roots"`
}

func readPlain(root, relative string) ([]byte, error) {
	file := filepath.Join(root, filepath.FromSlash(relative))
	for current := file; ; current = filepath.Dir(current) {
		info, err := os.Lstat(current)
		if err != nil {
			return nil, err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("快照路径含符号链接: %s", current)
		}
		if current == filepath.Dir(current) {
			break
		}
	}
	return os.ReadFile(file)
}

func verify(check catalogCheck) error {
	doc, err := p.ParseSnapshotCatalog(check.Catalog)
	if err != nil {
		return err
	}
	_, err = p.NewSnapshotCatalog(doc, func(relative string) ([]byte, error) {
		for _, root := range check.Roots {
			data, readErr := readPlain(root, relative)
			if errors.Is(readErr, os.ErrNotExist) {
				continue
			}
			return data, readErr
		}
		return nil, fmt.Errorf("所有登记目录均缺少快照: %s", relative)
	})
	return err
}

func main() {
	var checks []catalogCheck
	decoder := json.NewDecoder(os.Stdin)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&checks); err != nil {
		fmt.Fprintln(os.Stderr, "快照校验输入非法:", err)
		os.Exit(3)
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) || len(checks) == 0 {
		fmt.Fprintln(os.Stderr, "快照校验输入为空或有尾随数据")
		os.Exit(3)
	}
	for _, check := range checks {
		if err := verify(check); err != nil {
			fmt.Fprintln(os.Stderr, "快照目录闭合失败:", check.Name, err)
			os.Exit(3)
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"status": "passed", "catalog_count": len(checks)}); err != nil {
		fmt.Fprintln(os.Stderr, "快照校验结果写出失败:", err)
		os.Exit(3)
	}
}
