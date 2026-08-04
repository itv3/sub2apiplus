package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/sealcontract"
)

const (
	protectedReceiptPath     = "docs/changeset1a/legacy-seal-receipt.json"
	protectedCeilingPath     = "docs/changeset1a/legacy-ceiling.json"
	protectedSupplementsPath = "docs/changeset1a/pre-bootstrap-supplements.json"
	protectedBaselinePath    = "docs/changeset1a/legacy-baseline.json"
)

func main() {
	receiptPath := flag.String("receipt", "", "legacy seal receipt 路径")
	ceilingPath := flag.String("ceiling", "", "legacy ceiling 路径")
	supplementsPath := flag.String("supplements", "", "pre-bootstrap supplements 路径")
	baselinePath := flag.String("baseline", "", "当前 legacy baseline 路径")
	protectedBaseRef := flag.String("protected-base-ref", "", "CI 提供的受保护目标分支 commit/ref")
	flag.Parse()
	if err := run(*receiptPath, *ceilingPath, *supplementsPath, *baselinePath, *protectedBaseRef); err != nil {
		fmt.Fprintf(os.Stderr, "🔴 legacy 封存门禁失败：%v\n", err)
		os.Exit(1)
	}
}

func run(receiptPath, ceilingPath, supplementsPath, baselinePath, protectedBaseRef string) error {
	current, err := readCurrentAssets(receiptPath, ceilingPath, supplementsPath, baselinePath)
	if err != nil {
		return err
	}
	receipt, err := sealcontract.VerifyCurrent(current)
	if err != nil {
		return err
	}
	protectedBaseRef = strings.TrimSpace(protectedBaseRef)
	if protectedBaseRef == "" {
		if receipt.Lifecycle == sealcontract.LifecycleSealed {
			return errors.New("sealed 状态缺少 EGRESS_SEAL_BASE_REF，无法建立工作区外信任锚")
		}
		fmt.Println("✅ legacy 封存门禁通过：当前为 provisional，尚未建立 seal")
		return nil
	}

	commit, err := resolveCommit(protectedBaseRef)
	if err != nil {
		return err
	}
	protected, err := readProtectedAssets(commit)
	if err != nil {
		// 首次引入 receipt 时，受保护基准可能尚无该文件。仍需读取旧 ceiling，
		// 防止在已有 sealed 基准上借“首次引入”降级。
		baseCeiling, ceilingErr := gitShow(commit, protectedCeilingPath)
		if ceilingErr == nil {
			lifecycle, lifecycleErr := sealcontract.CeilingLifecycle(baseCeiling)
			if lifecycleErr != nil {
				return lifecycleErr
			}
			if lifecycle == sealcontract.LifecycleSealed {
				return errors.New("受保护基准 ceiling 已 sealed，但缺少可验证 seal receipt")
			}
		}
		if receipt.Lifecycle == sealcontract.LifecycleSealed {
			return fmt.Errorf("首次 sealed 前受保护基准必须先包含 provisional receipt: %w", err)
		}
		fmt.Println("✅ legacy 封存门禁通过：受保护基准尚未引入 provisional receipt")
		return nil
	}
	if err := sealcontract.VerifyProtectedBase(current, protected, commit); err != nil {
		return err
	}
	fmt.Printf("✅ legacy 封存门禁通过：受保护基准=%s lifecycle=%s\n", commit, receipt.Lifecycle)
	return nil
}

func readCurrentAssets(
	receiptPath,
	ceilingPath,
	supplementsPath,
	baselinePath string,
) (sealcontract.Assets, error) {
	paths := []string{receiptPath, ceilingPath, supplementsPath, baselinePath}
	for _, path := range paths {
		if strings.TrimSpace(path) == "" {
			return sealcontract.Assets{}, errors.New("必须提供 receipt、ceiling、supplements 与 baseline 路径")
		}
	}
	receiptRaw, err := os.ReadFile(filepath.Clean(receiptPath))
	if err != nil {
		return sealcontract.Assets{}, err
	}
	ceilingRaw, err := os.ReadFile(filepath.Clean(ceilingPath))
	if err != nil {
		return sealcontract.Assets{}, err
	}
	supplementsRaw, err := os.ReadFile(filepath.Clean(supplementsPath))
	if err != nil {
		return sealcontract.Assets{}, err
	}
	baselineRaw, err := os.ReadFile(filepath.Clean(baselinePath))
	if err != nil {
		return sealcontract.Assets{}, err
	}
	return sealcontract.Assets{
		ReceiptRaw: receiptRaw, CeilingRaw: ceilingRaw, SupplementsRaw: supplementsRaw,
		BaselineRaw: baselineRaw,
	}, nil
}

func readProtectedAssets(commit string) (sealcontract.Assets, error) {
	receiptRaw, err := gitShow(commit, protectedReceiptPath)
	if err != nil {
		return sealcontract.Assets{}, err
	}
	ceilingRaw, err := gitShow(commit, protectedCeilingPath)
	if err != nil {
		return sealcontract.Assets{}, err
	}
	supplementsRaw, err := gitShow(commit, protectedSupplementsPath)
	if err != nil {
		return sealcontract.Assets{}, err
	}
	baselineRaw, err := gitShow(commit, protectedBaselinePath)
	if err != nil {
		return sealcontract.Assets{}, err
	}
	return sealcontract.Assets{
		ReceiptRaw: receiptRaw, CeilingRaw: ceilingRaw, SupplementsRaw: supplementsRaw,
		BaselineRaw: baselineRaw,
	}, nil
}

func resolveCommit(ref string) (string, error) {
	if strings.HasPrefix(ref, "-") || strings.ContainsAny(ref, "\x00\r\n") {
		return "", errors.New("受保护基准 ref 非法")
	}
	command := exec.Command("git", "rev-parse", "--verify", ref+"^{commit}")
	raw, err := command.Output()
	if err != nil {
		return "", fmt.Errorf("解析受保护基准 ref %q: %w", ref, err)
	}
	commit := strings.TrimSpace(string(raw))
	if !sealcontract.ValidGitObjectID(commit) {
		return "", errors.New("受保护基准未解析为完整 commit SHA")
	}
	return commit, nil
}

func gitShow(commit, path string) ([]byte, error) {
	command := exec.Command("git", "show", commit+":"+path)
	raw, err := command.Output()
	if err != nil {
		return nil, fmt.Errorf("读取受保护基准 %s:%s: %w", commit, path, err)
	}
	return raw, nil
}
