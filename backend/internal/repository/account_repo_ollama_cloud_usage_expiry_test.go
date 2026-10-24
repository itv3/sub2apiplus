package repository

import (
	"context"
	"regexp"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

// 保存新会话并带到期时间：到期时间以 RFC3339 UTC 字符串写入受管键，且 SQL 先清掉旧到期时间。
func TestSaveOllamaCloudUsageSessionWithExpiryWritesManagedKey(t *testing.T) {
	client, mock := newOllamaCloudUsageRepositoryTestClient(t)
	account := ollamaCloudUsageRepositoryAccount()
	expiry := time.Date(2026, time.December, 1, 8, 30, 15, 0, time.FixedZone("UTC+8", 8*3600))

	mock.ExpectBegin()
	expectOllamaCloudUsageGroupLock(mock, account, true, `"cipher:wos-session=secret"`, `true`, `null`)
	mock.ExpectExec(`(?s)UPDATE accounts.*- 'ollama_cloud_usage_session_expires_at'\) \|\| \$1::jsonb`).
		WithArgs(`{"ollama_cloud_usage_auto_refresh":true,"ollama_cloud_usage_session":"cipher:wos-session=new","ollama_cloud_usage_session_expires_at":"2026-12-01T00:30:15Z"}`, "key", sqlmock.AnyArg()).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	repo := newAccountRepositoryWithSQL(client, nil, nil)

	require.NoError(t, repo.SaveOllamaCloudUsageSession(context.Background(), account, "cipher:wos-session=new", true, &expiry))
	require.NoError(t, mock.ExpectationsWereMet())
}

// 只更新到期时间：其余受管状态原样带回，到期时间进入比对（CAS）列。
func TestSetOllamaCloudUsageSessionExpiryKeepsOtherManagedState(t *testing.T) {
	client, mock := newOllamaCloudUsageRepositoryTestClient(t)
	account := ollamaCloudUsageRepositoryAccount()
	account.Extra[service.OllamaCloudUsageSessionExpiresAtExtraKey] = "2026-12-01T00:00:00Z"
	next := time.Date(2027, time.January, 5, 0, 0, 0, 0, time.UTC)

	mock.ExpectBegin()
	mock.ExpectQuery(`(?s)`+regexp.QuoteMeta("SELECT")+`.*ollama_cloud_usage_session_expires_at.*`+regexp.QuoteMeta("FOR NO KEY UPDATE")).
		WithArgs("key", account.ID, account.Platform, account.Type, sqlmock.AnyArg(), nil).
		WillReturnRows(sqlmock.NewRows([]string{"id", "anchor_matches", "session", "auto_refresh", "snapshot", "expires"}).
			AddRow(account.ID, true, `"cipher:wos-session=secret"`, `true`, `null`, `"2026-12-01T00:00:00Z"`))
	mock.ExpectExec(`(?s)UPDATE accounts`).
		WithArgs(`{"ollama_cloud_usage_auto_refresh":true,"ollama_cloud_usage_session":"cipher:wos-session=secret","ollama_cloud_usage_session_expires_at":"2027-01-05T00:00:00Z"}`, "key", sqlmock.AnyArg()).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	repo := newAccountRepositoryWithSQL(client, nil, nil)

	require.NoError(t, repo.SetOllamaCloudUsageSessionExpiry(context.Background(), account, &next))
	require.NoError(t, mock.ExpectationsWereMet())
}

// 数据库里的到期时间与调用方读到的不一致（并发被改）：按身份冲突拒绝，不覆盖。
func TestSetOllamaCloudUsageSessionExpiryRejectsConcurrentChange(t *testing.T) {
	client, mock := newOllamaCloudUsageRepositoryTestClient(t)
	account := ollamaCloudUsageRepositoryAccount()
	account.Extra[service.OllamaCloudUsageSessionExpiresAtExtraKey] = "2026-12-01T00:00:00Z"

	mock.ExpectBegin()
	mock.ExpectQuery(`(?s)` + regexp.QuoteMeta("SELECT") + `.*` + regexp.QuoteMeta("FOR NO KEY UPDATE")).
		WillReturnRows(sqlmock.NewRows([]string{"id", "anchor_matches", "session", "auto_refresh", "snapshot", "expires"}).
			AddRow(account.ID, true, `"cipher:wos-session=secret"`, `true`, `null`, `"2026-12-09T00:00:00Z"`))
	mock.ExpectRollback()
	repo := newAccountRepositoryWithSQL(client, nil, nil)

	err := repo.SetOllamaCloudUsageSessionExpiry(context.Background(), account, nil)
	require.ErrorIs(t, err, service.ErrOllamaCloudUsageIdentityChanged)
	require.NoError(t, mock.ExpectationsWereMet())
}

// 没有会话时不能单独写到期时间。
func TestSetOllamaCloudUsageSessionExpiryRequiresSession(t *testing.T) {
	client, _ := newOllamaCloudUsageRepositoryTestClient(t)
	account := ollamaCloudUsageRepositoryAccount()
	delete(account.Extra, service.OllamaCloudUsageSessionExtraKey)
	repo := newAccountRepositoryWithSQL(client, nil, nil)

	err := repo.SetOllamaCloudUsageSessionExpiry(context.Background(), account, nil)
	require.ErrorIs(t, err, service.ErrOllamaCloudUsageSessionRequired)
}
