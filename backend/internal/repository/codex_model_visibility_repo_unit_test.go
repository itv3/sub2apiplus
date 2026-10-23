package repository

// 本文件是展示配置原子存储的单元测试，使用内存 SQLite，可在本机直接运行。
// SQLite 不支持 FOR UPDATE，所以这里把 lockRows 置为 false，只验证解析、冲突判定与读写逻辑；
// 行锁下的并发行为见 codex_model_visibility_repo_integration_test.go（需要 PostgreSQL 容器）。

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"testing"
	"time"

	dbent "github.com/Wei-Shaw/sub2api/ent"
	"github.com/Wei-Shaw/sub2api/ent/enttest"
	"github.com/Wei-Shaw/sub2api/ent/setting"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"

	"entgo.io/ent/dialect"
	entsql "entgo.io/ent/dialect/sql"
	_ "modernc.org/sqlite"
)

func newCodexVisibilitySQLiteStore(t *testing.T) (*codexModelVisibilityStore, *dbent.Client) {
	t.Helper()
	db, err := sql.Open("sqlite", fmt.Sprintf("file:%s?mode=memory&cache=shared&_fk=1", t.Name()))
	require.NoError(t, err)
	t.Cleanup(func() { _ = db.Close() })
	db.SetMaxOpenConns(10)
	_, err = db.Exec("PRAGMA foreign_keys = ON")
	require.NoError(t, err)

	drv := entsql.OpenDB(dialect.SQLite, db)
	client := enttest.NewClient(t, enttest.WithOptions(dbent.Driver(drv)))
	t.Cleanup(func() { _ = client.Close() })
	return &codexModelVisibilityStore{client: client, lockRows: false}, client
}

func codexVisibilityConfig(models map[string]service.CodexModelVisibilityEntry) service.CodexModelVisibilityConfig {
	cfg := service.NewCodexModelVisibilityConfig()
	for slug, entry := range models {
		cfg.Models[slug] = entry
	}
	return cfg
}

func readCodexVisibilityRow(t *testing.T, client *dbent.Client, groupID int64) (string, bool) {
	t.Helper()
	row, err := client.Setting.Query().
		Where(setting.KeyEQ(service.CodexModelVisibilitySettingKey(groupID))).Only(context.Background())
	if dbent.IsNotFound(err) {
		return "", false
	}
	require.NoError(t, err)
	return row.Value, true
}

func TestCodexModelVisibilityStoreCreateThenUpdateBumpsRevision(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()

	first, err := store.CompareAndSwapCodexModelVisibility(ctx, 7, 0, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
		"kimi-k3": {Enabled: true},
	}))
	require.NoError(t, err)
	require.EqualValues(t, 1, first.Revision)
	require.Equal(t, service.CodexModelVisibilitySchemaVersion, first.SchemaVersion)
	require.True(t, first.IsEnabled("kimi-k3"))

	raw, found := readCodexVisibilityRow(t, client, 7)
	require.True(t, found)
	stored, err := service.ParseCodexModelVisibilityConfig(raw)
	require.NoError(t, err)
	require.EqualValues(t, 1, stored.Revision)

	second, err := store.CompareAndSwapCodexModelVisibility(ctx, 7, 1, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
		"kimi-k3":        {Enabled: false},
		"deepseek-flash": {Enabled: true},
	}))
	require.NoError(t, err)
	require.EqualValues(t, 2, second.Revision)
	require.False(t, second.IsEnabled("kimi-k3"))
	require.True(t, second.IsEnabled("deepseek-flash"))
}

func TestCodexModelVisibilityStoreIgnoresRevisionAndSchemaSuppliedByCaller(t *testing.T) {
	store, _ := newCodexVisibilitySQLiteStore(t)
	next := codexVisibilityConfig(nil)
	// 调用方在 next 里填的 revision 与结构版本都不可信，必须被覆盖。
	next.Revision = 99
	next.SchemaVersion = 0
	written, err := store.CompareAndSwapCodexModelVisibility(context.Background(), 8, 0, next)
	require.NoError(t, err)
	require.EqualValues(t, 1, written.Revision)
	require.Equal(t, service.CodexModelVisibilitySchemaVersion, written.SchemaVersion)
}

