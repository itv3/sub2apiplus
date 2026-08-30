package officialegress

import (
	"testing"

	"github.com/stretchr/testify/require"
)

func TestParseOfficialCodexRoutingHintFactsModelOnly(t *testing.T) {
	facts, err := ParseOfficialCodexRoutingHintFacts(
		"responses_http",
		[]byte(`{"model":"gpt-5.5"}`),
	)
	require.NoError(t, err)
	require.Equal(t, "gpt-5.5", facts.Model())
	_, present := facts.ServiceTier()
	require.False(t, present)
	headerValue, err := facts.HeaderValue()
	require.NoError(t, err)
	require.Equal(t, "model=gpt-5.5", headerValue)
	require.NotEmpty(t, facts.Digest())
}

func TestParseOfficialCodexRoutingHintFactsWithServiceTier(t *testing.T) {
	facts, err := ParseOfficialCodexRoutingHintFacts(
		"responses_compact",
		[]byte(`{"service_tier":"priority","model":"gpt-5.4-mini"}`),
	)
	require.NoError(t, err)
	tier, present := facts.ServiceTier()
	require.True(t, present)
	require.Equal(t, "priority", tier)
	headerValue, err := facts.HeaderValue()
	require.NoError(t, err)
	require.Equal(t, "model=gpt-5.4-mini;tier=priority", headerValue)
}

func TestParseOfficialCodexRoutingHintFactsTreatsNullAndAbsentTierAsAbsent(t *testing.T) {
	for _, body := range []string{
		`{"model":"gpt-5.6-terra"}`,
		`{"model":"gpt-5.6-terra","service_tier":null}`,
	} {
		facts, err := ParseOfficialCodexRoutingHintFacts("responses_ws", []byte(body))
		require.NoError(t, err)
		_, present := facts.ServiceTier()
		require.False(t, present)
		headerValue, err := facts.HeaderValue()
		require.NoError(t, err)
		require.Equal(t, "model=gpt-5.6-terra", headerValue)
	}
}

func TestParseOfficialCodexRoutingHintFactsRejectsDuplicateKeys(t *testing.T) {
	for _, body := range []string{
		`{"model":"gpt-5.5","model":"gpt-5.4-mini"}`,
		`{"model":"gpt-5.5","service_tier":"priority","service_tier":null}`,
	} {
		_, err := ParseOfficialCodexRoutingHintFacts("responses_http", []byte(body))
		require.ErrorContains(t, err, "字段重复")
	}
}

func TestParseOfficialCodexRoutingHintFactsRejectsInvalidHeaderBytes(t *testing.T) {
	for _, body := range []string{
		`{"model":"gpt-5.5\nforged"}`,
		`{"model":"gpt-5.5","service_tier":"priority\rforged"}`,
	} {
		_, err := ParseOfficialCodexRoutingHintFacts("responses_http", []byte(body))
		require.ErrorContains(t, err, "非法 Header 字节")
	}
}

func TestOptionalCodexRoutingHintFactsAllowsLegacyBodyWithoutModel(t *testing.T) {
	document, err := newOrderedJSONDocument([]byte(`{"input":[]}`))
	require.NoError(t, err)
	facts, err := optionalCodexRoutingHintFactsFromDocument(document)
	require.NoError(t, err)
	require.True(t, facts.IsZero())

	_, err = codexRoutingHintFactsFromDocument(document)
	require.ErrorContains(t, err, "缺少 model")
}
