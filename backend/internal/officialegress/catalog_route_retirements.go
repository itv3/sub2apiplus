package officialegress

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

// release-route-retirements.json 是“发布退役 route”的追加式覆盖层。
//
// 目标版本删除某个端点之后，只要 Active／Previous 里还有画像声明它，相关 route 照常发布；最后一个声明它的画像离开
// Active／Previous 时（legacy compact 在最后一份声明它的画像离开之后即此情形），bootstrap binding、Catalog 补充与历史
// MigrationReceipt 里的这条 route 就再也连接不到任何 ProfileSpec endpoint。这些历史事实不可改写——binding digest
// 与收据都绑定完整 route 集合——所以 route 原样留在静态 binding 里，只由本清单显式登记为发布退役：运行时不生成
// EndpointBinding、ReleaseSelection 与路由条目，请求在执行器解析端点时失败关闭。
//
// 只有同时满足下列条件才按退役处理，其余“缺端点”一律照旧失败关闭：
//  1. 清单逐条登记物理 route、端点 ID、最后声明它的画像（version 与官方摘要）和审核信息，按物理 route 严格排序；
//  2. 最后画像的字节仍在 catalogdata/runtime/profiles 下（退休画像按冻结合同原地保留），解析后官方摘要一致，且恰有
//     一个端点与该物理 route 匹配、ID 等于登记的端点 ID——证明该 route 确曾由画像声明；
//  3. 当前 Active、Previous 画像都没有与它匹配的端点；任一画像仍声明它时退役不生效（回滚到仍声明它的发布即照常发布）；
//  4. 生效的退役 route 必须被某个 Codex binding 实际引用，悬空登记失败关闭（见 validateReleaseRouteRetirementsUsed）。
//
//go:embed catalogdata/release-route-retirements.json
var embeddedReleaseRouteRetirements []byte

const releaseRouteRetirementSchemaVersion = 1

type releaseRouteRetirementManifest struct {
	SchemaVersion   int                         `json:"schema_version"`
	BootstrapCommit string                      `json:"bootstrap_commit"`
	Routes          []releaseRouteRetirementDoc `json:"routes"`
}

type releaseRouteRetirementDoc struct {
	Method      string                        `json:"method"`
	Host        string                        `json:"host"`
	Path        string                        `json:"path"`
	Protocol    string                        `json:"protocol"`
	EndpointID  string                        `json:"endpoint_id"`
	LastProfile releaseRouteRetirementProfile `json:"last_profile"`
	ReviewedBy  string                        `json:"reviewed_by"`
	ReviewRef   string                        `json:"review_ref"`
	Rationale   string                        `json:"rationale"`
}

type releaseRouteRetirementProfile struct {
	Version string `json:"version"`
	Digest  string `json:"digest"`
}

// releaseRouteRetirement 是一条通过历史证明的登记项；是否生效取决于当前 Active／Previous。
type releaseRouteRetirement struct {
	physical   PhysicalRouteKey
	endpointID string
}

// releaseRouteRetirements 只含对给定发布目录生效的退役 route，按物理 route 身份索引。
type releaseRouteRetirements struct {
	active map[string]releaseRouteRetirement
}

// retires 判断 route（不论用途）对应的物理 route 是否已发布退役。
func (r releaseRouteRetirements) retires(route CatalogRoute) bool {
	_, ok := r.active[physicalRouteFromCatalogRoute(route).identity()]
	return ok
}

// retiredEndpointID 返回已退役 route 登记的端点 ID；未退役时返回空串与 false。
func (r releaseRouteRetirements) retiredEndpointID(route CatalogRoute) (string, bool) {
	retirement, ok := r.active[physicalRouteFromCatalogRoute(route).identity()]
	return retirement.endpointID, ok
}

var loadEmbeddedReleaseRouteRetirementDocs = sync.OnceValues(func() ([]releaseRouteRetirement, error) {
	return parseReleaseRouteRetirements(embeddedReleaseRouteRetirements, releaseCatalogFS)
})

// defaultReleaseRouteRetirements 返回对内嵌默认发布目录生效的退役 route。
func defaultReleaseRouteRetirements() (releaseRouteRetirements, error) {
	return releaseRouteRetirementsFor(DefaultReleaseCatalog())
}

