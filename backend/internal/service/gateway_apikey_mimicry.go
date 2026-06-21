package service

import (
	"bytes"
	"context"
	"net/http"

	"github.com/gin-gonic/gin"
)

func shouldMimicAnthropicAPIKeyClaudeCode(account *Account, tokenType string) bool {
	return account != nil &&
		tokenType == "apikey" &&
		account.IsAnthropicAPIKeyClaudeCodeMimicEnabled()
}

func defaultAPIKeyCountTokensMimicBetaHeader(body []byte) string {
	beta := defaultAPIKeyBetaHeader(body)
	return mergeAnthropicBeta([]string{beta, "token-counting-2024-11-01"}, "")
}

func (s *GatewayService) buildAnthropicAPIKeyCLIMimicRequest(
	ctx context.Context,
	account *Account,
	body []byte,
	token string,
	targetURL string,
	reqStream bool,
	effectiveDropSet map[string]struct{},
) (*http.Request, []byte, error) {
	finalBetaHeader := stripBetaTokensWithSet(defaultAPIKeyBetaHeader(body), effectiveDropSet)
	if sanitized, changed := sanitizeAnthropicBodyForBetaTokens(body, finalBetaHeader); changed {
		body = sanitized
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	setHeaderRaw(req.Header, "x-api-key", token)
	setHeaderRaw(req.Header, "content-type", "application/json")
	setHeaderRaw(req.Header, "anthropic-version", "2023-06-01")
	applyClaudeCodeMimicHeaders(req, reqStream)
	deleteHeaderAllForms(req.Header, "anthropic-beta")
	if finalBetaHeader != "" {
		setHeaderRaw(req.Header, "anthropic-beta", finalBetaHeader)
	}
	return req, body, nil
}

func (s *GatewayService) buildAnthropicAPIKeyCLICountTokensMimicRequest(
	ctx context.Context,
	account *Account,
	body []byte,
	token string,
	targetURL string,
	effectiveDropSet map[string]struct{},
) (*http.Request, []byte, error) {
	body = sanitizeCountTokensRequestBody(body)
	finalBetaHeader := stripBetaTokensWithSet(defaultAPIKeyCountTokensMimicBetaHeader(body), effectiveDropSet)
	if sanitized, changed := sanitizeAnthropicBodyForBetaTokens(body, finalBetaHeader); changed {
		body = sanitized
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	setHeaderRaw(req.Header, "x-api-key", token)
	setHeaderRaw(req.Header, "content-type", "application/json")
	setHeaderRaw(req.Header, "anthropic-version", "2023-06-01")
	applyClaudeCodeMimicHeaders(req, false)
	deleteHeaderAllForms(req.Header, "anthropic-beta")
	if finalBetaHeader != "" {
		setHeaderRaw(req.Header, "anthropic-beta", finalBetaHeader)
	}
	return req, body, nil
}

func safeClientHeaders(c *gin.Context) http.Header {
	if c == nil || c.Request == nil {
		return http.Header{}
	}
	return c.Request.Header
}
