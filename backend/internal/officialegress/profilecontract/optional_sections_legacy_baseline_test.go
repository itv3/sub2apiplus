package profilecontract

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// 历史快照（testdata/snapshots 下新增可选节之前的全部夹具）在可选节扩展前后，
// 官方摘要、规范化后的快照字节、ProfileDigest、executable 摘要与可选节执行投影必须逐字节相同。
//
// legacySnapshotBaselines 里的值不是扩展之后现算的，而是在新增两个可选节之前，用当时的代码对每个夹具实测
// 得到的基线；测试用它们断言“节缺省即旧逻辑”，任何让旧画像摘要漂移的改动都会立刻变红。
// 表按官方摘要（即夹具文件名）索引；之后新增的、含新节的快照不在表里，也不受这个测试约束。
// 有两个较早版本的夹具按设计无法编译成可执行画像（端点生命周期与传输复用组合未获批准），
// 它们的期望是同一条编译错误，同样属于“旧行为不变”。

type legacySnapshotBaseline struct {
	// preparedDigest：PrepareSnapshotForManifest 规范化 RawMessage 后重算的官方摘要。
	preparedDigest string
	// preparedJSONSHA256：规范化后的 SnapshotDoc 序列化字节（含 Digest 字段）的 SHA-256。
	preparedJSONSHA256 string
	// profileDigest：ProfileSpec 的 ProfileDigest。
	profileDigest string
	// executableDigest：可执行画像摘要；编译失败的夹具为空。
	executableDigest string
	// compileError：按设计无法编译的夹具的期望错误文本；能编译的夹具为空。
	compileError string
	// optionalSHA256：可选节执行投影序列化字节的 SHA-256。
	optionalSHA256 string
}

