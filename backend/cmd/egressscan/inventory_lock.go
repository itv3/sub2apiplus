package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// bootstrapInventoryLock 是对“锚点提交之后重建的历史 inventory”建立的显式信任锚。
// 它同时固定历史树、inventory 原文和执行回放的扫描算法，避免同时修改分类规则与
// 基线即可重写历史。
type bootstrapInventoryLock struct {
	SchemaVersion          int    `json:"schema_version"`
	BootstrapCommit        string `json:"bootstrap_commit"`
	BootstrapTree          string `json:"bootstrap_tree"`
	InventorySHA256        string `json:"inventory_sha256"`
	ScannerAlgorithmSHA256 string `json:"scanner_algorithm_sha256"`
	ReconstructedAt        string `json:"reconstructed_at"`
	ReviewedBy             string `json:"reviewed_by"`
	ReviewRef              string `json:"review_ref"`
	Rationale              string `json:"rationale"`
}

func verifyBootstrapInventoryLock(lockPath string, baselineRaw []byte, scannerSourceRoot string) error {
	if strings.TrimSpace(lockPath) == "" || strings.TrimSpace(scannerSourceRoot) == "" {
		return errors.New("必须同时提供 -inventory-lock 与 -scanner-source-root")
	}
	raw, err := os.ReadFile(lockPath)
	if err != nil {
		return err
	}
	var lock bootstrapInventoryLock
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&lock); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("bootstrap inventory lock 尾部存在额外 JSON")
	}
	if lock.SchemaVersion != 1 || lock.BootstrapCommit != legacyBootstrapCommit ||
		lock.BootstrapTree != legacyBootstrapTree || !validDigest(lock.InventorySHA256) ||
		!validDigest(lock.ScannerAlgorithmSHA256) || strings.TrimSpace(lock.ReconstructedAt) == "" ||
		strings.TrimSpace(lock.ReviewedBy) == "" || strings.TrimSpace(lock.ReviewRef) == "" ||
		strings.TrimSpace(lock.Rationale) == "" {
		return errors.New("bootstrap inventory lock 字段非法")
	}
	inventorySum := sha256.Sum256(baselineRaw)
	if hex.EncodeToString(inventorySum[:]) != lock.InventorySHA256 {
		return errors.New("bootstrap inventory 原文摘要与 lock 不一致")
	}
	algorithmDigest, err := scannerAlgorithmDigest(scannerSourceRoot)
	if err != nil {
		return err
	}
	if algorithmDigest != lock.ScannerAlgorithmSHA256 {
		return fmt.Errorf("扫描算法已变化但 lock 未复审：lock=%s current=%s",
			lock.ScannerAlgorithmSHA256, algorithmDigest)
	}
	return nil
}

func scannerAlgorithmDigest(root string) (string, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return "", err
	}
	var names []string
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") || strings.HasSuffix(entry.Name(), "_test.go") {
			continue
		}
		names = append(names, entry.Name())
	}
	sort.Strings(names)
	if len(names) == 0 {
		return "", errors.New("scanner source root 中没有生产 Go 源码")
	}
	hash := sha256.New()
	for _, name := range names {
		raw, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			return "", err
		}
		_, _ = hash.Write([]byte(name))
		_, _ = hash.Write([]byte{0})
		_, _ = hash.Write(raw)
		_, _ = hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
