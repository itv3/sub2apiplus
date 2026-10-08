# 历史交付策略回归夹具

`claude_delivery_policy_v3_551a01c17.json` 是提交
`551a01c1762b7f6604e5be5716a195cbf251531b` 的
`tools/official_client_capture/claude_tool_identity_policy_v2.json`
原始字节，长度 11,085 字节，SHA-256：
`86d99e3a0906a4e2f5c96bcc0b0026f1891aa1ac43746e6076b37365b3205ace`。

本材料只用于 `regression_fixture`。历史交付测试先把原策略写入隔离副本的受管根，
再由正式身份引擎在该树实算完整身份；不改已封存身份的数字，也不替换合同准入函数。
该树的其余材料为合成回归工具树，不代表该历史提交的完整真实部署。

旧交付 plan、阶段事实与收据只验证历史形成期语义及恢复逻辑，不能作为当前交付成功、
真实来源或发布认证的凭证。当前 policy 4 的 typed 集成保持默认策略，并核缺配置与
旧 plan 在执行前零变更拒绝；非交付测试也继续使用当前受管树。
