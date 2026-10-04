"""新增路由的 Inventory 条目建议（UM-22）：别名、调用方与可并入的已有条目都按快照与源码起草。"""

from __future__ import annotations

import unittest

from tools.upstream_merge.surface_suggestions import (
    handler_caller,
    registration_caller,
    route_alias,
    suggest_inventory_entries,
)

SOURCE = """package routes

func registerAccountRoutes(accounts *gin.RouterGroup, h *handler.Handlers) {
\taccounts.GET("/:id/claude/reset-credits", h.Admin.Account.ClaudeResetCredits)
\taccounts.POST("/:id/claude/reset-credits/redeem", h.Admin.Account.RedeemClaudeResetCredit)
\taccounts.POST("/:id/odd", func(c *gin.Context) {})
}
"""

REDEEM = {
    "file": "backend/internal/server/routes/admin.go",
    "function": "registerAccountRoutes",
    "line_hint": 5,
    "method": "POST",
    "path": "/:id/claude/reset-credits/redeem",
    "receiver": "accounts",
    "route_fingerprint": "f1",
}
SYSTEMONE = {
    "file": "backend/internal/server/routes/gateway.go",
    "function": "RegisterGatewayRoutes",
    "line_hint": 99,
    "method": "POST",
    "path": "/systemone",
    "receiver": "gateway",
    "route_fingerprint": "f2",
}
INVENTORY = [
    {
        "logical_ingress_id": "account-admin-claude-reset-credits-routes",
        "physical_alias_ids": ["alias-account-claude-reset-credits-get"],
        "caller_ids": ["handler.Admin.Account.ClaudeResetCredits", "server.routes.registerAccountRoutes"],
    },
    {
        "logical_ingress_id": "account-admin-other",
        "physical_alias_ids": ["alias-account-other-get"],
        "caller_ids": ["server.routes.registerAccountRoutes"],
    },
]


class SurfaceSuggestionsTest(unittest.TestCase):
    def test_alias_and_callers_from_snapshot_and_source(self) -> None:
        self.assertEqual(route_alias(REDEEM), "alias-accounts-claude-reset-credits-redeem-post")
        self.assertEqual(registration_caller(REDEEM), "server.routes.registerAccountRoutes")
        self.assertEqual(handler_caller(SOURCE, 5), "handler.Admin.Account.RedeemClaudeResetCredit")
        # 匿名函数、越界行号都解析不出处理函数。
        self.assertIsNone(handler_caller(SOURCE, 6))
        self.assertIsNone(handler_caller(SOURCE, 99))

    def test_added_routes_get_existing_entry_or_new_id(self) -> None:
        deltas = [
            {"delta_id": "d1", "surface": "route", "change": "added", "identity": "f1", "clients": ["claude", "codex"]},
            {"delta_id": "d2", "surface": "route", "change": "added", "identity": "f2", "clients": ["codex"]},
            {"delta_id": "d3", "surface": "route", "change": "removed", "identity": "f9", "clients": ["codex"]},
            {"delta_id": "d4", "surface": "egress", "change": "added", "identity": "x", "clients": ["codex"]},
        ]
        suggestions = suggest_inventory_entries(
            deltas,
            [REDEEM, SYSTEMONE],
            lambda relative: SOURCE,
            {"claude": INVENTORY, "codex": INVENTORY},
        )
        self.assertEqual([item["delta_id"] for item in suggestions], ["d1", "d2"])
        redeem, systemone = suggestions
        self.assertEqual(
            redeem["suggested_caller_ids"],
            ["handler.Admin.Account.RedeemClaudeResetCredit", "server.routes.registerAccountRoutes"],
        )
        # 同一注册函数下别名词重合最多的已有条目排最前；没有重合的同注册函数条目不列。
        self.assertEqual(
            [(item["client"], item["logical_ingress_id"]) for item in redeem["existing_entry_candidates"]],
            [("claude", "account-admin-claude-reset-credits-routes"), ("codex", "account-admin-claude-reset-credits-routes")],
        )
        self.assertIsNone(redeem["new_logical_ingress_id"])
        # 没有可并入的条目时建议新建；处理函数解析不出时只给注册函数。
        self.assertEqual(systemone["existing_entry_candidates"], [])
        self.assertEqual(systemone["new_logical_ingress_id"], "gateway-systemone")
        self.assertEqual(systemone["suggested_caller_ids"], ["server.routes.RegisterGatewayRoutes"])


if __name__ == "__main__":
    unittest.main()
