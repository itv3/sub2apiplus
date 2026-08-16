package officialegress

import (
	"errors"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

// CatalogPromotionInput 冻结一次生产发布提升所依赖的 Campaign 与 acceptance 身份。
// 提升只交换已经验收的 Active/Previous，不创建画像，也不修改历史 Snapshot。
type CatalogPromotionInput struct {
	CampaignID            string
	AcceptanceSHA256      string
	TargetVersion         string
	TargetProfileDigest   string
	RollbackVersion       string
	RollbackProfileDigest string
}

// PromotedReleaseCatalog 是完成生产指针交换后的不可变目录。
type PromotedReleaseCatalog struct {
	catalog ReleaseCatalog
	input   CatalogPromotionInput

	oldActiveReleaseDigest   string
	oldPreviousReleaseDigest string
	activeReleaseDigest      string
	previousReleaseDigest    string
}

// BuildPromotedReleaseCatalog 把已验收的 Previous 提升为 Active，并把原 Active
// 固化为 Previous 回滚点。所有输入坐标必须与当前目录精确一致，任何漂移均失败关闭。
func BuildPromotedReleaseCatalog(
	base ReleaseCatalog,
	input CatalogPromotionInput,
) (PromotedReleaseCatalog, error) {
	if !catalogStageSafeID(input.CampaignID) || !receiptSHA256(input.AcceptanceSHA256) ||
		!catalogStageVersionPattern.MatchString(input.TargetVersion) ||
		!receiptSHA256(input.TargetProfileDigest) ||
		!catalogStageVersionPattern.MatchString(input.RollbackVersion) ||
		!receiptSHA256(input.RollbackProfileDigest) {
		return PromotedReleaseCatalog{}, errors.New("生产提升坐标不完整")
	}
	if input.TargetVersion == input.RollbackVersion &&
		input.TargetProfileDigest == input.RollbackProfileDigest {
		return PromotedReleaseCatalog{}, errors.New("生产提升目标与回滚坐标不能相同")
	}

	oldActive, err := base.Resolve(ReleaseModeActive)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	oldPrevious, err := base.Resolve(ReleaseModePrevious)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	if oldActive.Version() != input.RollbackVersion ||
		oldActive.ProfileDigest() != input.RollbackProfileDigest {
		return PromotedReleaseCatalog{}, fmt.Errorf(
			"当前 Active 不是批准的回滚坐标：version=%s profile_digest=%s",
			oldActive.Version(), oldActive.ProfileDigest(),
		)
	}
	if oldPrevious.Version() != input.TargetVersion ||
		oldPrevious.ProfileDigest() != input.TargetProfileDigest {
		return PromotedReleaseCatalog{}, fmt.Errorf(
			"当前 Previous 不是已验收目标坐标：version=%s profile_digest=%s",
			oldPrevious.Version(), oldPrevious.ProfileDigest(),
		)
	}

	graphDoc := base.graph.ToDoc()
	for index := range graphDoc.Nodes {
		switch graphDoc.Nodes[index].Mode {
		case releasecontract.ReleaseModeActive:
			graphDoc.Nodes[index].Mode = releasecontract.ReleaseModePrevious
		case releasecontract.ReleaseModePrevious:
			graphDoc.Nodes[index].Mode = releasecontract.ReleaseModeActive
		default:
			return PromotedReleaseCatalog{}, errors.New("生产提升遇到非法 ReleaseMode")
		}
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	source := "campaign:" + input.CampaignID + "/acceptance:" + input.AcceptanceSHA256
	promoted, err := newReleaseCatalog(graph, base.snapshots, "production-promotion", source)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	active, err := promoted.Resolve(ReleaseModeActive)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	previous, err := promoted.Resolve(ReleaseModePrevious)
	if err != nil {
		return PromotedReleaseCatalog{}, err
	}
	if active.Version() != input.TargetVersion ||
		active.ProfileDigest() != input.TargetProfileDigest ||
		previous.Version() != input.RollbackVersion ||
		previous.ProfileDigest() != input.RollbackProfileDigest {
		return PromotedReleaseCatalog{}, errors.New("生产提升后的 Active/Previous 坐标不符合批准顺序")
	}
	if active.ReleaseDigest() == previous.ReleaseDigest() {
		return PromotedReleaseCatalog{}, errors.New("生产提升错误折叠了 Active/Previous ReleaseDigest")
	}

	return PromotedReleaseCatalog{
		catalog:                  promoted,
		input:                    input,
		oldActiveReleaseDigest:   oldActive.ReleaseDigest(),
		oldPreviousReleaseDigest: oldPrevious.ReleaseDigest(),
		activeReleaseDigest:      active.ReleaseDigest(),
		previousReleaseDigest:    previous.ReleaseDigest(),
	}, nil
}

func (p PromotedReleaseCatalog) RuntimeCatalogFiles() ([]RuntimeCatalogFile, error) {
	return p.catalog.RuntimeCatalogFiles()
}

func (p PromotedReleaseCatalog) ContractReleaseGraphJSON() ([]byte, error) {
	return p.catalog.GraphJSON()
}

// CatalogPromotionReceiptCore 返回由输出工具补充逐文件 inventory 前的固定收据。
func (p PromotedReleaseCatalog) CatalogPromotionReceiptCore() map[string]any {
	return map[string]any{
		"schema_version":                   "official-egress-catalog-promotion/v1",
		"campaign_id":                      p.input.CampaignID,
		"acceptance_sha256":                p.input.AcceptanceSHA256,
		"target_version":                   p.input.TargetVersion,
		"target_profile_digest":            p.input.TargetProfileDigest,
		"rollback_version":                 p.input.RollbackVersion,
		"rollback_profile_digest":          p.input.RollbackProfileDigest,
		"old_active_release_digest":        p.oldActiveReleaseDigest,
		"old_previous_release_digest":      p.oldPreviousReleaseDigest,
		"promoted_active_release_digest":   p.activeReleaseDigest,
		"promoted_previous_release_digest": p.previousReleaseDigest,
		"production_selector_changed":      true,
		"active_mode":                      string(ReleaseModeActive),
		"previous_mode":                    string(ReleaseModePrevious),
	}
}
