package repository

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/stretchr/testify/require"
	"github.com/stretchr/testify/suite"
)

type OpenAIOAuthServiceSuite struct {
	suite.Suite
	ctx      context.Context
	srv      *httptest.Server
	svc      *openaiOAuthService
	received chan url.Values
}

func (s *OpenAIOAuthServiceSuite) SetupTest() {
	var err error
	s.ctx, err = officialegress.WithReleaseMode(
		context.Background(), officialegress.ReleaseModeActive,
	)
	require.NoError(s.T(), err)
	s.received = make(chan url.Values, 1)
	configureObserveGuardForOpenAIOAuthLocalTest(s.T())
}

// configureObserveGuardForOpenAIOAuthLocalTest 仅允许本套件把官方 OAuth 请求重定向到
// httptest 的本地地址。生产默认值从 1C 起保持 enforce，测试不得隐式依赖旧默认值。
func configureObserveGuardForOpenAIOAuthLocalTest(t *testing.T) {
	t.Helper()
	previous := officialegress.DefaultGuard()
	_, err := officialegress.ConfigureDefaultGuard(officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyObserve),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyObserve),
	}, nil)
	require.NoError(t, err)
	t.Cleanup(func() {
		_, restoreErr := officialegress.ConfigureDefaultGuard(previous.Config(), previous.Recorder())
		require.NoError(t, restoreErr)
	})
}

func (s *OpenAIOAuthServiceSuite) TearDownTest() {
	if s.srv != nil {
		s.srv.Close()
		s.srv = nil
	}
}

func (s *OpenAIOAuthServiceSuite) setupServer(handler http.HandlerFunc) {
	s.srv = newLocalTestServer(s.T(), handler)
	s.svc = &openaiOAuthService{tokenURL: s.srv.URL}
}

func (s *OpenAIOAuthServiceSuite) TestExchangeCode_DefaultRedirectURI() {
	errCh := make(chan string, 1)
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Empty(s.T(), r.Header.Get("User-Agent"))
		require.Empty(s.T(), r.Header.Get("originator"))
		require.Equal(s.T(), "*/*", r.Header.Get("Accept"))
		if r.Method != http.MethodPost {
			errCh <- "method mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if err := r.ParseForm(); err != nil {
			errCh <- "ParseForm failed"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.PostForm.Get("grant_type"); got != "authorization_code" {
			errCh <- "grant_type mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.PostForm.Get("client_id"); got != openai.ClientID {
			errCh <- "client_id mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.PostForm.Get("code"); got != "code" {
			errCh <- "code mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.PostForm.Get("redirect_uri"); got != openai.DefaultRedirectURI {
			errCh <- "redirect_uri mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.PostForm.Get("code_verifier"); got != "ver" {
			errCh <- "code_verifier mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		wantUA, wantOriginator := service.CodexCanonicalAuthIdentity()
		if got := r.Header.Get("User-Agent"); got != wantUA {
			errCh <- "user-agent mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.Header.Get("originator"); got != wantOriginator {
			errCh <- "originator mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"access_token":"at","refresh_token":"rt","token_type":"bearer","expires_in":3600}`)
	}))

	resp, err := s.svc.ExchangeCode(s.ctx, "code", "ver", "", "", "")
	require.NoError(s.T(), err, "ExchangeCode")
	select {
	case msg := <-errCh:
		require.Fail(s.T(), msg)
	default:
	}
	require.Equal(s.T(), "at", resp.AccessToken)
	require.Equal(s.T(), "rt", resp.RefreshToken)
}

func (s *OpenAIOAuthServiceSuite) TestDecodeRefreshResponseSuccess() {
	response := &http.Response{
		StatusCode: http.StatusOK,
		Body: io.NopCloser(strings.NewReader(
			`{"access_token":"at2","refresh_token":"rt2","token_type":"bearer","expires_in":3600}`,
		)),
	}
	decoded, err := s.svc.DecodeRefreshResponse(s.ctx, response, nil, "")
	require.NoError(s.T(), err)
	require.Equal(s.T(), "at2", decoded.AccessToken)
	require.Equal(s.T(), "rt2", decoded.RefreshToken)
}

func (s *OpenAIOAuthServiceSuite) TestTokenClientUsesCodexTLSDialer() {
	profile, err := resolveOpenAIExchangeTLSProfile(officialegress.ReleaseModeActive)
	require.NoError(s.T(), err)
	client, err := createOpenAIExchangeReqClient("", profile)
	require.NoError(s.T(), err)
	require.NotNil(s.T(), client.GetTransport().DialTLSContext)
}

func (s *OpenAIOAuthServiceSuite) TestNonSuccessStatus_IncludesBody() {
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = io.WriteString(w, "bad")
	}))

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", openai.DefaultRedirectURI, "", "")
	require.Error(s.T(), err)
	require.ErrorContains(s.T(), err, "status 400")
	require.ErrorContains(s.T(), err, "bad")
}

