// Command egresscatalogpromote 把已验收的候选 Release 提升为生产 Active。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

type inventoryEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int    `json:"size"`
}

func main() {
	campaignID := flag.String("campaign-id", "", "已验收 Campaign ID")
	acceptanceSHA := flag.String("acceptance-sha256", "", "acceptance result SHA-256")
	targetVersion := flag.String("target-version", "", "待提升为 Active 的版本")
	targetProfileDigest := flag.String("target-profile-digest", "", "待提升画像摘要")
	rollbackVersion := flag.String("rollback-version", "", "待固化为 Previous 的版本")
	rollbackProfileDigest := flag.String("rollback-profile-digest", "", "回滚画像摘要")
	output := flag.String("output", "", "不存在的提升目录绝对路径")
	flag.Parse()

	receipt, err := promoteCatalog(officialegress.CatalogPromotionInput{
		CampaignID:            *campaignID,
		AcceptanceSHA256:      *acceptanceSHA,
		TargetVersion:         *targetVersion,
		TargetProfileDigest:   *targetProfileDigest,
		RollbackVersion:       *rollbackVersion,
		RollbackProfileDigest: *rollbackProfileDigest,
	}, *output)
	if err != nil {
		fmt.Fprintf(os.Stderr, "生产目录提升失败：%v\n", err)
		os.Exit(1)
	}
	raw, _ := json.MarshalIndent(receipt, "", "  ")
	fmt.Println(string(raw))
}

func promoteCatalog(input officialegress.CatalogPromotionInput, output string) (map[string]any, error) {
	promoted, err := officialegress.BuildPromotedReleaseCatalog(
		officialegress.DefaultReleaseCatalog(),
		input,
	)
	if err != nil {
		return nil, err
	}
	files, err := promoted.RuntimeCatalogFiles()
	if err != nil {
		return nil, err
	}
	assets := make(map[string][]byte, len(files)+2)
	for _, file := range files {
		assets[filepath.ToSlash(filepath.Join("catalogdata/runtime", file.Path))] = file.Data
	}
	graphRaw, err := promoted.ContractReleaseGraphJSON()
	if err != nil {
		return nil, err
	}
	assets["releasecontract/testdata/release-graph.json"] = graphRaw

	receipt := promoted.CatalogPromotionReceiptCore()
	inventory := buildInventory(assets)
	receipt["inventory"] = inventory
	receipt["inventory_sha256"] = valueSHA256(inventory)
	receiptRaw, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, err
	}
	assets["catalog-promotion-receipt.json"] = append(receiptRaw, '\n')
	if err := writeOutputDirectory(output, assets); err != nil {
		return nil, err
	}
	return receipt, nil
}

func buildInventory(assets map[string][]byte) []inventoryEntry {
	paths := make([]string, 0, len(assets))
	for path := range assets {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	out := make([]inventoryEntry, 0, len(paths))
	for _, path := range paths {
		sum := sha256.Sum256(assets[path])
		out = append(out, inventoryEntry{
			Path: path, SHA256: hex.EncodeToString(sum[:]), Size: len(assets[path]),
		})
	}
	return out
}

func valueSHA256(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func writeOutputDirectory(output string, assets map[string][]byte) (err error) {
	if !filepath.IsAbs(output) || filepath.Clean(output) != output {
		return errors.New("-output 必须是规范绝对路径")
	}
	home, _ := os.UserHomeDir()
	if output == string(filepath.Separator) || output == os.TempDir() || output == home {
		return errors.New("-output 不能是根目录、HOME 或临时目录本身")
	}
	if _, statErr := os.Lstat(output); !errors.Is(statErr, os.ErrNotExist) {
		if statErr == nil {
			return errors.New("-output 已存在，禁止覆盖")
		}
		return statErr
	}
	parent := filepath.Dir(output)
	info, statErr := os.Stat(parent)
	if statErr != nil || !info.IsDir() {
		return errors.New("-output 父目录不存在或不是目录")
	}
	resolvedParent, evalErr := filepath.EvalSymlinks(parent)
	if evalErr != nil || resolvedParent != parent {
		return errors.New("-output 父目录不能经过符号链接")
	}
	if err := os.Mkdir(output, 0o700); err != nil {
		return err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(output)
		}
	}()
	paths := make([]string, 0, len(assets))
	for path := range assets {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	for _, relative := range paths {
		clean := filepath.Clean(filepath.FromSlash(relative))
		if clean == "." || filepath.IsAbs(clean) || clean == ".." ||
			len(clean) > 3 && clean[:3] == ".."+string(filepath.Separator) {
			return errors.New("提升输出路径越过根目录")
		}
		target := filepath.Join(output, clean)
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return err
		}
		file, openErr := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
		if openErr != nil {
			return openErr
		}
		_, writeErr := file.Write(assets[relative])
		closeErr := file.Close()
		if writeErr != nil {
			return writeErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}