var legacySnapshotBaselines = map[string]legacySnapshotBaseline{
	"31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3": {
		preparedDigest:     "31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3",
		preparedJSONSHA256: "b4001a941742199cf898645ed45fcde0f84a200ccaf9ab12cd337a3a56f98266",
		profileDigest:      "12b56aabac11e6134004f2bf1f1031232c9fc0927f397e4d3e5b60b5e2653957",
		executableDigest:   "7b8f9a34a0ce6d4f2543dc757de48d0be56da6ab6fffa607fb6c04cfcc13342e",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"33a537a32e6f059bb178b7af581de5c161a3091ed972b4a8aec215cbf977b563": {
		preparedDigest:     "33a537a32e6f059bb178b7af581de5c161a3091ed972b4a8aec215cbf977b563",
		preparedJSONSHA256: "bab3d321703b54921acae7e3bf20a154422186580683b7a8695f394fa7239b48",
		profileDigest:      "c27f1d60c04b51b2d46bd1c92dd3c3d51bd7ac1a8055942fa025a4c402aeab70",
		executableDigest:   "5ece5da499cba76a4d00966c48bb568b5d4a59c7e4a45e887ed7b9dc6e2c60ee",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"343991bad0f89614cd092778186f51eb23d5afbf4c98a198981639758bdf5431": {
		preparedDigest:     "7b1b3ed258491585a02affc78dca21a8183a2e4ffd91544a71c954bf0fac9f08",
		preparedJSONSHA256: "7b6eb02ac0b3682966be04e7cfa987d86ff841fbc892be651789762ab481f7d6",
		profileDigest:      "1ecd5970998d61816c6e0f8b62f913c6ceb5c7772aa8de07f4be2813a63871da",
		executableDigest:   "2b88dcc96d5dfd96224795d574b06fc59488818867506987394c89121972d999",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"3edd1c7bd487021469a932ff63599e581000402dd8633dfe02101a2ec4e3ae9d": {
		preparedDigest:     "3edd1c7bd487021469a932ff63599e581000402dd8633dfe02101a2ec4e3ae9d",
		preparedJSONSHA256: "1f82dddc69fae80f5564d2e604f2337fdfc74542933a16d62ee2f824d7f69c17",
		profileDigest:      "aeb742cad02418f3bd6ebcc13a71d0f8bcb485f72ce0faaedc9623cdf32c7acc",
		executableDigest:   "16980ae867f28c6327410aa517ffeba35fe206c0ebe8918dd7b915ada20c24ba",
		optionalSHA256:     "5c5af7323af421ce7fe3cbb90b056f76ad18dfec90cb3c18ee382894bda53d18",
	},
	"7d9c22ce150eb8395cb6ad66a5b77175c48840c1129710e32e484b50b9e63eb5": {
		preparedDigest:     "d9151225467e341c5d6721ca97e048e5419d4b4900239572417b39785ba71eb4",
		preparedJSONSHA256: "e77610d4f47a6a048ce6a92aec056a0b1cac78f54420e93155bf301271afe313",
		profileDigest:      "ad7af8e64a599ccc231d3f70e1251bd67365d44604da54dfd1a058990cc0fb63",
		compileError:       "endpoint wham_usage: backend_client_long_lived 必须允许跨调用连接复用",
	},
	"8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3": {
		preparedDigest:     "8c22d3b18b16d249ac041a97efad1b6703c11ef290622b0b1642679a3c010ec3",
		preparedJSONSHA256: "0b1fbc9c608118cc8d381d57348a3d988a65b51298562d2a079cf087fa97e6be",
		profileDigest:      "61d1db4b41ba97b9667678cb3cf219326d9ccb47c58c9ea2cae9e0e6160000d8",
		executableDigest:   "67731e4bf8152b374a6dc1efc636ca91f7cff658be92ea210d6c9d15fa249193",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60": {
		preparedDigest:     "8e59b38e2ad90a1fd4eb7520c2c54f01fc62f802690d45a2cdab5f91f249fb60",
		preparedJSONSHA256: "718e76d3e5739b57f3f29dad0745c8f66595bf1014d9a4bc37296d28cb5dd12c",
		profileDigest:      "1dc33ee4d72d8b39869a46663a106fbbf75170e8fda8345ffb241723c33fcf79",
		executableDigest:   "4fda2318d5bd056402d7dc2144fb1b7979779525e16dee3b0eae3894801f7ff8",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc": {
		preparedDigest:     "94071c8eb93cfd337ac6eabc291d878084e3dcec8a9e618e04e6f68792d1a7bc",
		preparedJSONSHA256: "d18cc0dedffbdbefb20dfd474c3220428c1c0389c4944a8dca5f0f86b4ffbfb3",
		profileDigest:      "09c5c40b2e74578ac98678f41fd4db5e91b57934c70c54c2503806ff3df7d158",
		executableDigest:   "2fb774d8601d4d1b2292314407c1d186f3c199c09e852318cd5fb81af2647872",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"9b7dd12df50dbcff74594b1f05440161cd99b963019a4f316f20c08ed5f5ba1e": {
		preparedDigest:     "dff9a497c5cfe22daaa8a3282cb5bbb8cc8281d4025d41b1ae9a03c7ba54c2f5",
		preparedJSONSHA256: "8ac0f98aa634d65ac844546d8258d2e302f8eceea48a293bcbdc007e655eb09b",
		profileDigest:      "f89c73f64b457cc8035c29f41b28fbf63822aace68e04a72d619e8839dffb95c",
		compileError:       "endpoint wham_usage: backend_client_long_lived 必须允许跨调用连接复用",
	},
	"d33a4f097f86d57929995760b7e51c4ddf672e5f2db5cb36c0d0815afc3ad12c": {
		preparedDigest:     "d33a4f097f86d57929995760b7e51c4ddf672e5f2db5cb36c0d0815afc3ad12c",
		preparedJSONSHA256: "08c232e825e5edd5602b23c23d3c9653388024444d3dca8a110d62cab64c4ecb",
		profileDigest:      "ead710a05228333910c952141b76d1cba9ae5c98849e73bc027e4ae30279394a",
		executableDigest:   "cb3c3cb71ae3e0e2fdcabc8e1415aadc59426e2d62efdf660bb0b0000fb8f9c4",
		optionalSHA256:     "beef233ac7d392a116139a55f380ff3521ffb0c19d338432fb3205fa677c99e8",
	},
	"dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39": {
		preparedDigest:     "dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39",
		preparedJSONSHA256: "d520b3b2ed94fca1e635f647d11197ba4013505442524fcf842c38611e5cceb2",
		profileDigest:      "5bc90f47a2f206ed27310aea688c291a5b2a25489d662d74ef32c8115b10c96b",
		executableDigest:   "60f090cbb467ef383d40cd72b43e9f27674cfacbb5b87fff3cbc7678550cf23a",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
	"e0b59772622f14717f1fdf5c15bfae5758226a04fe8f030110d8a616e20fdf6b": {
		preparedDigest:     "974cc42e4507f2f2cfbb8fcc23f293d9aef6b99c1e9bfaa9e96879100602297c",
		preparedJSONSHA256: "20be103c15b5a71589eec7fdac08da1e7d10ead485283bbd987081f8d84854ab",
		profileDigest:      "69999f7a170b318224d61cb7c223467ebbe1478564c618e904f414c2588156c1",
		executableDigest:   "dc5ac8042a8b46312047a16c3bd4e9685a16135d8cddef5a76ba073e49e4212c",
		optionalSHA256:     "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
	},
}

func sha256Hex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func TestLegacySnapshotsKeepEveryDigestAfterNewerSections(t *testing.T) {
	// 基线表必须覆盖已知的全部历史夹具，防止有人悄悄从表里删掉一个来“修绿”。
	if len(legacySnapshotBaselines) != 12 {
		t.Fatalf("历史快照基线表应覆盖 12 个夹具，实际 %d 个", len(legacySnapshotBaselines))
	}
	for name, want := range legacySnapshotBaselines {
		t.Run(name[:12], func(t *testing.T) {
			matches, err := filepath.Glob(filepath.Join("testdata", "snapshots", "*", name+".json"))
			if err != nil || len(matches) != 1 {
				t.Fatalf("历史夹具 %s 缺失或重复：%v %v", name, matches, err)
			}
			raw, err := os.ReadFile(matches[0])
			if err != nil {
				t.Fatal(err)
			}
			doc, err := ParseSnapshot(raw)
			if err != nil {
				t.Fatalf("历史夹具被严格解码拒绝：%v", err)
			}
			if doc.BaseInstructions != nil || doc.AccessPrograms != nil {
				t.Fatalf("历史夹具不应含新节：%s", matches[0])
			}
			official, err := OfficialSnapshotDigest(doc)
			if err != nil || official != name {
				t.Fatalf("官方摘要漂移：%s != %s (%v)", official, name, err)
			}
			prepared, err := PrepareSnapshotForManifest(doc)
			if err != nil {
				t.Fatalf("规范化失败：%v", err)
			}
			if prepared.Digest != want.preparedDigest {
				t.Fatalf("规范化后的官方摘要漂移：%s != %s", prepared.Digest, want.preparedDigest)
			}
			preparedJSON, err := json.Marshal(prepared)
			if err != nil {
				t.Fatal(err)
			}
			if got := sha256Hex(preparedJSON); got != want.preparedJSONSHA256 {
				t.Fatalf("规范化后的快照字节漂移：%s != %s", got, want.preparedJSONSHA256)
			}
			spec, err := NewProfileSpec(doc)
			if err != nil {
				t.Fatal(err)
			}
			profileDigest, err := spec.ProfileDigest()
			if err != nil {
				t.Fatal(err)
			}
			if profileDigest != want.profileDigest {
				t.Fatalf("ProfileDigest 漂移：%s != %s", profileDigest, want.profileDigest)
			}
			executable, err := CompileExecutableProfile(spec)
			if want.compileError != "" {
				if err == nil || err.Error() != want.compileError {
					t.Fatalf("按设计无法编译的夹具，编译错误应保持不变：期望 %q，实际 %v", want.compileError, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("历史夹具编译失败：%v", err)
			}
			if executable.Digest() != want.executableDigest {
				t.Fatalf("executable 摘要漂移：%s != %s", executable.Digest(), want.executableDigest)
			}
			optional := executable.Optional()
			if optional.BaseInstructions != nil || optional.AccessPrograms != nil {
				t.Fatalf("历史夹具不应有新节的执行投影：%#v", optional)
			}
			optionalJSON, err := json.Marshal(optional)
			if err != nil {
				t.Fatal(err)
			}
			if got := sha256Hex(optionalJSON); got != want.optionalSHA256 {
				t.Fatalf("可选节执行投影字节漂移：%s != %s", got, want.optionalSHA256)
			}
		})
	}
}