func TestCodexModelVisibilityStoreRevisionMismatchIsConflictAndKeepsRow(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	_, err := store.CompareAndSwapCodexModelVisibility(ctx, 9, 0, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
		"glm-5.3": {Enabled: true},
	}))
	require.NoError(t, err)
	before, _ := readCodexVisibilityRow(t, client, 9)

	for _, expected := range []int64{0, 2, 5} {
		_, err = store.CompareAndSwapCodexModelVisibility(ctx, 9, expected, codexVisibilityConfig(nil))
		require.ErrorIs(t, err, service.ErrCodexModelVisibilityConflict, "expected revision %d", expected)
	}
	after, _ := readCodexVisibilityRow(t, client, 9)
	require.Equal(t, before, after, "冲突时不得改动已有配置")
}

func TestCodexModelVisibilityStoreMissingRowWithNonZeroExpectationIsConflict(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	_, err := store.CompareAndSwapCodexModelVisibility(context.Background(), 10, 3, codexVisibilityConfig(nil))
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityConflict)
	_, found := readCodexVisibilityRow(t, client, 10)
	require.False(t, found, "冲突时不得顺手创建行")
}

func TestCodexModelVisibilityStoreCorruptRowIsNeverOverwritten(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	key := service.CodexModelVisibilitySettingKey(11)
	require.NoError(t, client.Setting.Create().SetKey(key).SetValue(`{"schema_version":1,"revision":`).SetUpdatedAt(time.Now()).Exec(ctx))

	// 无论期望 revision 是 0 还是别的值，损坏行都不能被覆盖，也不能被当成冲突掩盖。
	for _, expected := range []int64{0, 1} {
		_, err := store.CompareAndSwapCodexModelVisibility(ctx, 11, expected, codexVisibilityConfig(nil))
		require.Error(t, err)
		require.NotErrorIs(t, err, service.ErrCodexModelVisibilityConflict)
	}
	raw, _ := readCodexVisibilityRow(t, client, 11)
	require.Equal(t, `{"schema_version":1,"revision":`, raw)
}

func TestCodexModelVisibilityStoreUnknownSchemaVersionRowIsNeverOverwritten(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	key := service.CodexModelVisibilitySettingKey(12)
	future := `{"schema_version":99,"revision":4,"models":{}}`
	require.NoError(t, client.Setting.Create().SetKey(key).SetValue(future).SetUpdatedAt(time.Now()).Exec(ctx))

	_, err := store.CompareAndSwapCodexModelVisibility(ctx, 12, 4, codexVisibilityConfig(nil))
	require.Error(t, err)
	raw, _ := readCodexVisibilityRow(t, client, 12)
	require.Equal(t, future, raw)
}

func TestCodexModelVisibilityStoreRejectsInvalidNextBeforeTouchingRow(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	bad := codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{"kimi-*": {Enabled: true}})
	_, err := store.CompareAndSwapCodexModelVisibility(context.Background(), 13, 0, bad)
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityInvalid)
	_, found := readCodexVisibilityRow(t, client, 13)
	require.False(t, found)

	_, err = store.CompareAndSwapCodexModelVisibility(context.Background(), 0, 0, codexVisibilityConfig(nil))
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityInvalid)
}

func TestCodexModelVisibilityStoreConcurrentFirstCreateHitsUniqueKeyAsConflict(t *testing.T) {
	_, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	key := service.CodexModelVisibilitySettingKey(14)

	// 模拟并发首次创建：另一个节点已先一步写入同一个键，本次创建撞到唯一键约束。
	require.NoError(t, createCodexVisibilityRow(ctx, client, key, `{"schema_version":1,"revision":1,"models":{}}`))
	err := createCodexVisibilityRow(ctx, client, key, `{"schema_version":1,"revision":1,"models":{}}`)
	require.ErrorIs(t, err, service.ErrCodexModelVisibilityConflict)
}

