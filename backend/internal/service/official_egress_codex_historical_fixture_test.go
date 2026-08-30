package service

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

const (
	officialCodexHistoricalFixturePath = "../officialegress/profilecontract/testdata/snapshots/0.145.0/" +
		officialCodexHistoricalProfileDigest + ".json"
	officialCodexHistoricalFixtureBlobSHA256 = "36c6c0e4464e6182347210d05d17ea85f6121e98f70f3c36b6ffc2b4230a5c66"
)

// officialCodexHistoricalFixture 只承载冻结历史画像，不含 ReleaseGraph、selector、
// ReleaseDigest 或 Bundle，因此不能被误用为生产发布身份。
type officialCodexHistoricalFixture struct {
	profile    profilecontract.ProfileSpec
	executable profilecontract.ExecutableProfile
}

func (f officialCodexHistoricalFixture) Version() string {
	return f.profile.Version()
}

func (f officialCodexHistoricalFixture) ProfileDigest() string {
	return f.profile.OfficialDigest()
}

func (f officialCodexHistoricalFixture) Profile() profilecontract.ProfileSpec {
	return f.profile
}

func (f officialCodexHistoricalFixture) ExecutableProfileDigest() string {
	return f.executable.Digest()
}

var loadOfficialCodexHistoricalFixture = sync.OnceValues(func() (officialCodexHistoricalFixture, error) {
	raw, err := os.ReadFile(officialCodexHistoricalFixturePath)
	if err != nil {
		return officialCodexHistoricalFixture{}, fmt.Errorf("读取冻结 0.145 测试夹具: %w", err)
	}
	rawSum := sha256.Sum256(raw)
	if hex.EncodeToString(rawSum[:]) != officialCodexHistoricalFixtureBlobSHA256 {
		return officialCodexHistoricalFixture{}, errors.New("冻结 0.145 测试夹具文件摘要漂移")
	}
	snapshot, err := profilecontract.ParseSnapshot(raw)
	if err != nil {
		return officialCodexHistoricalFixture{}, fmt.Errorf("解析冻结 0.145 测试夹具: %w", err)
	}
	digest, err := profilecontract.OfficialSnapshotDigest(snapshot)
	if err != nil {
		return officialCodexHistoricalFixture{}, fmt.Errorf("复算冻结 0.145 画像摘要: %w", err)
	}
	if snapshot.Digest != officialCodexHistoricalProfileDigest ||
		digest != officialCodexHistoricalProfileDigest {
		return officialCodexHistoricalFixture{}, errors.New("冻结 0.145 测试夹具画像摘要漂移")
	}
	profile, err := profilecontract.NewProfileSpec(snapshot)
	if err != nil {
		return officialCodexHistoricalFixture{}, fmt.Errorf("构造冻结 0.145 画像: %w", err)
	}
	executable, err := profilecontract.CompileExecutableProfile(profile)
	if err != nil {
		return officialCodexHistoricalFixture{}, fmt.Errorf("编译冻结 0.145 可执行画像: %w", err)
	}
	if profile.Version() != officialCodexVersion0145 ||
		profile.OfficialDigest() != officialCodexHistoricalProfileDigest {
		return officialCodexHistoricalFixture{}, errors.New("冻结 0.145 测试夹具坐标漂移")
	}
	return officialCodexHistoricalFixture{profile: profile, executable: executable}, nil
})

func init() {
	loadOfficialCodexHistoricalExecutableProfile = func(
		version string,
		digest string,
	) (profilecontract.ExecutableProfile, error) {
		if version != officialCodexVersion0145 || digest != officialCodexHistoricalProfileDigest {
			return profilecontract.ExecutableProfile{}, fmt.Errorf(
				"未登记的冻结历史画像：version=%s digest=%s",
				version,
				digest,
			)
		}
		fixture, err := loadOfficialCodexHistoricalFixture()
		if err != nil {
			return profilecontract.ExecutableProfile{}, err
		}
		return fixture.executable, nil
	}
}

func mustOfficialCodexHistoricalFixture(t testing.TB) officialCodexHistoricalFixture {
	t.Helper()
	fixture, err := loadOfficialCodexHistoricalFixture()
	if err != nil {
		t.Fatal(err)
	}
	return fixture
}

func TestOfficialCodexHistoricalFixtureIsDetachedFromRuntimeCatalog(t *testing.T) {
	if _, err := officialegress.DefaultReleaseCatalog().ResolveSnapshotExact(
		officialCodexVersion0145,
		officialCodexHistoricalProfileDigest,
	); err == nil {
		t.Fatal("已退休 0.145 画像重新进入当前运行 Catalog")
	}
	fixture := mustOfficialCodexHistoricalFixture(t)
	if fixture.Version() != officialCodexVersion0145 ||
		fixture.ProfileDigest() != officialCodexHistoricalProfileDigest {
		t.Fatal("冻结 0.145 测试夹具坐标漂移")
	}
	if _, err := loadOfficialCodexHistoricalExecutableProfile(
		"0.145.1",
		officialCodexHistoricalProfileDigest,
	); err == nil {
		t.Fatal("历史测试注入边界接受了未登记版本")
	}
}
