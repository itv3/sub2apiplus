//go:build unit

package repository

import (
	"database/sql"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestOpsInsertErrorLogArgsPreservesExplicitZeroUpstreamStatus(t *testing.T) {
	zero := 0
	args := opsInsertErrorLogArgs(&service.OpsInsertErrorLogInput{UpstreamStatusCode: &zero})

	require.Len(t, args, 38)
	encoded, ok := args[27].(sql.NullInt64)
	require.True(t, ok)
	require.True(t, encoded.Valid)
	require.Zero(t, encoded.Int64)
}

func TestOpsNullableIntPointerDistinguishesNilZeroAndStatus(t *testing.T) {
	missing := requireType[sql.NullInt64](t, opsNullableIntPointer(nil))
	require.False(t, missing.Valid)

	zeroValue := 0
	zero := requireType[sql.NullInt64](t, opsNullableIntPointer(&zeroValue))
	require.True(t, zero.Valid)
	require.Zero(t, zero.Int64)

	statusValue := 503
	status := requireType[sql.NullInt64](t, opsNullableIntPointer(&statusValue))
	require.True(t, status.Valid)
	require.EqualValues(t, 503, status.Int64)
}
