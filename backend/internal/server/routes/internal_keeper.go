package routes

import (
	"crypto/subtle"
	"net/http"
	"os"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/handler"

	"github.com/gin-gonic/gin"
)

const (
	keeperInternalTokenEnv       = "SUB2APIPLUS_KEEPER_INTERNAL_TOKEN"
	legacyKeeperInternalTokenEnv = "KEEPER_INTERNAL_TOKEN"
)

func RegisterInternalKeeperRoutes(v1 *gin.RouterGroup, h *handler.Handlers) {
	internal := v1.Group("/internal/keeper")
	internal.Use(keeperInternalAuth())
	{
		internal.GET("/accounts", h.Admin.Account.KeeperListAccounts)
		internal.GET("/accounts/:id/models", h.Admin.Account.KeeperAccountModels)
		internal.POST("/accounts/:id/keepalive", h.Admin.Account.KeeperKeepalive)
	}
}

func keeperInternalAuth() gin.HandlerFunc {
	return func(c *gin.Context) {
		expected := keeperInternalToken()
		if expected == "" {
			c.AbortWithStatus(http.StatusNotFound)
			return
		}
		actual := extractKeeperInternalToken(c.GetHeader("Authorization"))
		if actual == "" {
			actual = strings.TrimSpace(c.GetHeader("X-Keeper-Token"))
		}
		if actual == "" || subtle.ConstantTimeCompare([]byte(actual), []byte(expected)) != 1 {
			c.AbortWithStatus(http.StatusUnauthorized)
			return
		}
		c.Next()
	}
}

func keeperInternalToken() string {
	if token := strings.TrimSpace(os.Getenv(keeperInternalTokenEnv)); token != "" {
		return token
	}
	return strings.TrimSpace(os.Getenv(legacyKeeperInternalTokenEnv))
}

func extractKeeperInternalToken(header string) string {
	header = strings.TrimSpace(header)
	if header == "" {
		return ""
	}
	if strings.HasPrefix(strings.ToLower(header), "bearer ") {
		return strings.TrimSpace(header[len("bearer "):])
	}
	return header
}
