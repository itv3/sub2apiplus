package service

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"
	"github.com/stretchr/testify/require"
)

type groupRepoStubForModelsListCandidates struct {
	GroupRepository

	group *Group
	err   error
}

func (s *groupRepoStubForModelsListCandidates) GetByIDLite(_ context.Context, _ int64) (*Group, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.group, nil
}

type accountRepoStubForModelsListCandidates struct {
	AccountRepository

	accounts []Account
	calls    int
}

func (s *accountRepoStubForModelsListCandidates) ListSchedulableByGroupID(_ context.Context, _ int64) ([]Account, error) {
	s.calls++
	out := make([]Account, len(s.accounts))
	copy(out, s.accounts)
	return out, nil
}

func TestDefaultModelsListCandidateIDsAntigravityUsesOfficialModels(t *testing.T) {
	require.Equal(t, antigravity.OfficialModelIDs(), defaultModelsListCandidateIDs(PlatformAntigravity))
}

func TestGetGroupModelsListCandidatesAntigravityDoesNotMergeAccountAliases(t *testing.T) {
	accountRepo := &accountRepoStubForModelsListCandidates{
		accounts: []Account{
			{
				Platform: PlatformAntigravity,
				Credentials: map[string]any{
					"model_mapping": map[string]any{
						"custom-alias": "o3",
					},
				},
			},
		},
	}
	svc := &adminServiceImpl{
		groupRepo: &groupRepoStubForModelsListCandidates{
			group: &Group{ID: 9, Platform: PlatformAntigravity},
		},
		accountRepo: accountRepo,
	}

	got, err := svc.GetGroupModelsListCandidates(context.Background(), 9, "")
	require.NoError(t, err)
	require.Equal(t, antigravity.OfficialModelIDs(), got)
	require.Zero(t, accountRepo.calls, "Antigravity 候选源不应再回读账号映射 alias")
}