func (s *OpenAIOAuthServiceSuite) TestRequestErrorWithProxyKeepsGenericReason() {
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	s.srv.Close()

	_, err := s.svc.ExchangeCode(
		s.ctx,
		"code",
		"ver",
		openai.DefaultRedirectURI,
		"http://127.0.0.1:1",
		"",
	)
	require.Error(s.T(), err)
	require.Equal(s.T(), "OPENAI_OAUTH_REQUEST_FAILED", infraerrors.Reason(err))
	require.ErrorContains(s.T(), err, "request failed")
}

func (s *OpenAIOAuthServiceSuite) TestExchangeCode_RequestErrorWithoutProxyReturnsDirectConnectionHint() {
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	s.srv.Close()

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", openai.DefaultRedirectURI, "", "")

	require.Error(s.T(), err)
	require.Equal(s.T(), "OPENAI_OAUTH_DIRECT_CONNECTION_FAILED", infraerrors.Reason(err))
	require.Contains(s.T(), infraerrors.Message(err), "could not reach OpenAI directly")
}

func (s *OpenAIOAuthServiceSuite) TestProfileErrorIsNotReportedAsDirectConnectionFailure() {
	err := &url.Error{
		Op:  http.MethodPost,
		URL: openai.TokenURL,
		Err: errors.New("official h1 wire request does not match immutable profile"),
	}

	require.False(s.T(), shouldReturnOpenAIDirectConnectionHint(s.ctx, "", err))
}

func (s *OpenAIOAuthServiceSuite) TestContextCancel() {
	started := make(chan struct{})
	block := make(chan struct{})
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-block
	}))

	ctx, cancel := context.WithCancel(s.ctx)

	done := make(chan error, 1)
	go func() {
		_, err := s.svc.ExchangeCode(ctx, "code", "ver", openai.DefaultRedirectURI, "", "")
		done <- err
	}()

	select {
	case <-started:
	case <-time.After(2 * time.Second):
		s.T().Fatal("OAuth 取消测试的本地服务器未收到请求")
	}
	cancel()
	close(block)

	var err error
	select {
	case err = <-done:
	case <-time.After(2 * time.Second):
		s.T().Fatal("OAuth 请求取消后未按时返回")
	}
	require.Error(s.T(), err)
}

func (s *OpenAIOAuthServiceSuite) TestExchangeCode_UsesProvidedRedirectURI() {
	want := "http://localhost:9999/cb"
	errCh := make(chan string, 1)
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if got := r.PostForm.Get("redirect_uri"); got != want {
			errCh <- "redirect_uri mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"access_token":"at","token_type":"bearer","expires_in":1}`)
	}))

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", want, "", "")
	require.NoError(s.T(), err, "ExchangeCode")
	select {
	case msg := <-errCh:
		require.Fail(s.T(), msg)
	default:
	}
}

func (s *OpenAIOAuthServiceSuite) TestExchangeCode_UseProvidedClientID() {
	wantClientID := "custom-exchange-client-id"
	errCh := make(chan string, 1)
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		if got := r.PostForm.Get("client_id"); got != wantClientID {
			errCh <- "client_id mismatch"
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"access_token":"at","token_type":"bearer","expires_in":1}`)
	}))

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", openai.DefaultRedirectURI, "", wantClientID)
	require.NoError(s.T(), err, "ExchangeCode")
	select {
	case msg := <-errCh:
		require.Fail(s.T(), msg)
	default:
	}
}

func (s *OpenAIOAuthServiceSuite) TestTokenURL_CanBeOverriddenWithQuery() {
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		s.received <- r.PostForm
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"access_token":"at","token_type":"bearer","expires_in":1}`)
	}))
	s.svc.tokenURL = s.srv.URL + "?x=1"

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", openai.DefaultRedirectURI, "", "")
	require.NoError(s.T(), err, "ExchangeCode")
	select {
	case <-s.received:
	default:
		require.Fail(s.T(), "expected server to receive request")
	}
}

func (s *OpenAIOAuthServiceSuite) TestExchangeCode_SuccessButInvalidJSON() {
	s.setupServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "not-valid-json")
	}))

	_, err := s.svc.ExchangeCode(s.ctx, "code", "ver", openai.DefaultRedirectURI, "", "")
	require.Error(s.T(), err, "expected error for invalid JSON response")
	require.Equal(s.T(), "OPENAI_OAUTH_REQUEST_FAILED", infraerrors.Reason(err))
}

func (s *OpenAIOAuthServiceSuite) TestRefreshToken_NonSuccessStatus() {
	response := &http.Response{
		StatusCode: http.StatusUnauthorized,
		Body:       io.NopCloser(strings.NewReader("unauthorized")),
	}
	_, err := s.svc.DecodeRefreshResponse(s.ctx, response, nil, "")
	require.Error(s.T(), err, "expected error for non-2xx status")
	require.ErrorContains(s.T(), err, "status 401")
}

func TestNewOpenAIOAuthClient_DefaultTokenURL(t *testing.T) {
	client := NewOpenAIOAuthClient()
	svc, ok := client.(*openaiOAuthService)
	require.True(t, ok)
	require.Equal(t, openai.TokenURL, svc.tokenURL)
}

func TestOpenAIOAuthServiceSuite(t *testing.T) {
	suite.Run(t, new(OpenAIOAuthServiceSuite))
}