func TestMapCodexVisibilityCreateErrorLeavesOtherErrorsAlone(t *testing.T) {
	require.NoError(t, mapCodexVisibilityCreateError(nil))
	other := errors.New("connection reset")
	require.Same(t, other, mapCodexVisibilityCreateError(other))
}

func TestCodexModelVisibilityStoreDeleteIsIdempotent(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, 15), "不存在时视为成功")

	_, err := store.CompareAndSwapCodexModelVisibility(ctx, 15, 0, codexVisibilityConfig(nil))
	require.NoError(t, err)
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, 15))
	_, found := readCodexVisibilityRow(t, client, 15)
	require.False(t, found)
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, 15))

	require.ErrorIs(t, store.DeleteCodexModelVisibility(ctx, -1), service.ErrCodexModelVisibilityInvalid)
}

func TestCodexModelVisibilityStoreDoesNotTouchOtherGroups(t *testing.T) {
	store, client := newCodexVisibilitySQLiteStore(t)
	ctx := context.Background()
	for _, id := range []int64{20, 21} {
		_, err := store.CompareAndSwapCodexModelVisibility(ctx, id, 0, codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{
			"kimi-k3": {Enabled: true},
		}))
		require.NoError(t, err)
	}
	require.NoError(t, store.DeleteCodexModelVisibility(ctx, 20))
	_, found := readCodexVisibilityRow(t, client, 21)
	require.True(t, found)
}

func TestPlanCodexModelVisibilityWrite(t *testing.T) {
	cfg := codexVisibilityConfig(map[string]service.CodexModelVisibilityEntry{"kimi-k3": {Enabled: true}})
	existing, err := codexVisibilityConfig(nil).Marshal()
	require.NoError(t, err)
	existing3 := func() string {
		c := codexVisibilityConfig(nil)
		c.Revision = 3
		raw, mErr := c.Marshal()
		require.NoError(t, mErr)
		return raw
	}()

	tests := []struct {
		name     string
		found    bool
		raw      string
		expected int64
		wantErr  error
		created  bool
		wantRev  int64
	}{
		{name: "行不存在且期望 0：创建", found: false, expected: 0, created: true, wantRev: 1},
		{name: "行不存在但期望非 0：冲突", found: false, expected: 2, wantErr: service.ErrCodexModelVisibilityConflict},
		{name: "行已存在但期望 0：冲突", found: true, raw: existing, expected: 0, wantErr: service.ErrCodexModelVisibilityConflict},
		{name: "revision 相等：更新", found: true, raw: existing3, expected: 3, wantRev: 4},
		{name: "revision 落后：冲突", found: true, raw: existing3, expected: 2, wantErr: service.ErrCodexModelVisibilityConflict},
		{name: "revision 超前：冲突", found: true, raw: existing3, expected: 4, wantErr: service.ErrCodexModelVisibilityConflict},
		{name: "期望为负：非法", found: false, expected: -1, wantErr: service.ErrCodexModelVisibilityInvalid},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			plan, planErr := planCodexModelVisibilityWrite(tc.found, tc.raw, tc.expected, cfg)
			if tc.wantErr != nil {
				require.ErrorIs(t, planErr, tc.wantErr)
				return
			}
			require.NoError(t, planErr)
			require.Equal(t, tc.created, plan.created)
			require.Equal(t, tc.wantRev, plan.written.Revision)
			parsed, parseErr := service.ParseCodexModelVisibilityConfig(plan.value)
			require.NoError(t, parseErr)
			require.Equal(t, tc.wantRev, parsed.Revision)
			require.True(t, parsed.IsEnabled("kimi-k3"))
		})
	}

	_, planErr := planCodexModelVisibilityWrite(true, "not json", 1, cfg)
	require.Error(t, planErr)
	require.NotErrorIs(t, planErr, service.ErrCodexModelVisibilityConflict)
}
