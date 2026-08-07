package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"regexp"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

var catalogStageVersionPattern = regexp.MustCompile(`^\d+\.\d+\.\d+$`)

// CatalogStageInput 是把已批准完整画像编译成候选 RuntimeCatalog 的离线输入。
// 目标画像只进入 Previous 候选槽位，当前 Active 保持不变；生产启用必须另走切换门禁。
type CatalogStageInput struct {
	TargetVersion     string
	ProfileID         string
	ProfileDigest     string
	ProfilePayload    json.RawMessage
	CampaignID        string
	ClassificationSHA string
}

// StagedReleaseCatalog 是不可变候选目录及其契约镜像。
type StagedReleaseCatalog struct {
	catalog               ReleaseCatalog
	targetKey             profilecontract.SnapshotKey
	targetSnapshot        []byte
	profileID             string
	campaignID            string
	classificationSHA     string
	activeVersion         string
	activeProfileDigest   string
	activeReleaseDigest   string
	previousReleaseDigest string
}

// BuildStagedReleaseCatalog 校验批准画像并生成 Active 不变、Previous=目标版本的
// 完整内容寻址目录。它只构造内存资产，不写仓库、不切换生产 selector。
func BuildStagedReleaseCatalog(
	base ReleaseCatalog,
	input CatalogStageInput,
) (StagedReleaseCatalog, error) {
	if !catalogStageVersionPattern.MatchString(input.TargetVersion) ||
		!catalogStageSafeID(input.ProfileID) ||
		!receiptSHA256(input.ProfileDigest) ||
		!catalogStageSafeID(input.CampaignID) ||
		!receiptSHA256(input.ClassificationSHA) ||
		len(input.ProfilePayload) == 0 {
		return StagedReleaseCatalog{}, errors.New("候选 Catalog 导入坐标不完整")
	}
	active, err := base.Resolve(ReleaseModeActive)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	if active.Version() == input.TargetVersion {
		return StagedReleaseCatalog{}, errors.New("目标版本已经是 Active，禁止作为候选重复导入")
	}

	var snapshot profilecontract.SnapshotDoc
	decoder := json.NewDecoder(bytes.NewReader(input.ProfilePayload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&snapshot); err != nil {
		return StagedReleaseCatalog{}, fmt.Errorf("解析批准画像 profile_payload: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return StagedReleaseCatalog{}, errors.New("批准画像 profile_payload 含多余 JSON")
	}
	if snapshot.Version != input.TargetVersion || snapshot.Digest != input.ProfileDigest {
		return StagedReleaseCatalog{}, errors.New("批准画像 version/digest 与导入坐标不一致")
	}
	computedDigest, err := profilecontract.OfficialSnapshotDigest(snapshot)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	if computedDigest != input.ProfileDigest {
		return StagedReleaseCatalog{}, fmt.Errorf(
			"批准画像官方摘要不一致：manifest=%s computed=%s",
			input.ProfileDigest,
			computedDigest,
		)
	}
	targetRaw, err := json.Marshal(snapshot)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	targetRaw = append(targetRaw, '\n')
	targetBlobSum := sha256.Sum256(targetRaw)
	targetBlobSHA := hex.EncodeToString(targetBlobSum[:])
	targetKey := profilecontract.SnapshotKey{
		Version: input.TargetVersion,
		Digest:  input.ProfileDigest,
	}
	targetFile := path.Join("profiles", targetKey.Version, targetKey.Digest+".json")

	baseFiles, err := base.RuntimeCatalogFiles()
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	profileBytes := make(map[string][]byte)
	for _, file := range baseFiles {
		if strings.HasPrefix(file.Path, "profiles/") {
			profileBytes[file.Path] = append([]byte(nil), file.Data...)
		}
	}
	if existing, ok := profileBytes[targetFile]; ok && !strings.EqualFold(
		runtimeCatalogSHA256(existing), targetBlobSHA,
	) {
		return StagedReleaseCatalog{}, errors.New("目标内容寻址画像路径已存在但字节不同")
	}
	profileBytes[targetFile] = append([]byte(nil), targetRaw...)

	snapshotDoc := base.snapshots.ToDoc()
	foundTarget := false
	for _, entry := range snapshotDoc.Snapshots {
		if entry.Version == targetKey.Version && entry.Digest == targetKey.Digest {
			if entry.File != targetFile || entry.BlobSHA256 != targetBlobSHA {
				return StagedReleaseCatalog{}, errors.New("目标画像坐标已存在但内容寻址事实不一致")
			}
			foundTarget = true
		}
	}
	if !foundTarget {
		snapshotDoc.Snapshots = append(snapshotDoc.Snapshots, profilecontract.SnapshotCatalogEntry{
			Version: targetKey.Version, Digest: targetKey.Digest,
			BlobSHA256: targetBlobSHA, File: targetFile,
		})
	}
	sort.Slice(snapshotDoc.Snapshots, func(i, j int) bool {
		if snapshotDoc.Snapshots[i].Version != snapshotDoc.Snapshots[j].Version {
			return snapshotDoc.Snapshots[i].Version < snapshotDoc.Snapshots[j].Version
		}
		return snapshotDoc.Snapshots[i].Digest < snapshotDoc.Snapshots[j].Digest
	})
	snapshots, err := profilecontract.NewSnapshotCatalog(
		snapshotDoc,
		func(relativePath string) ([]byte, error) {
			raw, ok := profileBytes[relativePath]
			if !ok {
				return nil, fmt.Errorf("候选 SnapshotCatalog 缺少画像：%s", relativePath)
			}
			return append([]byte(nil), raw...), nil
		},
	)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}

	build, transportByPurpose, err := stagedReleaseIdentity(snapshot, input)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	graphDoc := base.graph.ToDoc()
	for index := range graphDoc.Nodes {
		node := &graphDoc.Nodes[index]
		if node.Mode != releasecontract.ReleaseModePrevious {
			continue
		}
		activeNode, ok := base.graph.Resolve(node.Purpose, releasecontract.ReleaseModeActive)
		if !ok {
			return StagedReleaseCatalog{}, fmt.Errorf("Active 缺少 purpose：%s", node.Purpose)
		}
		node.Build = build
		node.Wire = activeNode.Wire
		node.Wire.ID = input.ProfileID + "-candidate-" + node.Purpose
		node.Wire.BuildID = build.ID
		node.Wire.TransportProfileID = transportByPurpose[node.Purpose]
		node.Wire.Source = build.Source
		node.Wire.Digest = ""
		digest, digestErr := releasecontract.RegistryProfileDigest(node.Build, node.Wire)
		if digestErr != nil {
			return StagedReleaseCatalog{}, digestErr
		}
		node.Wire.Digest = digest
		node.Snapshot = releasecontract.SnapshotReferenceDoc{
			Version: targetKey.Version,
			Digest:  targetKey.Digest,
		}
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	source := "campaign:" + input.CampaignID + "/classification:" + input.ClassificationSHA
	catalog, err := newReleaseCatalog(graph, snapshots, "staged-candidate", source)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	previous, err := catalog.Resolve(ReleaseModePrevious)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	stagedActive, err := catalog.Resolve(ReleaseModeActive)
	if err != nil {
		return StagedReleaseCatalog{}, err
	}
	if stagedActive.Version() != active.Version() ||
		stagedActive.ProfileDigest() != active.ProfileDigest() ||
		stagedActive.ReleaseDigest() != active.ReleaseDigest() {
		return StagedReleaseCatalog{}, errors.New("候选导入意外修改了当前 Active")
	}
	if previous.Version() != input.TargetVersion ||
		previous.ProfileDigest() != input.ProfileDigest ||
		previous.ReleaseDigest() == stagedActive.ReleaseDigest() {
		return StagedReleaseCatalog{}, errors.New("候选 Previous 发布坐标未正确建立")
	}
	return StagedReleaseCatalog{
		catalog: catalog, targetKey: targetKey, targetSnapshot: targetRaw,
		profileID: input.ProfileID, campaignID: input.CampaignID,
		classificationSHA: input.ClassificationSHA,
		activeVersion:     active.Version(), activeProfileDigest: active.ProfileDigest(),
		activeReleaseDigest: active.ReleaseDigest(), previousReleaseDigest: previous.ReleaseDigest(),
	}, nil
}

func catalogStageSafeID(value string) bool {
	if len(value) == 0 || len(value) > 128 {
		return false
	}
	for index, character := range value {
		if (character >= 'a' && character <= 'z') ||
			(character >= 'A' && character <= 'Z') ||
			(character >= '0' && character <= '9') ||
			(index > 0 && (character == '.' || character == '_' || character == '-')) {
			continue
		}
		return false
	}
	return true
}

func stagedReleaseIdentity(
	snapshot profilecontract.SnapshotDoc,
	input CatalogStageInput,
) (releasecontract.ReleaseBuildDoc, map[string]string, error) {
	var surfaces []struct {
		ID                   string `json:"ID"`
		Product              string `json:"Product"`
		Version              string `json:"Version"`
		PlatformPrefix       string `json:"PlatformPrefix"`
		DefaultTerminalToken string `json:"DefaultTerminalToken"`
		SuffixName           string `json:"SuffixName"`
		SuffixVersion        string `json:"SuffixVersion"`
		Originator           string `json:"Originator"`
	}
	if err := json.Unmarshal(snapshot.Surfaces, &surfaces); err != nil {
		return releasecontract.ReleaseBuildDoc{}, nil, fmt.Errorf("解析批准画像 Surfaces: %w", err)
	}
	var selected *struct {
		ID                   string `json:"ID"`
		Product              string `json:"Product"`
		Version              string `json:"Version"`
		PlatformPrefix       string `json:"PlatformPrefix"`
		DefaultTerminalToken string `json:"DefaultTerminalToken"`
		SuffixName           string `json:"SuffixName"`
		SuffixVersion        string `json:"SuffixVersion"`
		Originator           string `json:"Originator"`
	}
	for index := range surfaces {
		if surfaces[index].ID == "exec" {
			selected = &surfaces[index]
			break
		}
	}
	if selected == nil || selected.Product == "" || selected.Version != input.TargetVersion ||
		selected.PlatformPrefix == "" || selected.DefaultTerminalToken == "" ||
		selected.SuffixName == "" || selected.SuffixVersion != input.TargetVersion ||
		selected.Originator == "" {
		return releasecontract.ReleaseBuildDoc{}, nil, errors.New("批准画像缺少完整 exec Surface 身份")
	}
	userAgent := fmt.Sprintf(
		"%s/%s %s %s (%s; %s)",
		selected.Product, selected.Version, selected.PlatformPrefix,
		selected.DefaultTerminalToken, selected.SuffixName, selected.SuffixVersion,
	)
	source := "campaign:" + input.CampaignID + "/classification:" + input.ClassificationSHA
	build := releasecontract.ReleaseBuildDoc{
		ID:       input.ProfileID + "-" + input.ProfileDigest[:12],
		Provider: "openai", Product: "codex", Surface: "cli",
		Version: input.TargetVersion, UserAgent: userAgent,
		Originator: selected.Originator, RuntimeHeaders: []releasecontract.HeaderValueDoc{},
		Source: source,
	}
	endpointTransport := make(map[string]string)
	for _, endpoint := range snapshot.Endpoints {
		endpointTransport[endpoint.ID] = endpoint.TransportID
	}
	transportByPurpose := map[string]string{
		RegistryPurposeOpenAIOAuthHTTP: endpointTransport["responses_http"],
		RegistryPurposeOpenAIOAuthWS:   endpointTransport["responses_ws"],
	}
	for purpose, transportID := range transportByPurpose {
		if transportID == "" {
			return releasecontract.ReleaseBuildDoc{}, nil,
				fmt.Errorf("批准画像缺少 %s 对应 transport", purpose)
		}
	}
	return build, transportByPurpose, nil
}

func (s StagedReleaseCatalog) RuntimeCatalogFiles() ([]RuntimeCatalogFile, error) {
	return s.catalog.RuntimeCatalogFiles()
}

func (s StagedReleaseCatalog) ContractReleaseGraphJSON() ([]byte, error) {
	return s.catalog.GraphJSON()
}

func (s StagedReleaseCatalog) ContractSnapshotCatalogJSON() ([]byte, error) {
	doc := s.catalog.snapshots.ToDoc()
	for index := range doc.Snapshots {
		entry := &doc.Snapshots[index]
		entry.BlobSHA256 = ""
		entry.File = path.Join("snapshots", entry.Version, entry.Digest+".json")
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		return nil, err
	}
	return append(raw, '\n'), nil
}

func (s StagedReleaseCatalog) TargetSnapshot() (string, []byte) {
	return path.Join("snapshots", s.targetKey.Version, s.targetKey.Digest+".json"),
		append([]byte(nil), s.targetSnapshot...)
}

// CatalogStageReceiptCore 返回由输出工具加入逐文件 inventory 前的固定收据字段。
func (s StagedReleaseCatalog) CatalogStageReceiptCore() map[string]any {
	return map[string]any{
		"schema_version":              "official-egress-catalog-stage/v1",
		"campaign_id":                 s.campaignID,
		"classification_sha256":       s.classificationSHA,
		"profile_id":                  s.profileID,
		"target_version":              s.targetKey.Version,
		"target_profile_digest":       s.targetKey.Digest,
		"candidate_release_mode":      string(ReleaseModePrevious),
		"active_unchanged":            true,
		"active_version":              s.activeVersion,
		"active_profile_digest":       s.activeProfileDigest,
		"active_release_digest":       s.activeReleaseDigest,
		"candidate_release_digest":    s.previousReleaseDigest,
		"production_selector_changed": false,
	}
}
