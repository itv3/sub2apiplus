// Command egresscatalogstage 把已批准完整画像编译为不切 Active 的候选目录。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

var safeProfileIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

type approvedProfileManifest struct {
	SchemaVersion        string          `json:"schema_version"`
	CodexVersion         string          `json:"codex_version"`
	ProfileID            string          `json:"profile_id"`
	ProfileDigest        string          `json:"profile_digest"`
	ProfilePayload       json.RawMessage `json:"profile_payload"`
	ProfilePayloadSHA256 string          `json:"profile_payload_sha256"`
	Status               string          `json:"status"`
}

type inventoryEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int    `json:"size"`
}

func main() {
	prepareSnapshot := flag.String("prepare-snapshot", "", "待规范化的完整 Snapshot JSON")
	prepareProfileID := flag.String("prepare-profile-id", "", "待审核画像 ID")
	prepareOutput := flag.String("prepare-output", "", "不存在的 draft profile.json")
	profileManifest := flag.String("profile-manifest", "", "classify 封存的 approved profile.json")
	campaignID := flag.String("campaign-id", "", "不可变 Campaign ID")
	classificationSHA := flag.String("classification-sha256", "", "五份批准清单联合摘要")
	output := flag.String("output", "", "不存在的候选目录绝对路径")
	flag.Parse()
	if *prepareSnapshot != "" || *prepareProfileID != "" || *prepareOutput != "" {
		if *profileManifest != "" || *campaignID != "" || *classificationSHA != "" || *output != "" {
			fmt.Fprintln(os.Stderr, "画像草案生成失败：prepare 与 stage 参数不能混用")
			os.Exit(2)
		}
		result, err := prepareProfileManifest(*prepareSnapshot, *prepareProfileID, *prepareOutput)
		if err != nil {
			fmt.Fprintf(os.Stderr, "画像草案生成失败：%v\n", err)
			os.Exit(1)
		}
		raw, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(raw))
		return
	}
	result, err := stageApprovedProfile(
		*profileManifest,
		*campaignID,
		*classificationSHA,
		*output,
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "候选目录生成失败：%v\n", err)
		os.Exit(1)
	}
	raw, _ := json.MarshalIndent(result, "", "  ")
	fmt.Println(string(raw))
}

func prepareProfileManifest(
	snapshotPath string,
	profileID string,
	output string,
) (approvedProfileManifest, error) {
	if snapshotPath == "" || output == "" || !safeProfileIDPattern.MatchString(profileID) {
		return approvedProfileManifest{}, errors.New("prepare 参数不完整或 profile ID 非法")
	}
	info, err := os.Lstat(snapshotPath)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return approvedProfileManifest{}, errors.New("Snapshot 必须是非符号链接普通文件")
	}
	raw, err := os.ReadFile(snapshotPath)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	snapshot, err := profilecontract.ParseSnapshot(raw)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	snapshot, err = profilecontract.PrepareSnapshotForManifest(snapshot)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	payload, err := json.Marshal(snapshot)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	var canonicalPayload any
	if err := json.Unmarshal(payload, &canonicalPayload); err != nil {
		return approvedProfileManifest{}, err
	}
	manifest := approvedProfileManifest{
		SchemaVersion:        "codex-egress-profile/v1",
		CodexVersion:         snapshot.Version,
		ProfileID:            profileID,
		ProfileDigest:        snapshot.Digest,
		ProfilePayload:       payload,
		ProfilePayloadSHA256: canonicalSHA256(canonicalPayload),
		Status:               "draft",
	}
	manifestRaw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return approvedProfileManifest{}, err
	}
	manifestRaw = append(manifestRaw, '\n')
	if !filepath.IsAbs(output) || filepath.Clean(output) != output {
		return approvedProfileManifest{}, errors.New("prepare-output 必须是规范绝对路径")
	}
	if err := validateNewOutputParent(output); err != nil {
		return approvedProfileManifest{}, err
	}
	if _, err := os.Lstat(output); !errors.Is(err, os.ErrNotExist) {
		if err == nil {
			return approvedProfileManifest{}, errors.New("prepare-output 已存在，禁止覆盖")
		}
		return approvedProfileManifest{}, err
	}
	if err := writeExclusive(output, manifestRaw); err != nil {
		return approvedProfileManifest{}, err
	}
	return manifest, nil
}

func stageApprovedProfile(
	manifestPath string,
	campaignID string,
	classificationSHA string,
	output string,
) (map[string]any, error) {
	manifest, err := readApprovedProfileManifest(manifestPath)
	if err != nil {
		return nil, err
	}
	staged, err := officialegress.BuildStagedReleaseCatalog(
		officialegress.DefaultReleaseCatalog(),
		officialegress.CatalogStageInput{
			TargetVersion:     manifest.CodexVersion,
			ProfileID:         manifest.ProfileID,
			ProfileDigest:     manifest.ProfileDigest,
			ProfilePayload:    manifest.ProfilePayload,
			CampaignID:        campaignID,
			ClassificationSHA: classificationSHA,
		},
	)
	if err != nil {
		return nil, err
	}
	files, err := staged.RuntimeCatalogFiles()
	if err != nil {
		return nil, err
	}
	assets := make(map[string][]byte, len(files)+3)
	for _, file := range files {
		assets[filepath.ToSlash(filepath.Join("catalogdata/runtime", file.Path))] = file.Data
	}
	graphRaw, err := staged.ContractReleaseGraphJSON()
	if err != nil {
		return nil, err
	}
	assets["releasecontract/testdata/release-graph.json"] = graphRaw
	catalogRaw, err := staged.ContractSnapshotCatalogJSON()
	if err != nil {
		return nil, err
	}
	assets["profilecontract/testdata/snapshot-catalog.json"] = catalogRaw
	snapshotPath, snapshotRaw := staged.TargetSnapshot()
	assets[filepath.ToSlash(filepath.Join("profilecontract/testdata", snapshotPath))] = snapshotRaw

	receipt := staged.CatalogStageReceiptCore()
	inventory := stageInventory(assets)
	receipt["inventory"] = inventory
	receipt["inventory_sha256"] = canonicalSHA256(inventory)
	receiptRaw, err := json.MarshalIndent(receipt, "", "  ")
	if err != nil {
		return nil, err
	}
	receiptRaw = append(receiptRaw, '\n')
	assets["catalog-stage-receipt.json"] = receiptRaw
	if err := writeStageDirectory(output, assets); err != nil {
		return nil, err
	}
	return receipt, nil
}