// releaseRouteRetirementsFor 返回对指定发布目录（Active 与 Previous）生效的退役 route。
func releaseRouteRetirementsFor(catalog ReleaseCatalog) (releaseRouteRetirements, error) {
	declared, err := loadEmbeddedReleaseRouteRetirementDocs()
	if err != nil {
		return releaseRouteRetirements{}, err
	}
	profiles := make([]profilecontract.ExecutableProfile, 0, 2)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, resolveErr := catalog.Resolve(mode)
		if resolveErr != nil {
			return releaseRouteRetirements{}, resolveErr
		}
		profiles = append(profiles, release.ExecutableProfile())
	}
	return activeReleaseRouteRetirements(declared, profiles), nil
}

// activeReleaseRouteRetirements 过滤出所有给定画像都不声明其端点的登记项。
func activeReleaseRouteRetirements(
	declared []releaseRouteRetirement,
	profiles []profilecontract.ExecutableProfile,
) releaseRouteRetirements {
	active := make(map[string]releaseRouteRetirement, len(declared))
	for _, retirement := range declared {
		stillDeclared := false
		for _, profile := range profiles {
			if profileDeclaresPhysicalRoute(profile, retirement.physical) {
				stillDeclared = true
				break
			}
		}
		if !stillDeclared {
			active[retirement.physical.identity()] = retirement
		}
	}
	return releaseRouteRetirements{active: active}
}

// profileDeclaresPhysicalRoute 与 uniqueProfileEndpointForPhysical 用同一匹配口径，但只要有一个端点匹配即视为仍声明
// （多匹配属于画像自身的歧义，交由端点绑定照旧失败关闭，不能借退役绕过）。
func profileDeclaresPhysicalRoute(profile profilecontract.ExecutableProfile, physical PhysicalRouteKey) bool {
	for _, endpoint := range profile.Endpoints() {
		protocol := WireProtocolHTTP
		if strings.EqualFold(strings.TrimSpace(endpoint.Upgrade), "websocket") {
			protocol = WireProtocolWebSocket
		}
		endpointPath := endpoint.Path
		if !strings.HasPrefix(endpointPath, "/") {
			endpointPath = "/" + endpointPath
		}
		if endpoint.Method == physical.Method && protocol == physical.Protocol &&
			normalizeRouteHost(endpoint.Host) == normalizeRouteHost(physical.Host) && endpointPath == physical.Path {
			return true
		}
	}
	return false
}

