package migrations

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestMigration187RemovesDeprecatedOfficialEgressAccountSettings(t *testing.T) {
	content, err := FS.ReadFile("187_remove_official_egress_account_settings.sql")
	require.NoError(t, err)

	sql := string(content)
	require.Contains(t, sql, "- 'official_egress_enabled'")
	require.Contains(t, sql, "- 'official_egress_profile_version'")
	require.Contains(t, sql, "UPDATE accounts")
}
