package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"testing"
)

const (
	changeset50145AllowlistSHA256      = "6a8f74076c01e1af35aab6bca42c818b523edc45a8cd91751011954ff54cec09"
	changeset50145NameSetSHA256        = "10d4572ef7b862efe42f54a987dbc7670b1bca31ad4adebbe31a9f43453db22c"
	changeset50145NameReasonSetSHA256  = "3d999d59ed93daace9d9f99fa0435c2ca224b0960db2cb119eda394de745f3e9"
	changeset5SurfaceInventorySHA256   = "569212ecc10787a9524ba69844fa3fcc4f6a45295f46e2492427461ebe680d60"
	changeset5SurfacePathSetSHA256     = "976a2edfddd3f5d469c7d56b0b797b3f147f621b2c04c2cf08eb56702d360b3c"
	changeset5ExclusionPathSetSHA256   = "a79141f7dbf62f3eab3fee3036c16b7777a97d36551138b8fd9b775236b25560"
	changeset5ExclusionReasonSetSHA256 = "0e0b64d039d91b54890147cac21e63a1e8f5517dd7b754ceb11645594298b302"
)

type changeset5NamedReason struct {
	Name   string `json:"name"`
	Reason string `json:"reason"`
}

type changeset50145Allowlist struct {
	SchemaVersion      string                  `json:"schema_version"`
	Changeset          string                  `json:"changeset"`
	AllowedIdentifiers []changeset5NamedReason `json:"allowed_identifiers"`
}

type changeset5PathReason struct {
	Path     string `json:"path"`
	FileType string `json:"file_type,omitempty"`
	Reason   string `json:"reason,omitempty"`
}

type changeset5SurfaceInventory struct {
	SchemaVersion              string                 `json:"schema_version"`
	Changeset                  string                 `json:"changeset"`
	ClassificationUpstreamBase string                 `json:"classification_upstream_base"`
	ObservedRemoteHead         string                 `json:"observed_remote_head"`
	SurfaceCount               int                    `json:"surface_count"`
	Surfaces                   []changeset5PathReason `json:"surfaces"`
	Exclusions                 []changeset5PathReason `json:"exclusions"`
}

func TestChangeset50145AllowlistIsIndependentlyLocked(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/changeset5/0145-symbol-allowlist.json")
	if err != nil {
		t.Fatal(err)
	}
	if changeset5InventorySHA256(raw) != changeset50145AllowlistSHA256 {
		t.Fatal("0145 允许清单文件摘要漂移")
	}
	var payload changeset50145Allowlist
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	if err := changeset5Validate0145AllowlistLock(payload); err != nil {
		t.Fatal(err)
	}
}

func TestChangeset5SurfaceInventoryIsIndependentlyLocked(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/changeset5/egress-surface-inventory.json")
	if err != nil {
		t.Fatal(err)
	}
	if changeset5InventorySHA256(raw) != changeset5SurfaceInventorySHA256 {
		t.Fatal("52 面结构化清单文件摘要漂移")
	}
	var payload changeset5SurfaceInventory
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	scanned := changeset5DeclaredSurfacePaths(payload)
	if err := changeset5ValidateSurfaceInventoryLock(payload, scanned); err != nil {
		t.Fatal(err)
	}
}

func TestChangeset5SurfaceInventoryLockRejectsCoordinatedMutation(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/changeset5/egress-surface-inventory.json")
	if err != nil {
		t.Fatal(err)
	}
	var original changeset5SurfaceInventory
	if err := json.Unmarshal(raw, &original); err != nil {
		t.Fatal(err)
	}
	clone := func() changeset5SurfaceInventory {
		var result changeset5SurfaceInventory
		encoded, _ := json.Marshal(original)
		_ = json.Unmarshal(encoded, &result)
		return result
	}
	mutations := map[string]changeset5SurfaceInventory{}
	added := clone()
	added.Surfaces = append(added.Surfaces, changeset5PathReason{Path: "backend/internal/service/coordinated_new_surface.go", FileType: "regular"})
	added.SurfaceCount++
	mutations["清单和扫描器同时增加一项"] = added
	replaced := clone()
	replaced.Surfaces[0].Path = "backend/internal/service/coordinated_replacement.go"
	mutations["等量替换"] = replaced
	reasonChanged := clone()
	reasonChanged.Exclusions[0].Reason = "被同时修改的排除理由"
	mutations["修改排除理由"] = reasonChanged
	for name, payload := range mutations {
		t.Run(name, func(t *testing.T) {
			if err := changeset5ValidateSurfaceInventoryLock(payload, changeset5DeclaredSurfacePaths(payload)); err == nil {
				t.Fatal("协调修改清单与扫描结果后仍绕过独立锁")
			}
		})
	}
}