func readApprovedProfileManifest(pathValue string) (approvedProfileManifest, error) {
	if pathValue == "" {
		return approvedProfileManifest{}, errors.New("必须提供 -profile-manifest")
	}
	info, err := os.Lstat(pathValue)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
		return approvedProfileManifest{}, errors.New("profile manifest 必须是非符号链接普通文件")
	}
	raw, err := os.ReadFile(pathValue)
	if err != nil {
		return approvedProfileManifest{}, err
	}
	var manifest approvedProfileManifest
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return approvedProfileManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return approvedProfileManifest{}, errors.New("profile manifest 含多余 JSON")
	}
	if manifest.SchemaVersion != "codex-egress-profile/v1" || manifest.Status != "approved" ||
		manifest.CodexVersion == "" || manifest.ProfileID == "" ||
		manifest.ProfileDigest == "" || len(manifest.ProfilePayload) == 0 {
		return approvedProfileManifest{}, errors.New("profile manifest 未批准或坐标不完整")
	}
	var payload any
	if err := json.Unmarshal(manifest.ProfilePayload, &payload); err != nil {
		return approvedProfileManifest{}, err
	}
	if canonicalSHA256(payload) != manifest.ProfilePayloadSHA256 {
		return approvedProfileManifest{}, errors.New("profile_payload_sha256 不一致")
	}
	return manifest, nil
}

func writeStageDirectory(output string, assets map[string][]byte) error {
	if !filepath.IsAbs(output) || filepath.Clean(output) != output {
		return errors.New("-output 必须是规范绝对路径")
	}
	home, _ := os.UserHomeDir()
	if output == string(filepath.Separator) || output == os.TempDir() || output == home {
		return errors.New("-output 不能是根目录、HOME 或临时目录本身")
	}
	if _, err := os.Lstat(output); !errors.Is(err, os.ErrNotExist) {
		if err == nil {
			return errors.New("-output 已存在，禁止覆盖")
		}
		return err
	}
	if err := validateNewOutputParent(output); err != nil {
		return err
	}
	parent := filepath.Dir(output)
	temporary, err := os.MkdirTemp(parent, ".egresscatalogstage-*")
	if err != nil {
		return err
	}
	defer func() { _ = os.RemoveAll(temporary) }()
	paths := make([]string, 0, len(assets))
	for relativePath := range assets {
		if !filepath.IsLocal(relativePath) || filepath.Clean(relativePath) != relativePath {
			return fmt.Errorf("候选资产路径非法：%s", relativePath)
		}
		paths = append(paths, relativePath)
	}
	sort.Strings(paths)
	for _, relativePath := range paths {
		raw := assets[relativePath]
		target := filepath.Join(temporary, filepath.FromSlash(relativePath))
		if err := writeExclusive(target, raw); err != nil {
			return err
		}
	}
	if err := os.Rename(temporary, output); err != nil {
		return err
	}
	return nil
}

// validateNewOutputParent 要求离线产物落在已存在且完全解析的真实目录中，
// 避免通过父目录符号链接把不可变候选资产写到审核范围外。
func validateNewOutputParent(output string) error {
	parent := filepath.Dir(output)
	parentInfo, err := os.Lstat(parent)
	if err != nil || !parentInfo.IsDir() || parentInfo.Mode()&os.ModeSymlink != 0 {
		return errors.New("输出父目录必须是已存在的非符号链接目录")
	}
	resolvedParent, err := filepath.EvalSymlinks(parent)
	if err != nil || resolvedParent != parent {
		return errors.New("输出父路径包含符号链接")
	}
	return nil
}

func stageInventory(assets map[string][]byte) []inventoryEntry {
	paths := make([]string, 0, len(assets))
	for relativePath := range assets {
		paths = append(paths, relativePath)
	}
	sort.Strings(paths)
	inventory := make([]inventoryEntry, 0, len(paths))
	for _, relativePath := range paths {
		raw := assets[relativePath]
		sum := sha256.Sum256(raw)
		inventory = append(inventory, inventoryEntry{
			Path: relativePath, SHA256: hex.EncodeToString(sum[:]), Size: len(raw),
		})
	}
	return inventory
}

func writeExclusive(target string, raw []byte) error {
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	handle, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err = handle.Write(raw); err == nil {
		err = handle.Close()
	} else {
		_ = handle.Close()
	}
	return err
}

func canonicalSHA256(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