func parseReleaseRouteRetirements(raw []byte, profiles fs.FS) ([]releaseRouteRetirement, error) {
	var manifest releaseRouteRetirementManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return nil, fmt.Errorf("发布退役 route 清单解析失败: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("发布退役 route 清单尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != releaseRouteRetirementSchemaVersion || manifest.BootstrapCommit != BootstrapCommit {
		return nil, errors.New("发布退役 route 清单 schema/bootstrap 非法")
	}
	retirements := make([]releaseRouteRetirement, 0, len(manifest.Routes))
	previous := ""
	for index, doc := range manifest.Routes {
		retirement, err := validateReleaseRouteRetirementDoc(doc, profiles)
		if err != nil {
			return nil, fmt.Errorf("发布退役 route 第 %d 项: %w", index+1, err)
		}
		identity := retirement.physical.identity()
		if previous >= identity {
			return nil, errors.New("发布退役 route 清单必须按物理 route 严格排序且不重复")
		}
		previous = identity
		retirements = append(retirements, retirement)
	}
	return retirements, nil
}

func validateReleaseRouteRetirementDoc(doc releaseRouteRetirementDoc, profiles fs.FS) (releaseRouteRetirement, error) {
	if strings.TrimSpace(doc.EndpointID) == "" || strings.TrimSpace(doc.ReviewedBy) == "" ||
		strings.TrimSpace(doc.ReviewRef) == "" || strings.TrimSpace(doc.Rationale) == "" ||
		strings.TrimSpace(doc.LastProfile.Version) == "" || !receiptcontract.ValidSHA256(doc.LastProfile.Digest) {
		return releaseRouteRetirement{}, errors.New("字段不完整或最后画像摘要非法")
	}
	physical := PhysicalRouteKey{
		Method: doc.Method, Host: doc.Host, Path: doc.Path, Protocol: WireProtocol(doc.Protocol),
	}
	if err := physical.Validate(); err != nil || doc.Host != normalizeRouteHost(doc.Host) ||
		strings.TrimSpace(doc.Path) != doc.Path {
		return releaseRouteRetirement{}, errors.New("物理 route 非法或未规范化")
	}
	executable, err := loadReleaseRouteRetirementProfile(profiles, doc.LastProfile)
	if err != nil {
		return releaseRouteRetirement{}, err
	}
	endpoint, ok := uniqueProfileEndpointForPhysical(executable, physical)
	if !ok || endpoint.ID != doc.EndpointID {
		return releaseRouteRetirement{}, fmt.Errorf(
			"最后画像 %s/%s 没有唯一端点 %s 对应该 route", doc.LastProfile.Version, doc.LastProfile.Digest, doc.EndpointID,
		)
	}
	return releaseRouteRetirement{physical: physical, endpointID: doc.EndpointID}, nil
}

// loadReleaseRouteRetirementProfile 按 version 与官方摘要从运行画像目录读取最后声明该 route 的画像，复核官方摘要后编译。
func loadReleaseRouteRetirementProfile(
	profiles fs.FS,
	last releaseRouteRetirementProfile,
) (profilecontract.ExecutableProfile, error) {
	if strings.ContainsAny(last.Version, `/\`) || strings.Contains(last.Version, "..") {
		return profilecontract.ExecutableProfile{}, errors.New("最后画像版本非法")
	}
	profilePath := path.Join("catalogdata/runtime/profiles", last.Version, last.Digest+".json")
	raw, err := fs.ReadFile(profiles, profilePath)
	if err != nil {
		return profilecontract.ExecutableProfile{}, fmt.Errorf("读取最后画像 %s: %w", profilePath, err)
	}
	snapshot, err := profilecontract.ParseSnapshot(raw)
	if err != nil {
		return profilecontract.ExecutableProfile{}, fmt.Errorf("解析最后画像: %w", err)
	}
	computed, err := profilecontract.OfficialSnapshotDigest(snapshot)
	if err != nil {
		return profilecontract.ExecutableProfile{}, fmt.Errorf("计算最后画像摘要: %w", err)
	}
	if computed != last.Digest || snapshot.Digest != last.Digest {
		return profilecontract.ExecutableProfile{}, errors.New("最后画像内容摘要不一致")
	}
	profile, err := profilecontract.NewProfileSpec(snapshot)
	if err != nil {
		return profilecontract.ExecutableProfile{}, fmt.Errorf("构造最后画像: %w", err)
	}
	if profile.Version() != last.Version {
		return profilecontract.ExecutableProfile{}, errors.New("最后画像版本与登记不一致")
	}
	executable, err := profilecontract.CompileExecutableProfile(profile)
	if err != nil {
		return profilecontract.ExecutableProfile{}, fmt.Errorf("编译最后画像: %w", err)
	}
	return executable, nil
}

// validateReleaseRouteRetirementsUsed 要求每条生效的退役 route 至少被一个 Codex（codex_profile 证据）binding 引用。
func validateReleaseRouteRetirementsUsed(retirements releaseRouteRetirements, inputs []SinkBindingInput) error {
	used := make(map[string]bool, len(retirements.active))
	for _, input := range inputs {
		if input.Persona != PersonaCodexCLI || input.EndpointEvidence != EndpointEvidenceCodexProfile {
			continue
		}
		for _, route := range input.Routes {
			identity := physicalRouteFromCatalogRoute(route).identity()
			if _, ok := retirements.active[identity]; ok {
				used[identity] = true
			}
		}
	}
	for identity, retirement := range retirements.active {
		if !used[identity] {
			return fmt.Errorf(
				"发布退役 route 没有被任何 Codex binding 引用: %s %s %s",
				retirement.physical.Method, retirement.physical.Host, retirement.physical.Path,
			)
		}
	}
	return nil
}