func changeset5Validate0145AllowlistLock(payload changeset50145Allowlist) error {
	if payload.SchemaVersion != "changeset5-0145-symbol-allowlist/v1" || payload.Changeset != "5" ||
		len(payload.AllowedIdentifiers) != 2 {
		return fmt.Errorf("0145 允许清单元数据或数量漂移")
	}
	names := make([]string, 0, len(payload.AllowedIdentifiers))
	entries := make([]string, 0, len(payload.AllowedIdentifiers))
	exact := map[string]string{
		"officialCodexVersion0145":                  "0.145.0 不可变画像的证据版本常量，值和名称均属于版本化快照",
		"openAIAPIKeyCodexMimicClientCodexExec0145": "API Key mimic 已持久化历史画像 ID 的内部绑定常量，不得机械改写外部稳定值",
	}
	for _, item := range payload.AllowedIdentifiers {
		if exact[item.Name] != item.Reason {
			return fmt.Errorf("0145 允许项名称或理由漂移：%s", item.Name)
		}
		names = append(names, item.Name)
		entries = append(entries, item.Name+"\x00"+item.Reason)
	}
	if changeset5StringSetSHA256(names) != changeset50145NameSetSHA256 ||
		changeset5StringSetSHA256(entries) != changeset50145NameReasonSetSHA256 {
		return fmt.Errorf("0145 允许项排序闭集指纹漂移")
	}
	return nil
}

func changeset5ValidateSurfaceInventoryLock(
	payload changeset5SurfaceInventory,
	scanned []string,
) error {
	if payload.SchemaVersion != "changeset5-egress-surface-inventory/v1" || payload.Changeset != "5" ||
		payload.ClassificationUpstreamBase != "26d894ef4f50645a4bf1030e378ac892f17d0223" ||
		payload.ObservedRemoteHead != "825ca7b1fc9335f904bc077f051de815fb61e47f" ||
		payload.SurfaceCount != 52 || len(payload.Surfaces) != 52 || len(payload.Exclusions) != 2 {
		return fmt.Errorf("52 面清单顶层事实漂移")
	}
	surfaces := make([]string, 0, len(payload.Surfaces))
	for _, item := range payload.Surfaces {
		if item.FileType != "regular" || item.Path == "" {
			return fmt.Errorf("52 面清单存在非法 surface")
		}
		surfaces = append(surfaces, item.Path)
	}
	exclusionPaths := make([]string, 0, len(payload.Exclusions))
	exclusionEntries := make([]string, 0, len(payload.Exclusions))
	for _, item := range payload.Exclusions {
		exclusionPaths = append(exclusionPaths, item.Path)
		exclusionEntries = append(exclusionEntries, item.Path+"\x00"+item.Reason)
	}
	if changeset5StringSetSHA256(surfaces) != changeset5SurfacePathSetSHA256 ||
		changeset5StringSetSHA256(exclusionPaths) != changeset5ExclusionPathSetSHA256 ||
		changeset5StringSetSHA256(exclusionEntries) != changeset5ExclusionReasonSetSHA256 {
		return fmt.Errorf("52 面路径或排除理由排序闭集指纹漂移")
	}
	declared := changeset5DeclaredSurfacePaths(payload)
	if changeset5StringSetSHA256(scanned) != changeset5StringSetSHA256(declared) {
		return fmt.Errorf("扫描结果与结构化清单不一致")
	}
	return nil
}

func changeset5DeclaredSurfacePaths(payload changeset5SurfaceInventory) []string {
	result := make([]string, 0, len(payload.Surfaces)+len(payload.Exclusions))
	for _, item := range payload.Surfaces {
		result = append(result, item.Path)
	}
	for _, item := range payload.Exclusions {
		result = append(result, item.Path)
	}
	return result
}

func changeset5StringSetSHA256(values []string) string {
	cloned := append([]string(nil), values...)
	sort.Strings(cloned)
	sum := sha256.Sum256([]byte(strings.Join(cloned, "\n") + "\n"))
	return hex.EncodeToString(sum[:])
}

func changeset5InventorySHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
